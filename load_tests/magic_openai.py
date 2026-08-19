"""
magic_openai.py  —  Find the magic number for OpenAI concurrent API calls.

Tests two workloads separately:
  - EVAL: GPT-4o evaluation of a candidate answer (higher quality, slower)
  - QGEN: GPT-4o-mini question generation (faster, lighter)

Each step fires N simultaneous requests and measures end-to-end latency.

Run:
    python -m load_tests.magic_openai
    python -m load_tests.magic_openai --workload eval
    python -m load_tests.magic_openai --workload qgen
    python -m load_tests.magic_openai --steps 1 2 5 10 20 30 50
"""
import asyncio, argparse, logging, time
from openai import AsyncOpenAI

from .config      import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_EVAL_MODEL, REPORTS_DIR, cleanup_previous_outputs
from .ladder_core import LadderBenchmark

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [OAI-MAGIC] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "magic_openai.log", mode="w"),
    ]
)
logger = logging.getLogger("magic_openai")

# ── Prompt templates ───────────────────────────────────────────────────
EVAL_SYSTEM = (
    "You are an expert technical interviewer. Evaluate the candidate answer "
    "on a 1-10 scale across: technical_accuracy, communication_clarity, depth. "
    "Respond with JSON only: "
    '{"score": <int>, "technical_accuracy": <int>, "communication_clarity": <int>, '
    '"depth": <int>, "feedback": "<str>"}'
)
EVAL_USER = (
    "Question: Explain the difference between horizontal and vertical scaling. "
    "When would you choose one over the other?\n\n"
    "Answer: Horizontal scaling means adding more machines to distribute load, "
    "while vertical scaling means upgrading the existing machine. I'd choose "
    "horizontal when the workload is stateless and can be partitioned, like web "
    "servers. Vertical makes sense for databases that can't easily shard."
)

QGEN_SYSTEM = (
    "You are a technical interviewer. Generate ONE concise follow-up question "
    "based on the candidate's answer. Respond with just the question text, "
    "no preamble. Maximum 40 words."
)
QGEN_USER = (
    "Candidate said: 'Horizontal scaling adds machines, vertical upgrades one machine. "
    "I'd use horizontal for stateless services and vertical for databases.'\n"
    "Generate a follow-up question:"
)

WORKLOADS = {
    "eval": {
        "model"      : OPENAI_EVAL_MODEL,
        "system"     : EVAL_SYSTEM,
        "user"       : EVAL_USER,
        "max_tokens" : 200,
        "temperature": 0.2,
        "label"      : "GPT-4o Evaluation",
    },
    "qgen": {
        "model"      : OPENAI_MODEL,
        "system"     : QGEN_SYSTEM,
        "user"       : QGEN_USER,
        "max_tokens" : 80,
        "temperature": 0.7,
        "label"      : "GPT-4o-mini Question Gen",
    },
}


def make_fn(workload: dict):
    """Return an async callable that fires one OpenAI call and returns latency ms."""
    async def _fn(call_id: int) -> float:
        client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        t0 = time.perf_counter()
        try:
            resp = await client.chat.completions.create(
                model       = workload["model"],
                messages    = [
                    {"role": "system", "content": workload["system"]},
                    {"role": "user",   "content": workload["user"]},
                ],
                max_tokens  = workload["max_tokens"],
                temperature = workload["temperature"],
            )
            ms = (time.perf_counter() - t0) * 1000
            logger.debug("[call-%03d] %.0fms | %d tokens | model=%s",
                         call_id, ms, resp.usage.total_tokens, workload["model"])
            return ms
        finally:
            await client.close()
    return _fn


async def run(steps: list, workload_key: str):
    workload = WORKLOADS[workload_key]
    name     = f"OpenAI {workload['label']}"
    fn       = make_fn(workload)

    bench = LadderBenchmark(name=name, steps=steps, cooldown_s=2.0)

    logger.info("=" * 60)
    logger.info("  %s — Magic Number Benchmark", name)
    logger.info("  Model: %s | Steps: %s", workload["model"], steps)
    logger.info("=" * 60)

    await bench.run(fn)

    print(f"\n{'='*60}")
    print(f"  🎯 MAGIC NUMBER: {bench.magic_number} concurrent OpenAI calls ({workload_key})")
    print(f"  ⚠️  WARN  NUMBER: {bench.warn_number}")
    print(f"{'='*60}")
    print(bench.ascii_chart())

    extra = f"""
## Operational Recommendation

```python
# In your interview orchestrator:
MAX_CONCURRENT_OPENAI_{workload_key.upper()} = {bench.magic_number}

# If you have interview peaks (all 50 candidates answer at once),
# use a semaphore:
openai_sem = asyncio.Semaphore({bench.magic_number})
async with openai_sem:
    result = await openai_call(...)
```

## Rate Limit Notes
- OpenAI TPM (tokens per minute) limits matter as much as RPM
- At n={bench.magic_number}, estimated TPM ≈ {bench.magic_number * workload['max_tokens'] * 60 // 5:,} tokens/min
  (assuming ~5s per call, {workload['max_tokens']} tokens each)
- Check your OpenAI tier limits at platform.openai.com/account/limits
"""
    path = bench.save_report(REPORTS_DIR, extra_notes=extra)
    logger.info("Report: %s", path)
    return bench


def main():
    ap = argparse.ArgumentParser(description="OpenAI magic number finder")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--steps",    nargs="+", type=int,
                    default=[1, 2, 5, 10, 15, 20, 25, 30],
                    help="Concurrency ladder steps")
    ap.add_argument("--workload", choices=["eval", "qgen", "both"],
                    default="both", help="Which workload to test")
    args = ap.parse_args()

    async def _main():
        if args.workload == "both":
            for wl in ["qgen", "eval"]:
                await run(args.steps, wl)
                await asyncio.sleep(5)
        else:
            await run(args.steps, args.workload)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
