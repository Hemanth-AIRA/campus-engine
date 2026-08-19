"""
test_03_tts.py  —  Cartesia TTS concurrent request test.

Tests N simultaneous TTS requests and measures:
  - TTFA: time to first audio byte (perceived latency)
  - Total duration: full synthesis time
  - Success rate

Run:
    python -m load_tests.test_03_tts --n 20
"""
import asyncio, argparse, logging, time
import cartesia

from .config  import CARTESIA_API_KEY, CARTESIA_VOICE_ID, CARTESIA_MODEL, REPORTS_DIR, cleanup_previous_outputs
from .metrics import MetricCollector

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [TTS-TEST] %(message)s")
logger = logging.getLogger("test_tts")

TEST_TEXTS = [
    "Tell me about your experience with distributed systems and how you've handled failures.",
    "Can you walk me through a time you had to optimize a slow database query?",
    "What is your approach to code review and ensuring code quality in a team?",
    "Explain the CAP theorem and give an example of when you've had to make trade-offs.",
    "How do you handle disagreements with your team about technical decisions?",
]


async def one_tts_request(session_id: int, text: str,
                          col_ttfa: MetricCollector,
                          col_total: MetricCollector):
    label  = f"session-{session_id:03d}"
    client = cartesia.AsyncCartesia(api_key=CARTESIA_API_KEY)

    t_start = time.perf_counter()
    ttfa_recorded = False
    total_bytes   = 0

    try:
        async with client.tts.sse(
            model_id=CARTESIA_MODEL,
            transcript=text,
            voice={"id": CARTESIA_VOICE_ID},
            output_format={
                "container": "raw",
                "encoding":  "pcm_f32le",
                "sample_rate": 22050,
            },
        ) as response:
            async for chunk in response:
                chunk_bytes = getattr(chunk, "audio", None)
                if chunk_bytes:
                    if not ttfa_recorded:
                        ttfa_ms = (time.perf_counter() - t_start) * 1000
                        col_ttfa.record(label, ttfa_ms, True)
                        logger.info("[%s] TTFA: %.0fms", label, ttfa_ms)
                        ttfa_recorded = True
                    total_bytes += len(chunk_bytes)

        total_ms = (time.perf_counter() - t_start) * 1000
        if ttfa_recorded:
            col_total.record(label, total_ms, True, bytes=total_bytes)
            logger.info("[%s] Total: %.0fms | %d bytes", label, total_ms, total_bytes)
        else:
            col_ttfa.record(label, total_ms, False, "No audio bytes received")
            col_total.record(label, total_ms, False, "No audio bytes received")
            logger.warning("[%s] No audio received", label)

    except Exception as e:
        ms = (time.perf_counter() - t_start) * 1000
        col_ttfa.record(label, ms, False, str(e))
        col_total.record(label, ms, False, str(e))
        logger.error("[%s] ❌ %s", label, e)
    finally:
        await client.close()


async def run(n: int):
    col_ttfa  = MetricCollector(f"Cartesia TTS — TTFA (n={n})")
    col_total = MetricCollector(f"Cartesia TTS — Total (n={n})")

    logger.info("Launching %d concurrent TTS requests…", n)
    t0 = time.perf_counter()

    tasks = [
        one_tts_request(i, TEST_TEXTS[i % len(TEST_TEXTS)], col_ttfa, col_total)
        for i in range(n)
    ]
    await asyncio.gather(*tasks)

    logger.info("All done in %.1fs", time.perf_counter() - t0)
    col_ttfa.print_summary()
    col_total.print_summary()
    p1 = col_ttfa.save_report(REPORTS_DIR)
    p2 = col_total.save_report(REPORTS_DIR)
    logger.info("Reports: %s | %s", p1, p2)
    return col_ttfa.summary(), col_total.summary()


def main():
    ap = argparse.ArgumentParser(description="Cartesia TTS concurrency test")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--n", type=int, default=10, help="Concurrent TTS requests")
    args = ap.parse_args()
    asyncio.run(run(args.n))


if __name__ == "__main__":
    main()
