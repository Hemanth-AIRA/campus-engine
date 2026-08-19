"""
api_key_pool.py — Cross-process / cross-POD API-key concurrency pool.

WHY THIS EXISTS
───────────────
Each provider caps how many concurrent calls one API key may run (the
"magic number" — e.g. Cartesia ≈ 5 concurrent streams/key). AIRA runs many
interviews as SEPARATE OS PROCESSES, and now as MULTIPLE k8s REPLICAS (pods)
across two environments (main prod + campus prod). An in-memory counter can't
see other processes, and a per-VM file can't see other pods — either way the
key limit gets blown past.

BACKENDS
────────
• RedisBackend (shared, multi-pod safe): slot/lease state lives in a shared
  Redis (on the LiveKit VM). Atomic via Lua. Crash-safety via lease TTL +
  heartbeat (works across hosts, unlike PID liveness). This is the default when
  REDIS_URL is set and reachable.
• FileBackend (single-VM fallback): the original fcntl.flock'd JSON file with
  PID liveness. Used automatically when Redis isn't configured/reachable, so a
  Redis blip never hard-fails an interview.

Keys + per-key limits stay in .env (CARTESIA_API_KEY, CARTESIA_MAX_CONCURRENCY,
…) — NO secret is ever stored in Redis. Redis holds only reclaimable slot state,
keyed by the *identity hash* of each key (not its position), so two environments
sharing the same keys coordinate correctly even if their .env ordering differs.

MODEL (.env — unchanged)
────────────────────────
    CARTESIA_API_KEY=key_a,key_b,key_c
    CARTESIA_MAX_CONCURRENCY=5
    REDIS_URL=redis://:<pw>@134.195.138.72:6380/0     # enables the shared backend
    AIRA_KEYPOOL_BACKEND=auto                         # auto | redis | file

USAGE (unchanged)
─────────────────
    from api_key_pool import acquire, acquire_async, acquire_key_async, release_key
    with acquire("openai") as api_key: ...
    async with acquire_async("cartesia") as api_key: ...
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from typing import Dict, List, Optional, Tuple

try:  # ensure REDIS_URL / keys from .env are present even if imported very early
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass

try:
    import fcntl  # POSIX (macOS, Linux) — present on the AIRA VMs
except ImportError:  # pragma: no cover - Windows dev only
    fcntl = None

try:  # redis>=5 ships both sync + asyncio clients
    import redis as _redis_sync
    import redis.asyncio as _redis_async
    from redis.exceptions import RedisError as _RedisError
except Exception:  # pragma: no cover - redis missing → file backend only
    _redis_sync = None
    _redis_async = None
    class _RedisError(Exception):
        pass

logger = logging.getLogger("api_key_pool")

# ── Provider → .env prefix + default per-key concurrency ─────────────────────
_PROVIDERS: Dict[str, Tuple[str, int]] = {
    "cartesia": ("CARTESIA", 5),
    "deepgram": ("DEEPGRAM", 5),
    "openai":   ("OPENAI", 10),
}

# Shared state directory (file backend) — same path for every process on the VM.
_STATE_DIR = os.getenv(
    "AIRA_KEYPOOL_DIR", os.path.join(tempfile.gettempdir(), "aira_keypool")
)

# Safety valve: after this many seconds fully saturated, proceed over-limit.
_MAX_WAIT_S = float(os.getenv("AIRA_KEYPOOL_MAX_WAIT", "180"))

# File-backend lease staleness (PID-based). Redis uses its own shorter TTL below.
_LEASE_TTL_S = float(os.getenv("AIRA_KEYPOOL_LEASE_TTL", "1800"))

# How often to re-check for a free slot while waiting.
_POLL_S = float(os.getenv("AIRA_KEYPOOL_POLL", "0.25"))

# A queued waiter older than this is assumed leaked and dropped.
_WAITER_TTL_S = float(os.getenv("AIRA_KEYPOOL_WAITER_TTL", str(max(300.0, _MAX_WAIT_S + 60.0))))

# ── Redis backend config ─────────────────────────────────────────────────────
REDIS_URL = (os.getenv("REDIS_URL", "") or "").strip()
_BACKEND = (os.getenv("AIRA_KEYPOOL_BACKEND", "auto") or "auto").strip().lower()  # auto|redis|file
# Redis leases are short-lived and kept alive by a heartbeat; a dead pod's lease
# expires within this window and its slot is auto-reclaimed. Must exceed the
# longest single call that runs WITHOUT a heartbeat (sync OpenAI request).
_REDIS_LEASE_TTL_S = float(os.getenv("AIRA_KEYPOOL_REDIS_LEASE_TTL", "90"))
_HEARTBEAT_S = float(os.getenv("AIRA_KEYPOOL_HEARTBEAT", "30"))
_REDIS_RETRY_COOLDOWN_S = float(os.getenv("AIRA_KEYPOOL_REDIS_COOLDOWN", "10"))


def _mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…"


def _key_id(key: str) -> str:
    """Stable short identity of a key — so slots are shared by key IDENTITY, not
    by its position in .env (two envs with the same keys in different order still
    coordinate correctly)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


