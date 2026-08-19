"""
find_magic_numbers.py  —  Master runner: find magic numbers for all APIs.

Runs Deepgram, OpenAI (eval + qgen), and Cartesia in sequence.
After all benchmarks complete, generates a single MASTER report
documenting the magic numbers, recommendations, and Key Manager config.

Run:
    python -m load_tests.find_magic_numbers
    python -m load_tests.find_magic_numbers --steps 1 2 5 10 20 30
    python -m load_tests.find_magic_numbers --skip cartesia
    python -m load_tests.find_magic_numbers --only deepgram
"""
import asyncio, argparse, logging, time, json
from datetime import datetime
from pathlib import Path

from .config import REPORTS_DIR, cleanup_previous_outputs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MAGIC] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "find_magic_numbers.log", mode="w"),
    ]
)
logger = logging.getLogger("find_magic")

BANNER = r"""
  ╔═══════════════════════════════════════════════════════════════╗
  ║         AIRA Magic Number Finder — All APIs                   ║
  ║   Deepgram ·  OpenAI (eval + qgen) · Cartesia TTS             ║
  ╚═══════════════════════════════════════════════════════════════╝
"""


def generate_master_report(results: dict, steps: list, wall_time: float) -> Path:
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"MAGIC_NUMBERS_{ts}.md"

    rows = []
    for name, bench in results.items():
        if bench is None:
            rows.append((name, "ERROR", "—", "—", "—"))
        else:
            rows.append((
                name,
                str(bench.magic_number),
                str(bench.warn_number),
                f"{bench.baseline.p95:.0f}ms",
                f">{bench.baseline.p95*2:.0f}ms",
            ))

    table_header = "| API | 🎯 Magic # | ⚠️ Warn # | Baseline p95 | Degraded at |"
    table_sep    = "|-----|-----------|---------|-------------|------------|"
    table_rows   = "\n".join(
        f"| {r[0]} | **{r[1]}** | {r[2]} | {r[3]} | {r[4]} |" for r in rows
    )

    # Build key manager config
    magic = {name: bench.magic_number for name, bench in results.items() if bench}
    dg_magic    = magic.get("Deepgram TTFA", 20)
    oai_eval    = magic.get("OpenAI GPT-4o Evaluation", 15)
    oai_qgen    = magic.get("OpenAI GPT-4o-mini Question Gen", 25)
    tts_magic   = magic.get("Cartesia TTS TTFA", 20)

    key_manager_code = f'''
class APIKeyManager:
    """
    Capacity-unit based API key manager.
    Each concurrent interview = 1 capacity unit.
    
    Auto-generated from magic number benchmarks on {datetime.utcnow().strftime("%Y-%m-%d")}.
    Re-run: python -m load_tests.find_magic_numbers
    """
    
    # ── Magic numbers (safe concurrency limits per API key) ──────────
    DEEPGRAM_MAGIC   = {dg_magic}    # max concurrent WS streams per key
    OPENAI_EVAL_MAGIC = {oai_eval}   # max concurrent GPT-4o eval calls per key
    OPENAI_QGEN_MAGIC = {oai_qgen}   # max concurrent GPT-4o-mini calls per key
    TTS_MAGIC        = {tts_magic}   # max concurrent Cartesia requests per key
    
    def __init__(self, deepgram_keys: list, openai_keys: list,
                 cartesia_keys: list):
        import asyncio
        
        # Each key gets a semaphore capped at its magic number
        self._dg_sems  = {{k: asyncio.Semaphore(self.DEEPGRAM_MAGIC)
                           for k in deepgram_keys}}
        self._oai_eval_sems = {{k: asyncio.Semaphore(self.OPENAI_EVAL_MAGIC)
                                for k in openai_keys}}
        self._oai_qgen_sems = {{k: asyncio.Semaphore(self.OPENAI_QGEN_MAGIC)
                                for k in openai_keys}}
        self._tts_sems = {{k: asyncio.Semaphore(self.TTS_MAGIC)
                           for k in cartesia_keys}}
        
        # Usage counters for monitoring
        self._usage = {{k: 0 for k in
                        list(deepgram_keys) + list(openai_keys) + list(cartesia_keys)}}
    
    def _least_loaded(self, sems: dict) -> tuple:
        """Pick the key with the most remaining capacity."""
        return min(sems.items(), key=lambda kv: self._usage.get(kv[0], 0))
    
    def acquire_deepgram(self):
        key, sem = self._least_loaded(self._dg_sems)
        return _KeyLease(key, sem, self._usage)
    
    def acquire_openai_eval(self):
        key, sem = self._least_loaded(self._oai_eval_sems)
        return _KeyLease(key, sem, self._usage)
    
    def acquire_openai_qgen(self):
        key, sem = self._least_loaded(self._oai_qgen_sems)
        return _KeyLease(key, sem, self._usage)
    
    def acquire_tts(self):
        key, sem = self._least_loaded(self._tts_sems)
        return _KeyLease(key, sem, self._usage)
    
    def status(self) -> dict:
        return dict(self._usage)


class _KeyLease:
    """Context manager: acquire semaphore, track usage, release on exit."""
    def __init__(self, key, sem, usage):
        self.key   = key
        self._sem  = sem
        self._usage = usage
    
    async def __aenter__(self):
        await self._sem.acquire()
        self._usage[self.key] = self._usage.get(self.key, 0) + 1
        return self.key
    
    async def __aexit__(self, *_):
        self._sem.release()
        self._usage[self.key] = max(0, self._usage.get(self.key, 0) - 1)


# ── Usage Example ──────────────────────────────────────────────────────
# 
# key_mgr = APIKeyManager(
#     deepgram_keys = [os.environ["DEEPGRAM_API_KEY"]],
#     openai_keys   = [os.environ["OPENAI_API_KEY"]],
#     cartesia_keys = [os.environ["CARTESIA_API_KEY"]],
# )
# 
# async def run_one_interview():
#     # Deepgram
#     async with key_mgr.acquire_deepgram() as dg_key:
#         transcript = await stream_to_deepgram(audio, api_key=dg_key)
#
#     # OpenAI eval
#     async with key_mgr.acquire_openai_eval() as oai_key:
#         score = await evaluate(transcript, api_key=oai_key)
#
#     # TTS
#     async with key_mgr.acquire_tts() as tts_key:
#         audio = await speak(response, api_key=tts_key)
'''

    md = f"""# AIRA API Magic Number Report
**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
**Ladder steps tested:** `{steps}`
**Total benchmark time:** {wall_time:.1f}s

---

## 🎯 Magic Numbers Summary

{table_header}
{table_sep}
{table_rows}

> **Magic number** = last safe concurrency level where p95 latency < 2× baseline.
> Above this, candidates notice delays. At 3× baseline, quality degrades severely.

---

## 📊 Latency Charts

"""
    for name, bench in results.items():
        if bench:
            md += f"### {name}\n\n```\n{bench.ascii_chart()}\n```\n\n"

    md += f"""---

## 🏗️ Auto-Generated Key Manager

Copy this into `key_manager.py` in your engine root:

```python
{key_manager_code}
```

---

## 📐 Capacity Planning

Given your magic numbers, here's how many API keys you need for N interviews:

| Target Concurrent Interviews | Deepgram Keys | OpenAI Keys | Cartesia Keys |
|-----------------------------|---------------|-------------|---------------|
| 10 | {max(1, -(-10//dg_magic))} | {max(1, -(-10//min(oai_eval,oai_qgen)))} | {max(1, -(-10//tts_magic))} |
| 25 | {max(1, -(-25//dg_magic))} | {max(1, -(-25//min(oai_eval,oai_qgen)))} | {max(1, -(-25//tts_magic))} |
| 50 | {max(1, -(-50//dg_magic))} | {max(1, -(-50//min(oai_eval,oai_qgen)))} | {max(1, -(-50//tts_magic))} |
| 100 | {max(1, -(-100//dg_magic))} | {max(1, -(-100//min(oai_eval,oai_qgen)))} | {max(1, -(-100//tts_magic))} |

> Formula: `ceil(target / magic_number)` keys needed per API.

---

## 🔁 Re-Run Instructions

Magic numbers change when:
- API plan tier changes (higher tier = more capacity)
- Network conditions change (different datacenter routing)
- Vendor updates their infrastructure

**Re-benchmark monthly or after any API plan change:**

```bash
cd /Users/sridharkotha/Desktop/AIRA_ENGINE_REPORT
conda activate aira_env
python -m load_tests.find_magic_numbers --steps 1 2 5 10 15 20 25 30 40 50
```

---
*Report: `{path.name}`*
"""
    path.write_text(md)
    logger.info("Master report saved: %s", path)
    return path


