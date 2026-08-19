"""
magic_cartesia.py  —  Find the magic number for Cartesia TTS concurrent requests.

Measures TTFA (time-to-first-audio-byte) and total synthesis time at
increasing concurrency levels.

Run:
    python -m load_tests.magic_cartesia
    python -m load_tests.magic_cartesia --metric total
    python -m load_tests.magic_cartesia --steps 1 2 5 10 15 20 25
"""
import asyncio, argparse, logging, time
import cartesia

from .config      import (CARTESIA_API_KEY, CARTESIA_VOICE_ID,
                           CARTESIA_MODEL, REPORTS_DIR, cleanup_previous_outputs)
from .ladder_core import LadderBenchmark

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [TTS-MAGIC] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "magic_cartesia.log", mode="w"),
    ]
)
logger = logging.getLogger("magic_cartesia")

# Varied texts to avoid caching effects on the TTS side
TEST_TEXTS = [
    "Please tell me about your experience with system design and distributed architectures.",
    "Can you walk me through how you would design a URL shortening service?",
    "What is your approach to handling race conditions in concurrent systems?",
    "Describe a challenging technical problem you solved and what you learned from it.",
    "How do you ensure code quality when working under tight deadlines?",
    "What strategies do you use to debug production issues in a live system?",
    "Explain the differences between SQL and NoSQL databases and when you'd use each.",
    "How would you design a rate limiter for a high-traffic API?",
    "What is your experience with container orchestration and Kubernetes?",
    "Describe how you approach performance optimization in a backend service.",
]


def make_fn(metric: str):
    """Return async callable measuring either TTFA or total synthesis time."""
    async def _fn(call_id: int) -> float:
        text   = TEST_TEXTS[call_id % len(TEST_TEXTS)]
        client = cartesia.AsyncCartesia(api_key=CARTESIA_API_KEY)
        t_start = time.perf_counter()
        ttfa_ms = None
        total_bytes = 0

        try:
            async with client.tts.generate_sse(
                model_id    = CARTESIA_MODEL,
                transcript  = text,
                voice       = {"id": CARTESIA_VOICE_ID},
                output_format = {
                    "container"   : "raw",
                    "encoding"    : "pcm_f32le",
                    "sample_rate" : 22050,
                },
            ) as response:
                async for chunk in response:
                    audio = getattr(chunk, "audio", None)
                    if audio:
                        if ttfa_ms is None:
                            ttfa_ms = (time.perf_counter() - t_start) * 1000
                            logger.debug("[call-%03d] TTFA %.0fms", call_id, ttfa_ms)
                        total_bytes += len(audio)

            total_ms = (time.perf_counter() - t_start) * 1000

            if ttfa_ms is None:
                raise RuntimeError("No audio bytes received from Cartesia")

            logger.debug("[call-%03d] total=%.0fms | bytes=%d", call_id, total_ms, total_bytes)
            return ttfa_ms if metric == "ttfa" else total_ms

        finally:
            await client.close()

    return _fn


async def run(steps: list, metric: str):
    label = "TTFA" if metric == "ttfa" else "Total Synthesis"
    name  = f"Cartesia TTS {label}"
    fn    = make_fn(metric)

    bench = LadderBenchmark(name=name, steps=steps, cooldown_s=3.0)

    logger.info("=" * 60)
    logger.info("  %s — Magic Number Benchmark", name)
    logger.info("  Metric: %s | Steps: %s", metric, steps)
    logger.info("=" * 60)

    await bench.run(fn)

    print(f"\n{'='*60}")
    print(f"  🎯 MAGIC NUMBER: {bench.magic_number} concurrent TTS requests")
    print(f"  ⚠️  WARN  NUMBER: {bench.warn_number}")
    print(f"  Baseline p95 {label}: {bench.baseline.p95:.0f}ms")
    print(f"{'='*60}")
    print(bench.ascii_chart())

    extra = f"""
## Operational Recommendation

```python
# In your bot TTS handler:
MAX_CONCURRENT_TTS = {bench.magic_number}   # Cartesia magic number

tts_sem = asyncio.Semaphore({bench.magic_number})
async with tts_sem:
    audio = await speak(text)
```

## Impact on Candidate Experience

| Concurrent TTS | TTFA (expected) | Candidate Perception |
|----------------|-----------------|----------------------|
| 1–{bench.warn_number} | ~{bench.baseline.p95:.0f}ms | Instant — imperceptible |
| {bench.warn_number}–{bench.magic_number} | ~{bench.baseline.p95*1.5:.0f}ms | Slight pause — acceptable |
| >{bench.magic_number} | >{bench.baseline.p95*2:.0f}ms | Noticeable lag — degrades UX |

## Multi-Key Strategy

If you have 2 Cartesia API keys, effective capacity doubles:
```
Key A: {bench.magic_number} concurrent streams
Key B: {bench.magic_number} concurrent streams
Total: {bench.magic_number * 2} concurrent streams
```
"""
    path = bench.save_report(REPORTS_DIR, extra_notes=extra)
    logger.info("Report: %s", path)
    return bench


def main():
    ap = argparse.ArgumentParser(description="Cartesia TTS magic number finder")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--steps",  nargs="+", type=int,
                    default=[1, 2, 5, 10, 15, 20, 25, 30, 40],
                    help="Concurrency ladder steps")
    ap.add_argument("--metric", choices=["ttfa", "total", "both"],
                    default="ttfa", help="ttfa or total synthesis time")
    args = ap.parse_args()

    async def _main():
        if args.metric == "both":
            await run(args.steps, "ttfa")
            await asyncio.sleep(5)
            await run(args.steps, "total")
        else:
            await run(args.steps, args.metric)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
