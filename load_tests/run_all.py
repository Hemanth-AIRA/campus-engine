"""
run_all.py  —  Master test runner for all AIRA load tests.

Runs all individual API tests sequentially and generates a combined report.

Usage:
    python -m load_tests.run_all --n 10
    python -m load_tests.run_all --n 25 --skip-db
    python -m load_tests.run_all --n 50 --only deepgram openai tts
"""
import asyncio, argparse, logging, time, json
from datetime import datetime
from pathlib import Path

from .config  import REPORTS_DIR
from .metrics import MetricCollector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [RUN-ALL] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "run_all.log", mode="w"),
    ]
)
logger = logging.getLogger("run_all")

BANNER = """
╔══════════════════════════════════════════════════════════════╗
║           AIRA Load Test Suite — Full Run                    ║
║           Tests: Deepgram | OpenAI | TTS | DB | LiveKit      ║
╚══════════════════════════════════════════════════════════════╝
"""


async def run_test(name: str, coro, results: dict):
    logger.info("\n" + "─"*60)
    logger.info("  Running: %s", name)
    logger.info("─"*60)
    t0 = time.perf_counter()
    try:
        result = await coro
        elapsed = time.perf_counter() - t0
        results[name] = {
            "status": "PASSED",
            "elapsed_s": round(elapsed, 1),
            "data": result,
        }
        logger.info("✅ %s PASSED (%.1fs)", name, elapsed)
    except Exception as e:
        elapsed = time.perf_counter() - t0
        results[name] = {
            "status": "ERROR",
            "elapsed_s": round(elapsed, 1),
            "error": str(e),
        }
        logger.error("❌ %s ERROR: %s (%.1fs)", name, e, elapsed)


def build_summary_report(results: dict, n: int, wall_time: float) -> Path:
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"summary_n{n}_{ts}.md"

    md = f"""# AIRA Load Test Summary
**Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
**Concurrency level:** n={n}
**Total wall time:** {wall_time:.1f}s

## Test Results

| Test | Status | Time | Notes |
|------|--------|------|-------|
"""
    for name, r in results.items():
        status = "✅ PASS" if r["status"] == "PASSED" else "❌ ERROR"
        note   = r.get("error", "")[:60] if r["status"] == "ERROR" else ""
        md += f"| {name} | {status} | {r['elapsed_s']}s | {note} |\n"

    md += "\n---\n\n## API Latency Summary\n\n"

    # Extract latency tables per test
    for name, r in results.items():
        if r["status"] != "PASSED" or not r.get("data"):
            continue
        data = r["data"]
        if not isinstance(data, (tuple, list)):
            data = [data]
        for d in data:
            if isinstance(d, dict) and "latency_ms" in d:
                lat = d["latency_ms"]
                md += f"### {d.get('test', name)}\n"
                md += (f"- Success rate: {d.get('success_rate','?')}\n"
                       f"- P50: {lat.get('p50','?')}ms  "
                       f"P95: {lat.get('p95','?')}ms  "
                       f"Max: {lat.get('max','?')}ms\n\n")

    md += "---\n\n## Bottleneck Ranking\n\n"
    # Sort by max latency
    rankings = []
    for name, r in results.items():
        if r["status"] == "PASSED" and r.get("data"):
            data = r["data"]
            if not isinstance(data, (tuple, list)):
                data = [data]
            for d in data:
                if isinstance(d, dict) and "latency_ms" in d:
                    rankings.append((name, d["latency_ms"].get("p95", 0)))
    rankings.sort(key=lambda x: x[1], reverse=True)

    for i, (name, p95) in enumerate(rankings, 1):
        emoji = "🔴" if p95 > 5000 else "🟠" if p95 > 2000 else "🟡" if p95 > 1000 else "🟢"
        md += f"{i}. {emoji} **{name}** — P95 latency: {p95}ms\n"

    md += f"\n---\n*Generated: {path.name}*\n"

    with open(path, "w") as f:
        f.write(md)

    # JSON
    with open(path.with_suffix(".json"), "w") as f:
        json.dump({"n": n, "wall_time": wall_time, "results": results}, f, indent=2)

    return path


async def main(n: int, skip: list, only: list):
    print(BANNER)
    logger.info("Concurrency: n=%d", n)

    results   = {}
    t_wall    = time.perf_counter()

    # ── Import test modules lazily ─────────────────────────────────────
    from load_tests import (test_01_deepgram, test_02_openai,
                             test_03_tts, test_04_database, test_05_livekit)

    tests = {
        "deepgram": ("01 Deepgram WS Concurrency",
                     test_01_deepgram.run(n, stream_s=15)),
        "openai":   ("02 OpenAI API Concurrency",
                     test_02_openai.run(n)),
        "tts":      ("03 Cartesia TTS Concurrency",
                     test_03_tts.run(n)),
        "db":       ("04 MySQL DB Concurrency",
                     test_04_database.run(n)),
        "livekit":  ("05 LiveKit Room Joins",
                     test_05_livekit.run(n, stay_s=10)),
    }

    active = {k: v for k, v in tests.items()
              if k not in skip and (not only or k in only)}

    logger.info("Running %d tests: %s", len(active), list(active.keys()))

    for key, (name, coro) in active.items():
        await run_test(name, coro, results)
        await asyncio.sleep(2)   # brief cooldown between tests

    wall_time = time.perf_counter() - t_wall
    report    = build_summary_report(results, n, wall_time)

    print(f"\n{'='*60}")
    print(f"  All tests complete in {wall_time:.1f}s")
    print(f"  Report: {report}")
    print(f"{'='*60}\n")


def entry():
    ap = argparse.ArgumentParser(description="AIRA full load test suite")
    ap.add_argument("--n",    type=int,  default=10,
                    help="Concurrency level for all tests")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Tests to skip (deepgram openai tts db livekit)")
    ap.add_argument("--only", nargs="*", default=[],
                    help="Run only these tests")
    args = ap.parse_args()
    asyncio.run(main(args.n, args.skip or [], args.only or []))


if __name__ == "__main__":
    entry()