async def run(steps: list, skip: list, only: list):
    print(BANNER)
    logger.info("Steps: %s", steps)

    from . import magic_deepgram, magic_openai, magic_cartesia

    benches_to_run = {
        "deepgram": ("Deepgram TTFA",
                     lambda: magic_deepgram.run(steps, "ttfa")),
        "openai_qgen": ("OpenAI QGEN",
                        lambda: magic_openai.run(steps, "qgen")),
        "openai_eval": ("OpenAI EVAL",
                        lambda: magic_openai.run(steps, "eval")),
        "cartesia":  ("Cartesia TTS",
                      lambda: magic_cartesia.run(steps, "ttfa")),
    }

    if only:
        benches_to_run = {k: v for k, v in benches_to_run.items() if k in only}
    if skip:
        benches_to_run = {k: v for k, v in benches_to_run.items() if k not in skip}

    results = {}
    t_wall  = time.perf_counter()

    for key, (display_name, runner) in benches_to_run.items():
        logger.info("\n" + "─"*60)
        logger.info("  Benchmarking: %s", display_name)
        logger.info("─"*60)
        try:
            bench = await runner()
            # Store under the bench's own name for the report
            results[bench.name] = bench
            logger.info("✅ %s — magic number = %d", display_name, bench.magic_number)
        except Exception as e:
            logger.error("❌ %s failed: %s", display_name, e)
            results[display_name] = None

        await asyncio.sleep(3)   # Cooldown between API types

    wall_time = time.perf_counter() - t_wall

    # Master report
    report = generate_master_report(results, steps, wall_time)

    print(f"\n{'='*60}")
    print("  ✅ All benchmarks complete!")
    print(f"  ⏱  Wall time: {wall_time:.1f}s")
    print(f"\n  🎯 Magic Numbers:")
    for name, bench in results.items():
        if bench:
            print(f"     {name}: {bench.magic_number} concurrent")
    print(f"\n  📄 Master report: {report}")
    print(f"{'='*60}\n")


def main():
    cleanup_previous_outputs(__file__)
    ap = argparse.ArgumentParser(
        description="Find magic numbers for all AIRA APIs"
    )
    ap.add_argument("--steps", nargs="+", type=int,
                    default=[1, 2, 5, 10, 15, 20, 25, 30],
                    help="Concurrency ladder (start small!)")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="Skip these (deepgram openai_eval openai_qgen cartesia)")
    ap.add_argument("--only", nargs="*", default=[],
                    help="Run only these")
    args = ap.parse_args()
    asyncio.run(run(args.steps, args.skip or [], args.only or []))


if __name__ == "__main__":
    main()
