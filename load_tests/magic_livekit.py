"""
magic_livekit.py  —  Find the magic number for LiveKit concurrent room joins.

Tests three metrics on the ladder:
  - connect : room.connect() handshake time
  - publish : publish audio track after connect
  - e2e     : full flow: token + connect + publish + stay 5s + disconnect

Run:
    python -m load_tests.magic_livekit
    python -m load_tests.magic_livekit --metric connect
    python -m load_tests.magic_livekit --metric publish
    python -m load_tests.magic_livekit --steps 1 2 5 10 15 20 25 30
"""
import asyncio, argparse, logging, time, uuid

from livekit import rtc
from livekit.api import AccessToken, VideoGrants

from .config      import (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
                           AUDIO_SAMPLE_RATE, AUDIO_CHANNELS, REPORTS_DIR,
                           cleanup_previous_outputs)
from .audio_gen   import stream_speech_like_to_source
from .ladder_core import LadderBenchmark

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [LK-MAGIC] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "magic_livekit.log", mode="w"),
    ]
)
logger = logging.getLogger("magic_livekit")

STAY_S = 5.0   # how long to stay in room after publishing (for e2e test)


def make_token(room_name: str, identity: str) -> str:
    """Generate a LiveKit JWT locally (no Connect server needed)."""
    token = AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    token.with_identity(identity)
    token.with_name(identity)
    token.with_grants(VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
    ))
    return token.to_jwt()


# ── Metric callables ───────────────────────────────────────────────────

async def measure_connect(call_id: int) -> float:
    """Measure room.connect() handshake time only."""
    room_id  = f"magic-lk-{uuid.uuid4().hex[:8]}"
    identity = f"sim-{call_id:03d}-{uuid.uuid4().hex[:4]}"
    token    = make_token(room_id, identity)
    room     = rtc.Room()

    t0 = time.perf_counter()
    try:
        await room.connect(LIVEKIT_URL, token)
        ms = (time.perf_counter() - t0) * 1000
        logger.debug("[call-%03d] Connect %.0fms | room=%s", call_id, ms, room_id)
        return ms
    finally:
        try:
            await room.disconnect()
        except Exception:
            pass