# ═════════════════════════════════════════════════════════════════════════════
# FILE BACKEND (original, unchanged) — the automatic fallback
# ═════════════════════════════════════════════════════════════════════════════
class _Provider:
    """Holds the key list + limit for one provider and does the locked file I/O."""

    def __init__(self, name: str):
        if name not in _PROVIDERS:
            raise ValueError(f"Unknown provider '{name}'. Known: {sorted(_PROVIDERS)}")
        self.name = name
        prefix, default_limit = _PROVIDERS[name]
        self.prefix = prefix

        raw = os.getenv(f"{prefix}_API_KEY", "") or ""
        self.keys: List[str] = [k.strip() for k in raw.split(",") if k.strip()]
        self.limit = int(os.getenv(f"{prefix}_MAX_CONCURRENCY", str(default_limit)))
        if self.limit < 1:
            self.limit = 1

        os.makedirs(_STATE_DIR, exist_ok=True)
        self.state_path = os.path.join(_STATE_DIR, f"{name}.json")
        self._warned_empty = False

    def _with_lock(self, fn):
        if not os.path.exists(self.state_path):
            try:
                with open(self.state_path, "a"):
                    pass
            except FileExistsError:
                pass
        f = open(self.state_path, "r+")
        try:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                raw = f.read().strip()
                data = json.loads(raw) if raw else {"leases": []}
            except (json.JSONDecodeError, ValueError):
                data = {"leases": []}
            result = fn(data)
            f.seek(0)
            f.truncate()
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
            return result
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False
        except Exception:
            return True

    def _reclaim(self, data: dict) -> None:
        now = time.time()
        live = []
        for lease in data.get("leases", []):
            try:
                if now - float(lease.get("ts", 0)) > _LEASE_TTL_S:
                    continue
                if not self._pid_alive(int(lease.get("pid", -1))):
                    continue
            except Exception:
                continue
            live.append(lease)
        data["leases"] = live

    def _counts(self, data: dict) -> Dict[int, int]:
        counts: Dict[int, int] = {i: 0 for i in range(len(self.keys))}
        for lease in data.get("leases", []):
            idx = int(lease.get("key_index", -1))
            if 0 <= idx < len(self.keys):
                counts[idx] = counts.get(idx, 0) + 1
        return counts

    def _try_acquire(self) -> Optional[Tuple[str, str, int]]:
        def _op(data):
            self._reclaim(data)
            counts = self._counts(data)
            for idx in range(len(self.keys)):
                if counts.get(idx, 0) < self.limit:
                    lease_id = uuid.uuid4().hex
                    data.setdefault("leases", []).append({
                        "lease_id": lease_id, "key_index": idx,
                        "pid": os.getpid(), "ts": time.time(),
                    })
                    return (self.keys[idx], lease_id, idx)
            return None
        return self._with_lock(_op)

    def _force_least_loaded(self) -> Tuple[str, str, int]:
        def _op(data):
            self._reclaim(data)
            counts = self._counts(data)
            idx = min(range(len(self.keys)), key=lambda i: counts.get(i, 0))
            lease_id = uuid.uuid4().hex
            data.setdefault("leases", []).append({
                "lease_id": lease_id, "key_index": idx,
                "pid": os.getpid(), "ts": time.time(),
            })
            return (self.keys[idx], lease_id, idx)
        return self._with_lock(_op)

    def _reclaim_waiters(self, data: dict) -> None:
        now = time.time()
        live = []
        for w in data.get("waiters", []):
            try:
                if now - float(w.get("ts", 0)) > _WAITER_TTL_S:
                    continue
                if not self._pid_alive(int(w.get("pid", -1))):
                    continue
            except Exception:
                continue
            live.append(w)
        data["waiters"] = live

    def _try_acquire_fifo(self, ticket_id: str) -> Optional[Tuple[str, str, int]]:
        def _op(data):
            self._reclaim(data)
            self._reclaim_waiters(data)
            waiters = data.setdefault("waiters", [])
            if not any(w.get("tid") == ticket_id for w in waiters):
                waiters.append({"tid": ticket_id, "pid": os.getpid(), "ts": time.time()})
            if waiters and waiters[0].get("tid") != ticket_id:
                return None
            counts = self._counts(data)
            for idx in range(len(self.keys)):
                if counts.get(idx, 0) < self.limit:
                    data["waiters"] = [w for w in waiters if w.get("tid") != ticket_id]
                    lease_id = uuid.uuid4().hex
                    data.setdefault("leases", []).append({
                        "lease_id": lease_id, "key_index": idx,
                        "pid": os.getpid(), "ts": time.time(),
                    })
                    return (self.keys[idx], lease_id, idx)
            return None
        return self._with_lock(_op)

    def _remove_waiter(self, ticket_id: str) -> None:
        if not ticket_id:
            return
        def _op(data):
            data["waiters"] = [w for w in data.get("waiters", []) if w.get("tid") != ticket_id]
            return None
        try:
            self._with_lock(_op)
        except Exception as e:
            logger.warning("[keypool:%s] remove_waiter failed: %s", self.name, e)

    def release(self, lease_id: str) -> None:
        if not lease_id:
            return
        def _op(data):
            before = data.get("leases", [])
            data["leases"] = [l for l in before if l.get("lease_id") != lease_id]
            return None
        try:
            self._with_lock(_op)
        except Exception as e:
            logger.warning("[keypool:%s] release failed: %s", self.name, e)


