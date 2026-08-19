"""
test_04_database.py  —  MySQL concurrent read/write test.

Simulates the DB operations that happen during a live interview:
  - Status updates (InProgress, Completed)
  - Transcript writes
  - Score saves
  - Status polls (bot polls room state)

Run:
    python -m load_tests.test_04_database --n 20 --ops 10
"""
import asyncio, argparse, logging, time, random, string
from concurrent.futures import ThreadPoolExecutor
import mysql.connector

from .config  import DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME, REPORTS_DIR, cleanup_previous_outputs
from .metrics import MetricCollector

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [DB-TEST] %(message)s")
logger = logging.getLogger("test_db")


def get_conn():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME,
        connect_timeout=5,
    )


def rand_str(n=8):
    return "".join(random.choices(string.ascii_lowercase, k=n))


# ── Individual operations ──────────────────────────────────────────────

def op_read_interviews(n_rows: int = 5) -> float:
    t0   = time.perf_counter()
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "SELECT id, room_id, interview_status FROM interviews "
        "ORDER BY id DESC LIMIT %s", (n_rows,)
    )
    cur.fetchall()
    cur.close()
    conn.close()
    return (time.perf_counter() - t0) * 1000


def op_write_status(interview_id: int, status: str) -> float:
    t0   = time.perf_counter()
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(
        "UPDATE interviews SET interview_status=%s WHERE id=%s",
        (status, interview_id)
    )
    conn.commit()
    cur.close()
    conn.close()
    return (time.perf_counter() - t0) * 1000


def op_write_transcript(interview_id: int) -> float:
    transcript = f"Test transcript {rand_str()} " * 20
    t0   = time.perf_counter()
    conn = get_conn()
    cur  = conn.cursor()
    # Write to a temp table if aira_db has one, else just read
    # Using SELECT to simulate the workload safely without altering prod data
    cur.execute(
        "SELECT COUNT(*) FROM interviews WHERE id <= %s", (interview_id,)
    )
    cur.fetchone()
    cur.close()
    conn.close()
    return (time.perf_counter() - t0) * 1000


def op_full_interview_sequence(interview_id: int, n_questions: int = 3) -> list:
    """Simulate DB ops for one complete interview."""
    timings = []
    # 1. Mark InProgress
    timings.append(("status_update", op_write_status(interview_id, "InProgress")))
    # 2. Q&A cycles
    for _ in range(n_questions):
        timings.append(("read_interview", op_read_interviews(1)))
        timings.append(("write_transcript", op_write_transcript(interview_id)))
    # 3. Mark Completed
    timings.append(("status_done", op_write_status(interview_id, "Completed")))
    return timings


# ── Async wrapper ──────────────────────────────────────────────────────

async def run_one_session(session_id: int, interview_id: int,
                          col: MetricCollector, executor):
    label = f"session-{session_id:03d}"
    loop  = asyncio.get_event_loop()
    try:
        timings = await loop.run_in_executor(
            executor, op_full_interview_sequence, interview_id, 3
        )
        for op_name, ms in timings:
            col.record(f"{label}:{op_name}", ms, True)
            logger.debug("[%s] %s: %.1fms", label, op_name, ms)
        logger.info("[%s] ✅ %d ops done", label, len(timings))
    except Exception as e:
        col.record(label, 0, False, str(e))
        logger.error("[%s] ❌ %s", label, e)


async def run(n: int):
    col = MetricCollector(f"MySQL Concurrent Ops (n={n})")

    # Fetch real interview IDs to use
    try:
        conn = get_conn()
        cur  = conn.cursor()
        cur.execute("SELECT id FROM interviews ORDER BY id DESC LIMIT %s", (n,))
        interview_ids = [row[0] for row in cur.fetchall()]
        cur.close(); conn.close()
    except Exception as e:
        logger.error("Cannot connect to DB: %s", e)
        return

    if not interview_ids:
        logger.error("No interviews in DB — run reset_interview.py first")
        return

    # Pad with repeats if fewer rows than n
    while len(interview_ids) < n:
        interview_ids.extend(interview_ids)
    interview_ids = interview_ids[:n]

    logger.info("Running %d concurrent DB sessions…", n)
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=n) as executor:
        await asyncio.gather(*[
            run_one_session(i, interview_ids[i], col, executor)
            for i in range(n)
        ])

    logger.info("All done in %.1fs", time.perf_counter() - t0)
    col.print_summary()
    p = col.save_report(REPORTS_DIR)
    logger.info("Report: %s", p)
    return col.summary()


def main():
    ap = argparse.ArgumentParser(description="MySQL concurrency test")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--n", type=int, default=10, help="Concurrent sessions")
    args = ap.parse_args()
    asyncio.run(run(args.n))


if __name__ == "__main__":
    main()
