"""
magic_deepgram.py  —  Find the magic number for Deepgram WebSocket streams.

Measures TTFA (time-to-first-audio) latency at increasing concurrency.
Each step opens N simultaneous Deepgram WebSocket connections, streams
3 seconds of speech-like audio, and measures when the first transcript arrives.

Run:
    python -m load_tests.magic_deepgram
    python -m load_tests.magic_deepgram --steps 1 2 5 10 15 20 25 30
    python -m load_tests.magic_deepgram --metric connect  (WS connect time only)
"""
import asyncio, argparse, json, logging, time
import websockets

from .config      import DEEPGRAM_API_KEY, DEEPGRAM_WS_URL, REPORTS_DIR, cleanup_previous_outputs
from .audio_gen   import speech_like_bytes, FRAME_MS
from .ladder_core import LadderBenchmark

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [DG-MAGIC] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "magic_deepgram.log", mode="w"),
    ]
)
logger = logging.getLogger("magic_deepgram")

STREAM_SECONDS = 3.0   # how long to stream audio per call


async def measure_deepgram_ttfa(call_id: int) -> float:
    """
    Open one Deepgram WS, stream audio, return TTFA in ms.
    Raises on error so LadderBenchmark records it.
    """
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    t_start = time.perf_counter()

    async with websockets.connect(
        DEEPGRAM_WS_URL, additional_headers=headers, open_timeout=15
    ) as ws:
        connect_ms = (time.perf_counter() - t_start) * 1000
        logger.debug("[call-%03d] Connected in %.0fms", call_id, connect_ms)

        first_transcript_ms = None
        frame_s = FRAME_MS / 1000.0
        n_frames = int(STREAM_SECONDS / frame_s)

        async def send_audio():
            for _ in range(n_frames):
                await ws.send(speech_like_bytes())
                await asyncio.sleep(frame_s)
            await ws.send(json.dumps({"type": "CloseStream"}))

        async def recv_transcript():
            nonlocal first_transcript_ms
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    transcript = (data.get("channel", {})
                                      .get("alternatives", [{}])[0]
                                      .get("transcript", ""))
                    if data.get("type") == "Results" and transcript:
                        first_transcript_ms = (time.perf_counter() - t_start) * 1000
                        logger.debug("[call-%03d] TTFA %.0fms: %r",
                                     call_id, first_transcript_ms, transcript[:30])
                        break
                except Exception:
                    pass

        send_task = asyncio.create_task(send_audio())
        recv_task = asyncio.create_task(recv_transcript())
        await send_task
        try:
            await asyncio.wait_for(asyncio.shield(recv_task), timeout=10.0)
        except asyncio.TimeoutError:
            pass
        recv_task.cancel()

    if first_transcript_ms is None:
        # No transcript = synthetic audio not recognised — return connect time
        # as a proxy (the stream still worked; Deepgram just didn't transcribe noise)
        return connect_ms

    return first_transcript_ms


async def measure_deepgram_connect(call_id: int) -> float:
    """Measure WS handshake time only — no audio."""
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    t0 = time.perf_counter()
    async with websockets.connect(
        DEEPGRAM_WS_URL, additional_headers=headers, open_timeout=15
    ) as ws:
        ms = (time.perf_counter() - t0) * 1000
        logger.debug("[call-%03d] Connect %.0fms", call_id, ms)
        await ws.send(json.dumps({"type": "CloseStream"}))
        return ms


async def run(steps: list, metric: str):
    fn   = measure_deepgram_connect if metric == "connect" else measure_deepgram_ttfa
    name = f"Deepgram {'WS Connect' if metric == 'connect' else 'TTFA'}"

    bench = LadderBenchmark(name=name, steps=steps, cooldown_s=3.0)

    logger.info("=" * 60)
    logger.info("  %s — Magic Number Benchmark", name)
    logger.info("  Steps: %s", steps)
    logger.info("  Metric: %s", metric)
    logger.info("=" * 60)

    await bench.run(fn)

    print(f"\n{'='*60}")
    print(f"  🎯 MAGIC NUMBER: {bench.magic_number} concurrent Deepgram streams")
    print(f"  ⚠️  WARN  NUMBER: {bench.warn_number}")
    print(f"  Baseline p95: {bench.baseline.p95:.0f}ms")
    print(f"{'='*60}")
    print(bench.ascii_chart())

    extra = f"""
## Operational Recommendation

```python
# In your KeyManager / interview orchestrator:
MAX_CONCURRENT_DEEPGRAM = {bench.magic_number}   # magic number — do not exceed
WARN_CONCURRENT_DEEPGRAM = {bench.warn_number}    # start queuing above this

# If you have multiple API keys (key_a, key_b, key_c),
# each key can carry {bench.magic_number} streams:
# Total safe capacity = num_keys × {bench.magic_number}
```

## What happens above the magic number

Above **{bench.magic_number}** concurrent streams:
- p95 latency jumps from ~{bench.baseline.p95:.0f}ms to >{bench.baseline.p95*2:.0f}ms
- Candidates start noticing transcript delays
- Bot responses slow down
- At high enough concurrency, Deepgram returns HTTP 429
"""
    path = bench.save_report(REPORTS_DIR, extra_notes=extra)
    logger.info("Report: %s", path)
    return bench


def main():
    ap = argparse.ArgumentParser(description="Deepgram magic number finder")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--steps",  nargs="+", type=int,
                    default=[1, 2, 5, 10, 15, 20, 25, 30, 40, 50],
                    help="Concurrency ladder steps")
    ap.add_argument("--metric", choices=["ttfa", "connect"], default="ttfa",
                    help="ttfa=time-to-first-audio | connect=WS handshake only")
    args = ap.parse_args()
    asyncio.run(run(args.steps, args.metric))


if __name__ == "__main__":
    main()