# ═════════════════════════════════════════════════════════════════════════════
# REDIS BACKEND (shared, multi-pod safe)
# ═════════════════════════════════════════════════════════════════════════════
# All atomic read-modify-write happens inside these Lua scripts (server-side),
# the Redis equivalent of the file backend's flock'd critical section.
#
# slot key   = kp:{provider}:slot:<key_id>  (sorted set, member=token, score=expiry)
# waiters    = kp:{provider}:waiters        (list, FIFO order)
# waiter hb  = kp:{provider}:hb             (hash, ticket -> last_seen)

_LUA_ACQUIRE = """
local now=tonumber(ARGV[1]); local ttl=tonumber(ARGV[2]); local token=ARGV[3]
for i=1,#KEYS do
  redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now)
  if redis.call('ZCARD',KEYS[i]) < tonumber(ARGV[3+i]) then
    redis.call('ZADD',KEYS[i],now+ttl,token)
    return i-1
  end
end
return -1
"""

_LUA_FORCE = """
local now=tonumber(ARGV[1]); local ttl=tonumber(ARGV[2]); local token=ARGV[3]
local best=1; local bestc=nil
for i=1,#KEYS do
  redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now)
  local c=redis.call('ZCARD',KEYS[i])
  if bestc==nil or c<bestc then bestc=c; best=i end
end
redis.call('ZADD',KEYS[best],now+ttl,token)
return best-1
"""

_LUA_RELEASE = "return redis.call('ZREM',KEYS[1],ARGV[1])"

_LUA_HEARTBEAT = """
return redis.call('ZADD',KEYS[1],'XX','CH',tonumber(ARGV[1])+tonumber(ARGV[2]),ARGV[3])
"""

_LUA_STATUS = """
local now=tonumber(ARGV[1]); local res={}
for i=1,#KEYS do
  redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now)
  res[i]=redis.call('ZCARD',KEYS[i])
end
return res
"""

