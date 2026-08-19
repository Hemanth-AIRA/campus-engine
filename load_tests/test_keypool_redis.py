#!/usr/bin/env python3
"""
COMPLETE test suite for the Redis key-pool (api_key_pool.py) — deployment gate.

Runs against a REAL Redis, isolated on DB /15 with throwaway test keys, so it can
never touch prod counts. Usage:

    REDIS_URL='redis://:<PW>@134.195.138.72:6380/15' \
    conda run -n aira_Env python -m load_tests.test_keypool_redis

Covers: backend selection, first-fit ordering, release, status accuracy,
cross-PROCESS cap (the core invariant), FIFO fairness, TTL crash-recovery,
heartbeat keep-alive, key-IDENTITY sharing across differing key order,
safety valve, and automatic file-fallback when Redis is unreachable.
"""
import os, sys, time, subprocess

# ── test env (must be set BEFORE importing api_key_pool) ──────────────────────
if not os.environ.get("REDIS_URL"):
    print("Set REDIS_URL (use DB /15), e.g. redis://:<PW>@134.195.138.72:6380/15"); sys.exit(2)
os.environ["AIRA_KEYPOOL_BACKEND"] = "redis"
os.environ["CARTESIA_API_KEY"] = "kptest_A,kptest_B"     # test keys → own slots
os.environ["CARTESIA_MAX_CONCURRENCY"] = "1"             # cap = 2 keys × 1 = 2
os.environ["AIRA_KEYPOOL_MAX_WAIT"] = "30"               # extras WAIT (don't safety-valve mid-test)
os.environ["AIRA_KEYPOOL_REDIS_LEASE_TTL"] = "4"
os.environ["AIRA_KEYPOOL_HEARTBEAT"] = "1"
os.environ["AIRA_KEYPOOL_POLL"] = "0.1"

import asyncio
import multiprocessing as mp
import api_key_pool as kp

PROV, CAP = "cartesia", 2
_results = []


def check(name, cond, detail=""):
    _results.append(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail else ""))


def _redis():
    import redis
    return redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


def cleanup():
    try:
        r = _redis()
        for k in r.scan_iter("kp:{cartesia}:*"):
            r.delete(k)
    except Exception as e:
        print("  (cleanup warning:", e, ")")
    kp._local_leases.clear()


def _leaseidx(lease):
    return int(lease.split(".", 1)[0])


# ── worker for the multiprocess cap test (top-level so spawn can pickle it) ──
def _hold(hold_s):
    import api_key_pool as k
    _key, lease = k.acquire_key(PROV)   # contend immediately (staggered spawn gives concurrency)
    time.sleep(hold_s)
    k.release_key(PROV, lease)


def t_backend():
    check("backend == redis", kp.status(PROV)["backend"] == "redis", kp.status(PROV)["backend"])


def t_first_fit():
    cleanup()
    _k0, l0 = kp.acquire_key(PROV)
    _k1, l1 = kp.acquire_key(PROV)
    check("first-fit fills key 0 then key 1", _leaseidx(l0) == 0 and _leaseidx(l1) == 1,
          f"idx {_leaseidx(l0)}, {_leaseidx(l1)}")
    kp.release_key(PROV, l0); kp.release_key(PROV, l1)


def t_release_status():
    cleanup()
    _a = kp.acquire_key(PROV); _b = kp.acquire_key(PROV)
    at2 = kp.status(PROV)["total_in_use"]
    kp.release_key(PROV, _a[1]); at1 = kp.status(PROV)["total_in_use"]
    kp.release_key(PROV, _b[1]); at0 = kp.status(PROV)["total_in_use"]
    check("status/release accounting (2→1→0)", (at2, at1, at0) == (2, 1, 0), f"{at2},{at1},{at0}")


def t_cap_multiprocess():
    cleanup()
    n = 6
    procs = [mp.Process(target=_hold, args=(1.5,)) for _ in range(n)]
    for p in procs:
        p.start(); time.sleep(0.05)
    peak = 0; t0 = time.time()
    while any(p.is_alive() for p in procs) and time.time() - t0 < 40:
        peak = max(peak, kp.status(PROV)["total_in_use"]); time.sleep(0.03)
    for p in procs:
        p.join(timeout=5)
    time.sleep(0.3)
    drained = kp.status(PROV)["total_in_use"]
    check(f"cross-process cap ({n} procs never exceed {CAP})", peak <= CAP and peak >= 1 and drained == 0,
          f"peak={peak}, drained={drained}")


