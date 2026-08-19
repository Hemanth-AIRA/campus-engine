# Self-Hosting LiveKit on a Fresh VM (for AIRA)

A step-by-step runbook to stand up a **self-hosted LiveKit SFU** on a clean Linux VM,
secure it with TLS (no domain purchase needed, via `sslip.io`), open the right
firewall permissions, and wire it back into the AIRA engine's `.env`.

This reproduces the setup previously used at
`wss://livekit-157-20-214-192.sslip.io` (the commented-out block in `.env`), so you
can switch off LiveKit Cloud and run your own server again.

---

## 0. Architecture (what you're building)

```
   Candidate browser  ──WSS/WebRTC──►  ┌─────────────────────────────┐
                                       │        FRESH VM             │
   AIRA bot (webrtc_interview.py) ───► │  Caddy (TLS :443/:80)       │
   uses LIVEKIT_API_KEY/SECRET         │    └─► LiveKit server :7880  │
                                       │  Redis (room state)         │
                                       │  media: UDP 50000-60000     │
                                       └─────────────────────────────┘
```

- **LiveKit server** — the SFU that routes audio/video between the candidate and the AIRA agent.
- **Caddy** — terminates TLS and reverse-proxies `wss://…/` → LiveKit `:7880`. Gets a
  real Let's Encrypt cert automatically using an `sslip.io` hostname (no DNS setup).
- **Redis** — LiveKit's state store (required for a single node too).
- The **AIRA bot** is a *client* of this server; it just needs `LIVEKIT_URL` + the
  matching API key/secret. TTS (Cartesia) and STT (Deepgram) are unrelated to the SFU.

---

## 1. VM sizing & OS