# KEYS[1]=waiters, KEYS[2]=hb, KEYS[3..]=slots
# ARGV: 1=now 2=ttl 3=token 4=ticket 5=waiter_ttl 6..=per-slot limits
_LUA_FIFO = """
local now=tonumber(ARGV[1]); local ttl=tonumber(ARGV[2]); local token=ARGV[3]
local ticket=ARGV[4]; local wttl=tonumber(ARGV[5])
local W=KEYS[1]; local HB=KEYS[2]
local hb=redis.call('HGETALL',HB)
for j=1,#hb,2 do
  if now - tonumber(hb[j+1]) > wttl then
    redis.call('LREM',W,0,hb[j]); redis.call('HDEL',HB,hb[j])
  end
end
if redis.call('HEXISTS',HB,ticket)==0 then redis.call('RPUSH',W,ticket) end
redis.call('HSET',HB,ticket,now)
if redis.call('LINDEX',W,0) ~= ticket then return -1 end
for i=3,#KEYS do
  redis.call('ZREMRANGEBYSCORE',KEYS[i],'-inf',now)
  if redis.call('ZCARD',KEYS[i]) < tonumber(ARGV[3+i]) then
    redis.call('ZADD',KEYS[i],now+ttl,token)
    redis.call('LREM',W,0,ticket); redis.call('HDEL',HB,ticket)
    return i-3
  end
end
return -1
"""

_LUA_RMWAITER = "redis.call('LREM',KEYS[1],0,ARGV[1]); redis.call('HDEL',KEYS[2],ARGV[1]); return 1"


def _parse_lease(lease_id: str) -> Tuple[int, str]:
    idx_s, token = lease_id.split(".", 1)
    return int(idx_s), token


class _RedisBackend:
    """Shared slot accounting in Redis. Same acquire/release/FIFO semantics as the
    file backend, but coordinated across pods. lease_id format = '<idx>.<token>'."""

    def __init__(self, name: str, keys: List[str], limit: int):
        self.name = name
        self.keys = keys
        self.limit = limit
        tag = f"kp:{{{name}}}"          # {name} = cluster hash-tag (harmless single-node)
        self._slots = [f"{tag}:slot:{_key_id(k)}" for k in keys]
        self._waiters = f"{tag}:waiters"
        self._hb = f"{tag}:hb"
        self._sync = None
        self._async = None
        self._async_loop = None

    def _sc(self):
        if self._sync is None:
            self._sync = _redis_sync.from_url(
                REDIS_URL, decode_responses=True,
                socket_timeout=2, socket_connect_timeout=2, health_check_interval=30,
            )
        return self._sync

    def _ac(self):
        # The async client binds to the event loop that created it. mp.Pool reuses
        # worker processes across interviews (a fresh loop each), so recreate the
        # client whenever the running loop changed — else "Event loop is closed".
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._async is None or self._async_loop is not loop:
            self._async = _redis_async.from_url(
                REDIS_URL, decode_responses=True,
                socket_timeout=2, socket_connect_timeout=2, health_check_interval=30,
            )
            self._async_loop = loop
        return self._async

    def ping(self):
        return self._sc().ping()

    def _limits_argv(self):
        return [self.limit] * len(self.keys)

    def _pack(self, idx, token):
        idx = int(idx)
        if idx < 0:
            return None
        return (self.keys[idx], f"{idx}.{token}", idx)

    # ── first-fit ──
    def try_acquire(self):
        token = uuid.uuid4().hex
        argv = [time.time(), _REDIS_LEASE_TTL_S, token] + self._limits_argv()
        idx = self._sc().eval(_LUA_ACQUIRE, len(self._slots), *self._slots, *argv)
        return self._pack(idx, token)

    async def try_acquire_async(self):
        token = uuid.uuid4().hex
        argv = [time.time(), _REDIS_LEASE_TTL_S, token] + self._limits_argv()
        idx = await self._ac().eval(_LUA_ACQUIRE, len(self._slots), *self._slots, *argv)
        return self._pack(idx, token)

    # ── FIFO ──
    def try_acquire_fifo(self, ticket):
        token = uuid.uuid4().hex
        keys = [self._waiters, self._hb] + self._slots
        argv = [time.time(), _REDIS_LEASE_TTL_S, token, ticket, _WAITER_TTL_S] + self._limits_argv()
        idx = self._sc().eval(_LUA_FIFO, len(keys), *keys, *argv)
        return self._pack(idx, token)

    async def try_acquire_fifo_async(self, ticket):
        token = uuid.uuid4().hex
        keys = [self._waiters, self._hb] + self._slots
        argv = [time.time(), _REDIS_LEASE_TTL_S, token, ticket, _WAITER_TTL_S] + self._limits_argv()
        idx = await self._ac().eval(_LUA_FIFO, len(keys), *keys, *argv)
        return self._pack(idx, token)

    def remove_waiter(self, ticket):
        self._sc().eval(_LUA_RMWAITER, 2, self._waiters, self._hb, ticket)

    # ── safety valve ──
    def force_least_loaded(self):
        token = uuid.uuid4().hex
        idx = self._sc().eval(_LUA_FORCE, len(self._slots), *self._slots, time.time(), _REDIS_LEASE_TTL_S, token)
        return self._pack(idx, token)

    async def force_least_loaded_async(self):
        token = uuid.uuid4().hex
        idx = await self._ac().eval(_LUA_FORCE, len(self._slots), *self._slots, time.time(), _REDIS_LEASE_TTL_S, token)
        return self._pack(idx, token)

    # ── release / heartbeat ──
    def release(self, lease_id):
        idx, token = _parse_lease(lease_id)
        if 0 <= idx < len(self._slots):
            self._sc().eval(_LUA_RELEASE, 1, self._slots[idx], token)

    async def renew_async(self, lease_id):
        idx, token = _parse_lease(lease_id)
        if 0 <= idx < len(self._slots):
            await self._ac().eval(_LUA_HEARTBEAT, 1, self._slots[idx], time.time(), _REDIS_LEASE_TTL_S, token)

    def status_counts(self) -> Dict[int, int]:
        res = self._sc().eval(_LUA_STATUS, len(self._slots), *self._slots, time.time())
        return {i: int(res[i]) for i in range(len(res))}