def t_fifo():
    cleanup()
    order = []
    leases = {}

    async def run():
        h1 = await kp.acquire_key_async(PROV)          # saturate both slots
        h2 = await kp.acquire_key_async(PROV)

        async def waiter(i):
            async def on_wait(a=0):
                await asyncio.sleep(0.05); return True
            _k, l = await kp.acquire_key_async(PROV, on_wait=on_wait)
            order.append(i); leases[i] = l

        tasks = []
        for i in range(3):                             # enqueue in strict order 0,1,2
            tasks.append(asyncio.create_task(waiter(i)))
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.3)
        assert order == [], f"served while saturated: {order}"
        # free ONE slot at a time → the queue head must be the one served each time
        kp.release_key(PROV, h1[1]);       await asyncio.sleep(0.5)
        assert order == [0], f"after 1st free: {order}"
        kp.release_key(PROV, h2[1]);       await asyncio.sleep(0.5)
        assert order == [0, 1], f"after 2nd free: {order}"
        kp.release_key(PROV, leases[0]);   await asyncio.gather(*tasks)
        for l in leases.values():
            kp.release_key(PROV, l)
    asyncio.run(run())
    check("FIFO — head served on each single freed slot", order == [0, 1, 2], f"order={order}")


def t_ttl_expiry():
    cleanup()
    _k, _l = kp.acquire_key(PROV)              # SYNC acquire, never released, no heartbeat
    before = kp.status(PROV)["total_in_use"]
    time.sleep(float(os.environ["AIRA_KEYPOOL_REDIS_LEASE_TTL"]) + 2.0)
    after = kp.status(PROV)["total_in_use"]
    check("TTL crash-recovery (dead lease reclaimed)", before >= 1 and after == 0, f"{before}→{after}")
    kp._local_leases.clear()


def t_heartbeat():
    cleanup()

    async def run():
        _k, l = await kp.acquire_key_async(PROV)     # async → heartbeat task renews it
        await asyncio.sleep(float(os.environ["AIRA_KEYPOOL_REDIS_LEASE_TTL"]) + 2.5)
        alive = kp.status(PROV)["total_in_use"]
        kp.release_key(PROV, l)
        return alive
    alive = asyncio.run(run())
    check("heartbeat keeps a long lease alive past TTL", alive == 1, f"in_use after >TTL = {alive}")


def t_identity_sharing():
    cleanup()
    # b1 has both keys; b2 has ONLY key A (different set/order). If they share by
    # IDENTITY, b1 taking A must make b2 (which only has A) see it as full.
    b1 = kp._RedisBackend("cartesia", ["kptest_A", "kptest_B"], 1)
    b2 = kp._RedisBackend("cartesia", ["kptest_A"], 1)
    g1 = b1.try_acquire()                                   # b1 first-fit → key A (idx0)
    took_A = kp._key_id(b1.keys[_leaseidx(g1[1])]) == kp._key_id("kptest_A")
    g2 = b2.try_acquire()                                   # A full via shared identity → None
    blocked = g2 is None
    b1.release(g1[1])                                       # free A
    g3 = b2.try_acquire()                                   # now b2 can take A
    freed = g3 is not None
    if g3: b2.release(g3[1])
    check("key-IDENTITY sharing (differing key sets/order)", took_A and blocked and freed,
          f"took_A={took_A} blocked_when_full={blocked} freed_after_release={freed}")


def t_safety_valve():
    cleanup()
    p = kp._provider(PROV)
    a = kp.acquire_key(PROV); b = kp.acquire_key(PROV)     # saturate cap=2
    over = p._force_least_loaded()                          # safety valve grants OVER limit
    now = kp.status(PROV)["total_in_use"]
    check("safety valve grants over-limit when saturated", over is not None and now == CAP + 1,
          f"in_use={now} (cap {CAP})")
    kp.release_key(PROV, a[1]); kp.release_key(PROV, b[1])
    if over: p.release(over[1])


def t_fallback_subprocess():
    # child points at an unreachable Redis → must fall back to file backend and still work
    env = dict(os.environ)
    env["REDIS_URL"] = "redis://:x@127.0.0.1:1/0"     # nothing there
    env["AIRA_KEYPOOL_DIR"] = "/tmp/kp_fallback_test"
    code = (
        "import os,api_key_pool as k;"
        "st=k.status('cartesia');"
        "kk,l=k.acquire_key('cartesia');"
        "ok=(st['backend']=='file' and l is not None);"
        "k.release_key('cartesia',l);"
        "print('FALLBACK_OK' if ok else 'FALLBACK_FAIL', st['backend'])"
    )
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    ok = "FALLBACK_OK" in out.stdout
    check("auto-fallback to file when Redis unreachable", ok, out.stdout.strip() or out.stderr.strip()[:80])


if __name__ == "__main__":
    print("=== Redis key-pool — complete suite ===")
    print(f"endpoint {os.environ['REDIS_URL'].rsplit('@',1)[-1]} | keys=2 magic=1 cap={CAP}\n")
    for fn in (t_backend, t_first_fit, t_release_status, t_cap_multiprocess, t_fifo,
               t_ttl_expiry, t_heartbeat, t_identity_sharing, t_safety_valve, t_fallback_subprocess):
        try:
            fn()
        except Exception as e:
            import traceback; traceback.print_exc()
            check(fn.__name__ + " (raised)", False, str(e)[:80])
    cleanup()
    passed = sum(_results); total = len(_results)
    print(f"\n{'🎉' if passed==total else '⚠️'}  {passed}/{total} checks passed")
    sys.exit(0 if passed == total else 1)
