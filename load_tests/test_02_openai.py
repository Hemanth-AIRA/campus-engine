"""
test_02_openai.py  —  OpenAI concurrent API call test.

Simulates the two OpenAI calls that happen per interview question:
  1. GPT evaluation (judge the candidate's answer)
  2. GPT question generation (generate next question)

Run:
    python -m load_tests.test_02_openai --n 20
"""
import asyncio, argparse, logging, time
from openai import AsyncOpenAI

from .config  import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_EVAL_MODEL, REPORTS_DIR, cleanup_previous_outputs
from .metrics import MetricCollector

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [OAI-TEST] %(message)s")
logger = logging.getLogger("test_openai")

EVAL_PROMPT = (
    "You are an AI interview evaluator. "
    "Evaluate this answer on a scale of 1-10 for technical accuracy, "
    "communication clarity, and depth. Respond with JSON only: "
    '{"score": <int>, "feedback": "<str>", "dimension_scores": {}}'
)
EVAL_USER = (
    "Question: What is the difference between a process and a thread?\n"
    "Answer: A process is an independent program in execution with its own memory space, "
    "while a thread is a unit of execution within a process that shares memory."
)

QGEN_PROMPT = (
    "You are an AI technical interviewer. Based on the candidate's answer, "
    "generate one follow-up question that probes deeper understanding. "
    "Respond with just the question text."
)
QGEN_USER = (
    "Previous question: What is the difference between a process and a thread?\n"
    "Candidate answer: A process is an independent program with its own memory, "
    "a thread shares memory within a process.\n"
    "Generate a follow-up question:"
)


async def one_openai_round(session_id: int,
                           col_eval: MetricCollector,
                           col_qgen: MetricCollector):
    label  = f"session-{session_id:03d}"
    client = AsyncOpenAI(api_key=OPENAI_API_KEY)

    # ── Evaluation call ────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_EVAL_MODEL,
            messages=[
                {"role": "system",  "content": EVAL_PROMPT},
                {"role": "user",    "content": EVAL_USER},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        ms = (time.perf_counter() - t0) * 1000
        col_eval.record(label, ms, True,
                        tokens=resp.usage.total_tokens)
        logger.info("[%s] Eval: %.0fms | %d tokens",
                    label, ms, resp.usage.total_tokens)
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        col_eval.record(label, ms, False, str(e))
        logger.error("[%s] Eval error: %s", label, e)

    # ── Question generation call ───────────────────────────────────────
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",  "content": QGEN_PROMPT},
                {"role": "user",    "content": QGEN_USER},
            ],
            max_tokens=80,
            temperature=0.7,
        )
        ms = (time.perf_counter() - t0) * 1000
        col_qgen.record(label, ms, True,
                        tokens=resp.usage.total_tokens)
        logger.info("[%s] QGen: %.0fms | %d tokens",
                    label, ms, resp.usage.total_tokens)
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        col_qgen.record(label, ms, False, str(e))
        logger.error("[%s] QGen error: %s", label, e)


async def run(n: int):
    col_eval = MetricCollector(f"OpenAI Evaluation (n={n})")
    col_qgen = MetricCollector(f"OpenAI Question Generation (n={n})")

    logger.info("Launching %d concurrent OpenAI request pairs…", n)
    t0 = time.perf_counter()
    await asyncio.gather(*[
        one_openai_round(i, col_eval, col_qgen) for i in range(n)
    ])
    logger.info("All done in %.1fs", time.perf_counter() - t0)

    col_eval.print_summary()
    col_qgen.print_summary()
    p1 = col_eval.save_report(REPORTS_DIR)
    p2 = col_qgen.save_report(REPORTS_DIR)
    logger.info("Reports: %s | %s", p1, p2)
    return col_eval.summary(), col_qgen.summary()


def main():
    ap = argparse.ArgumentParser(description="OpenAI concurrency test")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--n", type=int, default=10, help="Concurrent request pairs")
    args = ap.parse_args()
    asyncio.run(run(args.n))


if __name__ == "__main__":
    main()