# ── heartbeat registry (renews long-held Redis leases; per-process) ──────────
_local_leases: Dict[Tuple[str, str], "_PoolProvider"] = {}
_hb_task = None


def _register_lease(provider, lease_id, pool):
    if lease_id and "." in lease_id:
        _local_leases[(provider, lease_id)] = pool


def _unregister_lease(provider, lease_id):
    _local_leases.pop((provider, lease_id), None)


def _ensure_heartbeat():
    global _hb_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _hb_task is None or _hb_task.done():
        _hb_task = loop.create_task(_heartbeat_loop())


async def _heartbeat_loop():
    while True:
        await asyncio.sleep(_HEARTBEAT_S)
        for (provider, lease_id), pool in list(_local_leases.items()):
            rb = getattr(pool, "redis", None)
            if rb is None:
                continue
            try:
                await rb.renew_async(lease_id)
            except Exception:
                pass  # a failed renew just lets the lease TTL-expire; harmless


# ═════════════════════════════════════════════════════════════════════════════
# DISPATCHER — Redis when healthy, file fallback on any Redis error
# ═════════════════════════════════════════════════════════════════════════════
class _PoolProvider:
    def __init__(self, name: str):
        self.file = _Provider(name)     # config (keys/limit/prefix) + fallback
        self.name = name
        self.prefix = self.file.prefix
        self.keys = self.file.keys
        self.limit = self.file.limit
        self.redis: Optional[_RedisBackend] = None
        self._redis_ok = False
        self._redis_next_try = 0.0

        want_redis = (_redis_sync is not None and REDIS_URL and _BACKEND in ("auto", "redis") and self.keys)
        if want_redis:
            try:
                rb = _RedisBackend(name, self.keys, self.limit)
                rb.ping()
                self.redis = rb
                self._redis_ok = True
                logger.info("🔑 [keypool:%s] backend=redis | %d key(s) limit=%d/key (max %d) | %s",
                            name, len(self.keys), self.limit, len(self.keys) * self.limit,
                            REDIS_URL.rsplit("@", 1)[-1])
            except Exception as e:
                logger.error("🚨 [keypool:%s] Redis unavailable at init (%s) → FILE backend", name, e)
                if _BACKEND == "redis":
                    logger.error("[keypool:%s] AIRA_KEYPOOL_BACKEND=redis but Redis is down; using file until it recovers", name)
        elif _BACKEND != "file" and not REDIS_URL:
            logger.info("🔑 [keypool:%s] backend=file (REDIS_URL not set) | %d key(s) limit=%d/key",
                        name, len(self.keys), self.limit)

    # ── redis health gate with cooldown ──
    def _use_redis(self) -> bool:
        if self.redis is None:
            return False
        if self._redis_ok:
            return True
        if time.time() >= self._redis_next_try:
            try:
                self.redis.ping()
                self._redis_ok = True
                logger.info("✅ [keypool:%s] Redis recovered → back on shared backend", self.name)
                return True
            except Exception:
                self._redis_next_try = time.time() + _REDIS_RETRY_COOLDOWN_S
        return False

    def _fail(self, e):
        self._redis_ok = False
        self._redis_next_try = time.time() + _REDIS_RETRY_COOLDOWN_S
        logger.warning("⚠️ [keypool:%s] Redis op failed (%s) → file fallback for now", self.name, e)

    # ── acquire (sync) ──
    def _try_acquire(self):
        if self._use_redis():
            try:
                got = self.redis.try_acquire()
                if got:
                    _register_lease(self.name, got[1], self)
                return got
            except Exception as e:
                self._fail(e)
        return self.file._try_acquire()

    async def _try_acquire_async(self):
        if self._use_redis():
            try:
                got = await self.redis.try_acquire_async()
                if got:
                    _register_lease(self.name, got[1], self)
                    _ensure_heartbeat()
                return got
            except Exception as e:
                self._fail(e)
        return self.file._try_acquire()

    def _try_acquire_fifo(self, tid):
        if self._use_redis():
            try:
                got = self.redis.try_acquire_fifo(tid)
                if got:
                    _register_lease(self.name, got[1], self)
                return got
            except Exception as e:
                self._fail(e)
        return self.file._try_acquire_fifo(tid)

    async def _try_acquire_fifo_async(self, tid):
        if self._use_redis():
            try:
                got = await self.redis.try_acquire_fifo_async(tid)
                if got:
                    _register_lease(self.name, got[1], self)
                    _ensure_heartbeat()
                return got
            except Exception as e:
                self._fail(e)
        return self.file._try_acquire_fifo(tid)

    def _remove_waiter(self, tid):
        if self.redis is not None and self._redis_ok:
            try:
                self.redis.remove_waiter(tid)
                return
            except Exception as e:
                self._fail(e)
        self.file._remove_waiter(tid)

    def _force_least_loaded(self):
        if self._use_redis():
            try:
                got = self.redis.force_least_loaded()
                if got:
                    _register_lease(self.name, got[1], self)
                return got
            except Exception as e:
                self._fail(e)
        return self.file._force_least_loaded()

    async def _force_least_loaded_async(self):
        if self._use_redis():
            try:
                got = await self.redis.force_least_loaded_async()
                if got:
                    _register_lease(self.name, got[1], self)
                    _ensure_heartbeat()
                return got
            except Exception as e:
                self._fail(e)
        return self.file._force_least_loaded()

    def release(self, lease_id):
        # Route by lease_id shape: Redis leases are "<idx>.<token>" (contain a dot);
        # file leases are a bare uuid hex. A Redis lease is always released on Redis
        # (even if Redis is flapping — else it TTL-expires) and never on the file.
        if self.redis is not None and isinstance(lease_id, str) and "." in lease_id:
            _unregister_lease(self.name, lease_id)
            try:
                self.redis.release(lease_id)
            except Exception as e:
                self._fail(e)   # lease will TTL-expire; safe
            return
        self.file.release(lease_id)

    def _counts(self) -> Dict[int, int]:
        if self._use_redis():
            try:
                return self.redis.status_counts()
            except Exception as e:
                self._fail(e)
        return self.file._with_lock(lambda d: (self.file._reclaim(d), self.file._counts(d))[1])

    @property
    def backend(self) -> str:
        return "redis" if (self.redis is not None and self._redis_ok) else "file"


