# Deploying the key-pool Redis on the LiveKit VM

A step-by-step runbook to stand up a **dedicated Redis** for AIRA's API-key concurrency
pool on the existing LiveKit VM (`134.195.138.72`), the **same docker + systemd way**
LiveKit is deployed (see [LIVEKIT_SELF_HOST_VM.md](LIVEKIT_SELF_HOST_VM.md)). Secured with
a password + a firewall allowlist to the engine node — **no TLS/certs**, because this Redis
never holds any secrets.

This is **Phase 1**. The engine keeps using its per-VM file-lock pool until Phase 2 (the
`api_key_pool.py` Redis backend) ships; standing up this Redis now changes nothing in the
running system.

---

## 0. Why this exists

The engine caps concurrent calls per API key (Cartesia ≈ 5 streams/key, etc.). It runs
interviews across processes and scales to **3 replicas** in k8s. Today the pool tracks
in-use slots in a **file on local disk** with **PID liveness** — both are **per-VM**, so a
2nd replica has its own view and they **collectively blow past every key's limit**. The fix
is one **shared Redis** that all replicas coordinate through.

**What this Redis holds:** only the live slot/lease counts + the FIFO waiter queue — all
**reclaimable** state. **What it never holds:** your API keys. Keys and per-key limits stay
in the engine `.env`. Because nothing sensitive crosses the wire, **AUTH + firewall** is
sufficient; no certificates, no renewal.

```
   engine node (103.127.31.224)  ──redis://:PW──►  LiveKit VM (134.195.138.72)
   [engine pods share this        AUTH + firewall     ┌──────────────────────────┐
    egress IP as they scale]       (engine IP only)   │ aira-keypool-redis :6380  │  ← this
                                                       │ livekit + redis(:6379) +  │
                                                       │ caddy  (unchanged)        │
                                                       └──────────────────────────┘
```

> **Does the IP allowlist limit scaling? No.** Engine pods egress through the node/NAT
> egress IP — a stable address that does not change as you add replicas — so one allowlist
> entry covers every pod. Auth is a shared password, identical for all pods.

---

## 1. Prereqs

- SSH access to the VM as the `livekit` service user (docker already installed & usable
  without sudo — `docker ps` works).
- The engine's **egress IP** — the address the engine leaves from, which is what you
  allowlist. Confirm per environment with `curl -s ifconfig.me` from the engine host/pod:
  - **Campus-Prod** = `157.20.214.104` (verified — this is the one that runs the high-volume
    interviews).
  - **Main prod** = `103.127.31.224` (allowlist too only if it shares the same API keys).
- Files from this repo's [deploy/keypool-redis/](../deploy/keypool-redis/):
  `docker-compose.yaml`, `.env.example`, `keypool-redis.service`.

---

## 2. Copy the files & set the password

```bash
sudo su - livekit
mkdir -p ~/keypool-redis && cd ~/keypool-redis

# copy docker-compose.yaml and .env.example here (scp them, or paste), then:
cp .env.example .env
# set a strong password:
sed -i "s#^KEYPOOL_REDIS_PASSWORD=.*#KEYPOOL_REDIS_PASSWORD=$(openssl rand -hex 32)#" .env
chmod 600 .env
grep KEYPOOL_REDIS_PASSWORD .env      # copy this value — it goes in the engine REDIS_URL later
```

---

## 3. Firewall — open 6380 to the engine node ONLY (both layers)

WebRTC media taught us this VM is firewalled at **two** layers; mirror both. **Never**
open 6380 to `0.0.0.0/0` — that would expose the pool to the world.

**On-host ufw:**
```bash
sudo ufw allow from 103.127.31.224 to any port 6380 proto tcp
sudo ufw reload
```

**Utho panel:** add an inbound rule → TCP `6380`, Source `103.127.31.224/32` only.

> Leave LiveKit's own Redis (6379) unexposed exactly as it is today.

---

## 4. Start the stack

```bash
cd ~/keypool-redis
docker compose up -d
docker compose ps                 # aira-keypool-redis should be "running (healthy)"
docker compose logs --tail=20 keypool-redis
```

### Run it as a boot service (so it survives reboots)

```bash
sudo cp ~/keypool-redis/keypool-redis.service /etc/systemd/system/keypool-redis.service
sudo systemctl daemon-reload
sudo systemctl enable --now keypool-redis.service
sudo systemctl status keypool-redis.service
```

---

## 5. Verify

```bash
# On the VM — local authed ping:
docker exec aira-keypool-redis redis-cli -a "$(grep -oP '(?<=KEYPOOL_REDIS_PASSWORD=).*' ~/keypool-redis/.env)" ping   # -> PONG
```

**From an engine pod** (the real test — proves reachability + auth + allowlist):
```bash
# TCP reachable?
nc -vz 134.195.138.72 6380
# Authed ping (install redis tools in the pod if needed, or use python below):
redis-cli -u redis://:<PASSWORD>@134.195.138.72:6380 ping     # -> PONG
```

Pure-python check (no redis-cli needed), run inside a pod:
```python
import socket
s = socket.create_connection(("134.195.138.72", 6380), timeout=4)
s.sendall(b"AUTH <PASSWORD>\r\nPING\r\n")
print(s.recv(128))     # expect b'+OK\r\n+PONG\r\n'
```

**Negative test (allowlist works):** from any *other* host, `nc -vz 134.195.138.72 6380`
should **time out / be refused**.

**Reboot test:** `sudo reboot` the VM, wait, then re-run the pod ping → still `PONG`.

---

## 6. Hand-off to the engine (Phase 2)

Nothing to change in the running engine yet. When Phase 2 lands, set in the engine
environment (k8s Secret / `.env`):

```dotenv
REDIS_URL=redis://:<PASSWORD>@134.195.138.72:6380/0
AIRA_KEYPOOL_BACKEND=auto        # redis when reachable, else falls back to the file pool
```

Keys + limits stay where they are (`CARTESIA_API_KEY`, `CARTESIA_MAX_CONCURRENCY`, …).

---

## 7. Day-2 ops

```bash
cd ~/keypool-redis
docker compose logs -f keypool-redis          # logs
docker compose restart keypool-redis          # restart
docker compose pull && docker compose up -d   # update the redis image
sudo systemctl status keypool-redis.service   # boot-service health

# rotate the password:
sed -i "s#^KEYPOOL_REDIS_PASSWORD=.*#KEYPOOL_REDIS_PASSWORD=$(openssl rand -hex 32)#" .env
docker compose up -d                          # recreate with the new password
# then update REDIS_URL in the engine and roll the engine.
```

Inspect live usage (once Phase 2 is deployed): `python keypool_status.py` from the engine,
or on the VM:
```bash
docker exec aira-keypool-redis redis-cli -a "$PW" --scan --pattern 'kp:*'
```

---

## Checklist

- [ ] `~/keypool-redis/.env` has a strong password, `chmod 600`
- [ ] `docker compose ps` shows `aira-keypool-redis` **healthy**
- [ ] ufw: 6380/tcp allowed **only** from `103.127.31.224`
- [ ] Utho panel: 6380/tcp source `103.127.31.224/32` **only** (not 0.0.0.0/0)
- [ ] LiveKit's 6379 still **not** exposed
- [ ] `keypool-redis.service` enabled → survives reboot
- [ ] Engine pod gets `PONG`; a non-allowlisted host is refused
- [ ] `REDIS_URL` recorded for the Phase-2 engine rollout