async def measure_publish(call_id: int) -> float:
    """Measure connect + audio track publish time."""
    room_id  = f"magic-lk-{uuid.uuid4().hex[:8]}"
    identity = f"sim-{call_id:03d}-{uuid.uuid4().hex[:4]}"
    token    = make_token(room_id, identity)
    room     = rtc.Room()

    t0 = time.perf_counter()
    try:
        await room.connect(LIVEKIT_URL, token)
        connect_ms = (time.perf_counter() - t0) * 1000

        t1  = time.perf_counter()
        src = rtc.AudioSource(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        trk = rtc.LocalAudioTrack.create_audio_track("mic", src)
        await room.local_participant.publish_track(trk)
        publish_ms = (time.perf_counter() - t1) * 1000

        total_ms = (time.perf_counter() - t0) * 1000
        logger.debug("[call-%03d] Connect %.0fms | Publish %.0fms | Total %.0fms",
                     call_id, connect_ms, publish_ms, total_ms)
        return total_ms

    finally:
        try:
            await room.disconnect()
        except Exception:
            pass


async def measure_e2e(call_id: int) -> float:
    """
    Full candidate flow: connect + publish + stream audio for STAY_S seconds + disconnect.
    Measures total wall time. Simulates what a real candidate participant does.
    """
    room_id  = f"magic-lk-{uuid.uuid4().hex[:8]}"
    identity = f"sim-{call_id:03d}-{uuid.uuid4().hex[:4]}"
    token    = make_token(room_id, identity)
    room     = rtc.Room()

    t0 = time.perf_counter()
    try:
        await room.connect(LIVEKIT_URL, token)
        connect_ms = (time.perf_counter() - t0) * 1000

        src = rtc.AudioSource(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        trk = rtc.LocalAudioTrack.create_audio_track("mic", src)
        await room.local_participant.publish_track(trk)
        publish_ms = (time.perf_counter() - t0) * 1000

        # Stream audio for STAY_S seconds
        await stream_speech_like_to_source(src, duration_s=STAY_S)

        total_ms = (time.perf_counter() - t0) * 1000
        logger.debug("[call-%03d] connect=%.0fms | publish=%.0fms | e2e=%.0fms",
                     call_id, connect_ms, publish_ms, total_ms)
        return total_ms

    finally:
        try:
            await room.disconnect()
        except Exception:
            pass


METRIC_FNS = {
    "connect": measure_connect,
    "publish": measure_publish,
    "e2e"    : measure_e2e,
}

METRIC_LABELS = {
    "connect": "LiveKit Room Connect (handshake)",
    "publish": "LiveKit Connect + Track Publish",
    "e2e"    : f"LiveKit E2E ({STAY_S}s audio session)",
}


async def run(steps: list, metric: str):
    fn    = METRIC_FNS[metric]
    name  = METRIC_LABELS[metric]
    bench = LadderBenchmark(name=name, steps=steps, cooldown_s=2.0)

    logger.info("=" * 60)
    logger.info("  %s — Magic Number Benchmark", name)
    logger.info("  LiveKit URL : %s", LIVEKIT_URL)
    logger.info("  Metric      : %s", metric)
    logger.info("  Steps       : %s", steps)
    logger.info("=" * 60)

    await bench.run(fn)

    print(f"\n{'='*60}")
    print(f"  🎯 MAGIC NUMBER : {bench.magic_number} concurrent LiveKit rooms")
    print(f"  ⚠️  WARN NUMBER  : {bench.warn_number}")
    if bench.baseline:
        print(f"  Baseline p95   : {bench.baseline.p95:.0f}ms")
        print(f"  Max tested p95 : {max(r.p95 for r in bench.results):.0f}ms")
        print(f"  Ratio at peak  : {max(r.p95 for r in bench.results)/bench.baseline.p95:.2f}×")
    print(f"{'='*60}")
    print()
    print(bench.ascii_chart())

    extra = f"""
## Operational Recommendation

```python
# In your orchestration / key manager:
MAX_CONCURRENT_LIVEKIT_ROOMS = {bench.magic_number}

# LiveKit Cloud handles the SFU scaling automatically,
# but your bot participants (Python) are the bottleneck —
# each bot opens: 1 Room, 1 AudioSource, 1-2 async tasks.
# Monitor asyncio task count: len(asyncio.all_tasks())
```

## What LiveKit Bottlenecks Look Like

| Symptom | Cause | Fix |
|---------|-------|-----|
| connect p95 > 3s | SFU cold room | Pre-warm rooms before interviews |
| publish fails | DTLS timeout | Retry with backoff |
| audio drop mid-interview | SFU packet loss | Use LiveKit Cloud (managed) |
| n=50 all green, n=100 degraded | Bot Python event loop lag | Use multiprocess per interview |

## SFU vs Bot Process Limits

LiveKit Cloud SFU can handle hundreds of rooms.
YOUR BOTTLENECK is the Python bot process:
- Each interview needs ~2-4 async tasks
- 50 interviews = 100-200 tasks in one event loop
- Monitor with: `len(asyncio.all_tasks())`
- Degrade point estimated at ~{bench.magic_number * 3} interviews per Python process
"""
    path = bench.save_report(REPORTS_DIR, extra_notes=extra)
    logger.info("Report: %s", path)
    return bench


def main():
    ap = argparse.ArgumentParser(description="LiveKit magic number finder")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--steps",  nargs="+", type=int,
                    default=[1, 2, 5, 10, 15, 20, 25, 30],
                    help="Concurrency ladder steps")
    ap.add_argument("--metric", choices=["connect", "publish", "e2e", "all"],
                    default="publish", help="What to measure")
    args = ap.parse_args()

    async def _main():
        if args.metric == "all":
            for m in ["connect", "publish", "e2e"]:
                await run(args.steps, m)
                await asyncio.sleep(3)
        else:
            await run(args.steps, args.metric)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