# Cache one _PoolProvider per name within a process.
_cache: Dict[str, _PoolProvider] = {}


def _provider(name: str) -> _PoolProvider:
    name = name.lower()
    p = _cache.get(name)
    if p is None:
        p = _PoolProvider(name)
        _cache[name] = p
        if not p.keys:
            logger.error(
                "🚨 [keypool:%s] No keys configured — set %s_API_KEY (comma-separated). "
                "Calls fall back to a single empty key.", name, p.prefix,
            )
    return p


# ── Manual acquire/release ───────────────────────────────────────────────────
def _saturated_warn(p):
    logger.error(
        "🚨🚨🚨 [keypool:%s] ALL %d KEYS SATURATED (limit=%d each, %d max concurrent) "
        "— WAITING for a free slot…", p.name, len(p.keys), p.limit, len(p.keys) * p.limit,
    )


def _safety_valve(p):
    logger.error("🚨 [keypool:%s] still saturated after %.0fs — proceeding OVER LIMIT "
                 "on least-loaded key (safety valve)", p.name, _MAX_WAIT_S)
    return p._force_least_loaded()


async def _safety_valve_async(p):
    logger.error("🚨 [keypool:%s] still saturated after %.0fs — proceeding OVER LIMIT "
                 "on least-loaded key (safety valve)", p.name, _MAX_WAIT_S)
    return await p._force_least_loaded_async()


