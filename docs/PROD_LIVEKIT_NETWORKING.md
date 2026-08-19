# Prod LiveKit — media (audio/video) networking fix

## Symptom
On production, interviews run on the LiveKit path (bot connects, speaks greeting via
Cartesia, recording starts) but the candidate hears **no bot audio** and the bot logs
`No candidate audio track after 60s` / `0ms of candidate audio` every cycle → the
interview terminates as "muted / no audio."

## Root cause: WebRTC **media (UDP)** can't flow between the engine pod and the LiveKit VM
Signaling (WSS/443) connects fine; only the media transport fails. Confirmed from inside
the engine pod (`kubectl exec … -- python`):

| Target `livekit-134-195-138-72.sslip.io` | Result |
|---|---|
| 443/tcp (signaling) | ✅ OPEN |
| 7881/tcp (ICE-TCP / WebRTC-over-TCP) | ✅ OPEN |
| 5349/tcp (TURN/TLS) | ❓ (test — likely BLOCKED, never opened on the VM) |
| 3478/udp (TURN/UDP) | ❌ TIMEOUT (blocked) |
| 50000–60000/udp (media) | ❌ blocked |

STUN probe to 3478/udp times out → **UDP egress from the pod is blocked.** The bot has
no working media path: UDP direct blocked, TURN/UDP blocked, TURN/TLS(5349) not open,
and the client isn't auto-selecting ICE-TCP(7881).

Everything else is correct and verified: merged code, LiveKit path enabled
(`AIRA_USE_LIVEKIT=true`), correct `LIVEKIT_URL`/keys, and the server `livekit.yaml`
already has `rtc.tcp_port: 7881`, `use_external_ip: true`, and TURN enabled.

## Fix — pick ONE

### Option 1 (recommended, deterministic): open UDP egress
Allow **outbound UDP from the engine pods/nodes → `134.195.138.72`**:
- `50000-60000/udp` (WebRTC media)
- `3478/udp` (TURN/UDP)

Where to change:
- k8s **NetworkPolicy** egress (if one restricts it), AND
- the **node / cloud security group** (outbound UDP).
- VM inbound is already open (media worked from a laptop earlier), but re-confirm ufw +
  the **Utho panel firewall** allow those UDP ports.

Result: media flows over UDP exactly as it does from a normal client. No LiveKit change.

### Option 2 (if UDP truly cannot be opened): TCP relay
The pod can do TCP (443, 7881). To carry media over TCP:
1. **Open `5349/tcp` on the VM** (TURN/TLS) — ufw **and** Utho panel:
   ```sh
   ufw allow 5349/tcp
   ```
   Then re-test `5349` from the pod (must be OPEN).
2. Ensure the client actually relays. The LiveKit token already carries TURN creds, so
   once 5349 is reachable the bot should relay media over TURN/TLS. If it still doesn't,
   inspect the server ICE exchange:
   ```sh
   # on the LiveKit VM
   cd /home/livekit/livekit-134-195-138-72.sslip.io
   docker compose logs -f livekit | grep -iE "AIRA-|ice|candidate|transport|tcp|relay|selected|failed"
   ```
This path is less certain than Option 1 because the client already ignores the open
7881 ICE-TCP candidate.

## Diagnostic commands used (from inside the engine pod)
```python
# UDP egress (STUN to TURN/UDP) — timeout = UDP blocked
import socket, os
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(3)
s.sendto(b'\x00\x01\x00\x00\x21\x12\xa4\x42'+os.urandom(12), ("livekit-134-195-138-72.sslip.io", 3478))
s.recvfrom(64)            # bytes = OK, socket.timeout = blocked

# TCP reachability
for p in (443, 7881, 5349):
    try: socket.create_connection(("livekit-134-195-138-72.sslip.io", p), timeout=3); print(p, "OPEN")
    except Exception as e: print(p, "BLOCKED", e)
```

## LiveKit server config (already correct — for reference)
`/home/livekit/livekit-134-195-138-72.sslip.io/livekit.yaml`
```yaml
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 60000
  use_external_ip: true
turn:
  enabled: true
  domain: livekit-turn-134-195-138-72.sslip.io
  tls_port: 5349
  udp_port: 3478
  external_tls: true
```

## Bottom line
Engine code and LiveKit config are correct. The only blocker is the **network path for
WebRTC media**. Preferred fix: **open outbound UDP (50000-60000 + 3478) from the pods to
the LiveKit VM**. Fallback: **open `5349/tcp` and relay over TURN/TLS**.