| Item        | Recommendation                                              |
|-------------|-------------------------------------------------------------|
| OS          | Ubuntu 22.04 or 24.04 LTS (x86_64)                          |
| vCPU        | 2 (4 for many concurrent interviews)                        |
| RAM         | 4 GB (8 GB for heavier load)                                |
| Disk        | 20 GB SSD                                                   |
| Public IP   | **Static / Elastic IP** (the sslip.io hostname encodes it) |
| Egress      | Open (pulls Docker images, Let's Encrypt, etc.)            |

> Media is UDP-heavy. Pick a VM/region close to your candidates for low latency.
> If you later resize or reassign the IP, the sslip.io hostname changes — see §7.

---

## 2. Firewall / Security-Group PERMISSIONS (the important part)

WebRTC needs more than port 443. Open these **inbound** rules in your cloud
security group (AWS SG / GCP firewall / Azure NSG / Hetzner / etc.):

| Port        | Proto | Source        | Purpose                                    |
|-------------|-------|---------------|--------------------------------------------|
| 22          | TCP   | **your IP**   | SSH admin (restrict to your IP, not world) |
| 80          | TCP   | 0.0.0.0/0     | Let's Encrypt HTTP-01 challenge (Caddy)     |
| 443         | TCP   | 0.0.0.0/0     | WSS signaling (Caddy → LiveKit)             |
| 7881        | TCP   | 0.0.0.0/0     | WebRTC over TCP (ICE/TCP fallback)          |
| 50000-60000 | UDP   | 0.0.0.0/0     | WebRTC media (RTP) — **must be open**       |
| 3478        | UDP   | 0.0.0.0/0     | TURN/UDP (only if you enable embedded TURN) |
| 5349        | TCP   | 0.0.0.0/0     | TURN/TLS (only if you enable embedded TURN) |

**Common failure:** signaling connects (candidate sees "connecting") but there's no
audio/video — that's almost always the **UDP 50000-60000 range being blocked**. Open it.

> Do **not** expose port `7880` (LiveKit HTTP) or `6379` (Redis) to the internet —
> Caddy fronts 7880, and Redis stays local.

### On-host firewall (ufw) — optional, mirror the SG

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 7881/tcp
sudo ufw allow 50000:60000/udp
# only if using embedded TURN:
sudo ufw allow 3478/udp
sudo ufw allow 5349/tcp
sudo ufw enable
```

---

## 3. OS prep + a non-root user with the right permissions

SSH in as root (or a sudo user) and create a dedicated user for the stack:

```bash
# create a service user (no password login; you'll use it for the compose stack)
sudo adduser --disabled-password --gecos "" livekit

# base packages
sudo apt-get update && sudo apt-get install -y ca-certificates curl gnupg ufw
```

---

## 4. Install Docker + grant docker permission

```bash
# Docker Engine + compose plugin (official convenience script)
curl -fsSL https://get.docker.com | sudo sh

# PERMISSION: let the 'livekit' user run docker without sudo
sudo usermod -aG docker livekit

# enable docker on boot
sudo systemctl enable --now docker
```

Log in as the service user for the rest (group change takes effect on new login):

```bash
sudo su - livekit
docker ps        # should work WITHOUT sudo — confirms the docker permission
```

---

## 5. Generate the LiveKit config (keys + TLS + compose)

LiveKit ships a generator that produces `docker-compose.yaml`, `livekit.yaml`,
`caddy.yaml`, and API keys — all wired for TLS.

```bash
mkdir -p ~/livekit && cd ~/livekit

docker run --rm -it -v "$PWD:/output" livekit/generate
```

When prompted:

- **Domain** → enter your **sslip.io hostname** built from the VM's public IP with
  dashes: for IP `A.B.C.D` use `livekit-A-B-C-D.sslip.io`.
  _(Example: IP `157.20.214.192` → `livekit-157-20-214-192.sslip.io`.)_
  sslip.io resolves that name to the IP automatically — no DNS records to buy or set.
- **TURN** → enable it (recommended) if some candidates are behind strict NAT/corporate
  firewalls; it will use ports `3478/udp` + `5349/tcp` (open them in §2).

The generator prints an **API key** and **API secret** — **copy them now**, you'll put
them in AIRA's `.env` in §8. They also live in `~/livekit/livekit.yaml` under `keys:`.

### Lock down the files that hold secrets (PERMISSION)

```bash
chmod 600 ~/livekit/livekit.yaml ~/livekit/caddy.yaml
chown livekit:livekit ~/livekit/*.yaml
```

---

## 6. Start the stack

```bash
cd ~/livekit
docker compose up -d
docker compose ps        # livekit, redis, caddy should be "running"
docker compose logs -f caddy | grep -i certificate   # watch TLS cert get issued
```

Give Caddy ~30–60s to obtain the Let's Encrypt cert (needs ports 80 + 443 reachable
from the internet — that's why §2 opens them).

### Run it as a boot service (systemd wrapper)

So the stack restarts on reboot:

```bash
sudo tee /etc/systemd/system/livekit.service >/dev/null <<'EOF'
[Unit]
Description=LiveKit SFU (docker compose)
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=livekit
WorkingDirectory=/home/livekit/livekit
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now livekit.service
```

---

## 7. (Alternative) Bare-metal binary + systemd, no Docker

If you'd rather not use Docker:

```bash
curl -sSL https://get.livekit.io | sudo bash          # installs /usr/local/bin/livekit-server
livekit-server --generate-keys                        # prints APIkey: / secret:
sudo apt-get install -y redis-server && sudo systemctl enable --now redis-server
```

Create `/etc/livekit/livekit.yaml` (keys, `redis: {address: localhost:6379}`, rtc
port range `50000-60000`), then a `livekit.service` unit running
`livekit-server --config /etc/livekit/livekit.yaml`. You'll still need a TLS
reverse-proxy (Caddy/nginx) in front for `wss://`. The Docker path in §5–6 does all of
this for you, so it's the recommended route.

---

## 8. Wire it into AIRA's `.env`

On the **AIRA host** (not the LiveKit VM), edit `.env` — swap the LiveKit Cloud line
for your VM, using the **same key/secret the generator printed**:

```dotenv
AIRA_USE_LIVEKIT=true

# self-hosted on the fresh VM:
LIVEKIT_URL=wss://livekit-A-B-C-D.sslip.io
LIVEKIT_API_KEY=<key from step 5>
LIVEKIT_API_SECRET=<secret from step 5>

# (comment out the LiveKit Cloud line)
# LIVEKIT_URL=wss://webrtc-interviews-7j6om7h0.livekit.cloud
```

> The API key/secret **must be identical** on the server (`livekit.yaml`) and in
> AIRA's `.env` — that's how the bot authenticates to create rooms and mint join tokens.
> Restart the AIRA bot after editing `.env`.

⚠️ Rotate the currently-committed LiveKit Cloud key/secret in `.env` and add `.env` to
`.gitignore` if it isn't already — live credentials shouldn't sit in the repo.

---

## 9. Verify

```bash
# 1) TLS + signaling reachable (from anywhere)
curl -sI https://livekit-A-B-C-D.sslip.io      # expect HTTP 200/404 over valid TLS (not a cert error)

# 2) Full round-trip using the LiveKit CLI (install: curl -sSL https://get.livekit.io/cli | bash)
livekit-cli create-token \
  --api-key <KEY> --api-secret <SECRET> \
  --url wss://livekit-A-B-C-D.sslip.io \
  --join --room test --identity tester --valid-for 1h

# 3) Media path: open https://meet.livekit.io, connect with that URL + token,
#    and confirm audio/video actually flow (proves UDP 50000-60000 is open).
```

Then run one real AIRA interview end-to-end and confirm the bot joins and audio works.

---

## 10. Day-2 ops

```bash
cd ~/livekit
docker compose logs -f livekit         # server logs
docker compose restart livekit         # restart just the SFU
docker compose pull && docker compose up -d   # update to latest images
sudo systemctl status livekit          # boot-service health
```

---

## Permissions checklist (quick reference)

- [ ] Security group: 443/tcp, 80/tcp, 7881/tcp, **50000-60000/udp** open to the world
- [ ] Security group: 22/tcp restricted to your admin IP
- [ ] TURN ports (3478/udp, 5349/tcp) open **only if** TURN enabled
- [ ] `7880/tcp` and `6379/tcp` **not** exposed publicly
- [ ] `livekit` user added to the `docker` group (`docker ps` works without sudo)
- [ ] `livekit.yaml` / `caddy.yaml` are `chmod 600`, owned by `livekit`
- [ ] Static/Elastic IP attached (sslip.io hostname depends on it)
- [ ] Same API key/secret in server config **and** AIRA `.env`
- [ ] `.env` gitignored; old Cloud credentials rotated