def acquire_key(provider: str) -> Tuple[str, Optional[str]]:
    """Blocking acquire for SYNC call sites. Returns (key, lease_id).
    Always pair with release_key(provider, lease_id) in a finally."""
    p = _provider(provider)
    if not p.keys:
        return os.getenv(f"{p.prefix}_API_KEY", ""), None
    start = time.monotonic()
    warned = False
    while True:
        got = p._try_acquire()
        if got:
            return got[0], got[1]
        if not warned:
            warned = True
            _saturated_warn(p)
        if _MAX_WAIT_S > 0 and (time.monotonic() - start) > _MAX_WAIT_S:
            key, lease_id, _ = _safety_valve(p)
            return key, lease_id
        time.sleep(_POLL_S)


async def acquire_key_async(provider: str, *, on_wait=None) -> Tuple[str, Optional[str]]:
    """Blocking acquire for ASYNC call sites. Returns (key, lease_id).

    on_wait: optional async callable. None (OpenAI/Deepgram) → plain first-fit poll.
    Provided (Cartesia/TTS) → FIFO-fair, awaiting on_wait between re-checks (fillers)."""
    p = _provider(provider)
    if not p.keys:
        return os.getenv(f"{p.prefix}_API_KEY", ""), None

    # ── Default path: first-fit poll (OpenAI / Deepgram) ──
    if on_wait is None:
        start = time.monotonic()
        warned = False
        while True:
            got = await p._try_acquire_async()
            if got:
                return got[0], got[1]
            if not warned:
                warned = True
                _saturated_warn(p)
            if _MAX_WAIT_S > 0 and (time.monotonic() - start) > _MAX_WAIT_S:
                key, lease_id, _ = await _safety_valve_async(p)
                return key, lease_id
            await asyncio.sleep(_POLL_S)

    # ── FIFO-fair path with filler (Cartesia) ──
    ticket_id = uuid.uuid4().hex
    start = time.monotonic()
    warned = False
    attempt = 0
    try:
        while True:
            got = await p._try_acquire_fifo_async(ticket_id)
            if got:
                return got[0], got[1]
            if not warned:
                warned = True
                _saturated_warn(p)
            if _MAX_WAIT_S > 0 and (time.monotonic() - start) > _MAX_WAIT_S:
                key, lease_id, _ = await _safety_valve_async(p)
                return key, lease_id
            played = False
            try:
                try:
                    played = await on_wait(attempt)
                except TypeError:
                    played = await on_wait()
            except Exception as e:
                logger.warning("[keypool:%s] on_wait (filler) failed: %s", p.name, e)
            if played:
                attempt += 1
            else:
                await asyncio.sleep(_POLL_S)
    finally:
        p._remove_waiter(ticket_id)


def release_key(provider: str, lease_id: Optional[str]) -> None:
    """Release a previously-acquired key slot. Safe to call with None."""
    if lease_id is None:
        return
    _provider(provider).release(lease_id)


# ── Context managers ─────────────────────────────────────────────────────────
@contextlib.contextmanager
def acquire(provider: str):
    """Blocking acquire for synchronous call sites (e.g. OpenAI requests)."""
    key, lease_id = acquire_key(provider)
    try:
        yield key
    finally:
        release_key(provider, lease_id)


@contextlib.asynccontextmanager
async def acquire_async(provider: str):
    """Blocking acquire for async call sites (Cartesia/Deepgram streaming)."""
    key, lease_id = await acquire_key_async(provider)
    try:
        yield key
    finally:
        release_key(provider, lease_id)


def status(provider: str) -> dict:
    """Snapshot of current in-use counts per key (for debugging/monitoring)."""
    p = _provider(provider)
    counts = p._counts()
    return {
        "provider": provider,
        "backend": p.backend,
        "limit_per_key": p.limit,
        "keys": [
            {"index": i, "key": _mask(p.keys[i]), "in_use": counts.get(i, 0)}
            for i in range(len(p.keys))
        ],
        "total_in_use": sum(counts.values()),
        "total_capacity": len(p.keys) * p.limit,
    }
