"""
test_06_full_simulation.py  —  Full end-to-end interview simulation.

This is the MAIN load test. It:

1. Resets N interviews in the DB (fresh rooms via Connect server)
2. Starts N engine bot processes (via aira_3_webrtc_orchestration.py logic)
3. Simultaneously joins N simulated candidates to their rooms
4. Each simulated candidate publishes speech-like audio for the full interview
5. The real bot engine handles the interview (Deepgram → GPT → Cartesia → LiveKit)
6. Tracks metrics for every phase and generates a final report

Usage:
    python -m load_tests.test_06_full_simulation --n 10 --duration 300
    python -m load_tests.test_06_full_simulation --n 50 --duration 600
    python -m load_tests.test_06_full_simulation --n 10 --duration 300 --dry-run
"""
import asyncio, argparse, logging, time, os, sys, json
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional
import mysql.connector
import httpx

# Engine root on sys.path so we can import orchestration code
ENGINE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

from load_tests.config        import (DB_HOST, DB_PORT, DB_USER, DB_PASS,
                                       DB_NAME, CONNECT_BASE_URL, REPORTS_DIR,
                                       cleanup_previous_outputs)
from load_tests.metrics       import MetricCollector
from load_tests.candidate_sim import run_simulated_candidate, fetch_livekit_token
from load_tests.recording     import recording_path
from load_tests.sim_report    import generate_interview_report, generate_master_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SIM] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(REPORTS_DIR / "simulation.log", mode="w"),
    ]
)
logger = logging.getLogger("full_sim")


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InterviewSlot:
    interview_id:    int
    room_id:         str
    candidate_name:  str
    candidate_url:   str = ""
    bot_url:         str = ""
    livekit_url:     str = ""
    livekit_token:   str = ""


@dataclass
class SimulationResult:
    interview_id:    int
    room_id:         str
    candidate_name:  str
    # Timing
    reset_ms:        float = 0.0
    candidate_connect_ms: float = 0.0
    first_bot_speech_ms:  float = 0.0
    total_duration_s:     float = 0.0
    # Status
    candidate_connected: bool = False
    track_published:     bool = False
    bot_heard:           bool = False
    error:               Optional[str] = None
    timeline:            list = field(default_factory=list)
    # Post-interview assets
    recording_path:      Optional[str] = None
    report_path:         Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# Step 1: Fetch interviews from DB
# ═══════════════════════════════════════════════════════════════════════════

def get_db():
    return mysql.connector.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASS, database=DB_NAME,
    )


def fetch_interview_slots(n: int) -> List[InterviewSlot]:
    """Fetch the N most recent interviews from the DB."""
    conn = get_db()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT i.id, i.room_id, i.candidate_join_url, i.bot_join_url,
               c.candidate_name
        FROM interviews i
        JOIN candidates c ON c.id = i.candidate_id
        ORDER BY i.id DESC
        LIMIT %s
    """, (n,))
    rows = cur.fetchall()
    cur.close(); conn.close()

    slots = []
    for r in rows:
        slots.append(InterviewSlot(
            interview_id   = r["id"],
            room_id        = r["room_id"] or "",
            candidate_name = r["candidate_name"] or f"Candidate-{r['id']}",
            candidate_url  = r["candidate_join_url"] or "",
            bot_url        = r["bot_join_url"] or "",
        ))
    return slots


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Reset rooms — delegates to reset_interview.py (single source of truth)
# ═══════════════════════════════════════════════════════════════════════════

import reset_interview as _ri

async def reset_one_room(slot: InterviewSlot, idx: int, total: int,
                          col_reset: MetricCollector) -> bool:
    """Delegate to reset_interview.reset_one() — auth/API/DB logic lives there."""
    t0 = time.perf_counter()
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _ri.reset_one, slot.interview_id, slot.candidate_name
        )
        ms = (time.perf_counter() - t0) * 1000
        col_reset.record(slot.room_id or str(slot.interview_id), ms, True)

        slot.room_id       = result["room_id"]
        slot.candidate_url = result["candidate_url"]
        slot.bot_url       = result["bot_url"]

        logger.info("[%d/%d] ✅ Room reset: %s → %s (%.0fms)",
                    idx+1, total, slot.interview_id, slot.room_id, ms)
        return True
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        col_reset.record(str(slot.interview_id), ms, False, str(e))
        logger.error("[%d/%d] ❌ Reset failed for interview %d: %s",
                     idx+1, total, slot.interview_id, e)
        return False


async def reset_all_rooms(slots: List[InterviewSlot],
                           col_reset: MetricCollector) -> List[InterviewSlot]:
    """Reset all rooms concurrently (max 5 at a time to avoid rate-limiting)."""
    sem  = asyncio.Semaphore(5)
    ok   = []

    async def _reset(slot, i):
        async with sem:
            success = await reset_one_room(slot, i, len(slots), col_reset)
            if success:
                ok.append(slot)

    await asyncio.gather(*[_reset(s, i) for i, s in enumerate(slots)])
    return ok


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Fetch LiveKit tokens for simulated candidates
# ═══════════════════════════════════════════════════════════════════════════

async def fetch_candidate_tokens(slots: List[InterviewSlot]) -> None:
    """Fetch LiveKit JWT for each candidate simulation."""
    sem = asyncio.Semaphore(10)

    async def _fetch(slot):
        async with sem:
            try:
                tok = await fetch_livekit_token(
                    slot.room_id, slot.candidate_name, role="candidate"
                )
                slot.livekit_url   = tok["url"]
                slot.livekit_token = tok["token"]
                logger.debug("Token OK for room %s", slot.room_id)
            except Exception as e:
                logger.error("Token failed for %s: %s", slot.room_id, e)

    await asyncio.gather(*[_fetch(s) for s in slots])


# ═══════════════════════════════════════════════════════════════════════════
# Step 4: Start engine bots for each interview
# ═══════════════════════════════════════════════════════════════════════════

def start_engine_for_slot(slot: InterviewSlot) -> Optional[asyncio.subprocess.Process]:
    """
    Start the engine for one interview in a subprocess.
    We call the engine's run_interview() via a small wrapper script.
    """
    # The engine normally picks up interviews from the DB.
    # We trigger it by calling aira_3_webrtc_orchestration.py with a specific interview_id.
    # This requires a --interview-id CLI mode in the orchestration.
    # 
    # Fallback: mark the interview as 'Scheduled' so the normal polling picks it up.
    pass   # Engine is started separately — see run()


# ═══════════════════════════════════════════════════════════════════════════
# Step 5: Run simulated candidates concurrently
# ═══════════════════════════════════════════════════════════════════════════

async def run_candidate(slot: InterviewSlot, duration_s: float,
                         col: MetricCollector,
                         results: list, sem: asyncio.Semaphore,
                         run_id: str = ""):
    async with sem:
        logger.info("🧑 Starting candidate sim: %s / room=%s",
                    slot.candidate_name, slot.room_id)
        t0 = time.perf_counter()

        result = await run_simulated_candidate(
            room_id            = slot.room_id,
            candidate_name     = slot.candidate_name,
            audio_duration_s   = duration_s,
            wait_for_bot_s     = 20.0,
            livekit_url        = slot.livekit_url or None,
            livekit_token      = slot.livekit_token or None,
            candidate_join_url = slot.candidate_url or None,
            interview_id       = slot.interview_id,
        )

        # Recording path comes from candidate_sim (client-side)
        rec_file = result.recording_path or recording_path(slot.room_id, slot.interview_id)

        total_ms = (time.perf_counter() - t0) * 1000
        ok = result.connected and result.track_published

        col.record(
            slot.room_id,
            result.connected_at_ms if result.connected else total_ms,
            ok,
            error=result.error,
        )

        # Generate per-interview HTML report
        report_path = None
        try:
            report_path = generate_interview_report(
                slot.interview_id,
                recording_path=rec_file,
                run_id=run_id,
            )
            if report_path:
                logger.info("📄 Interview report: %s", report_path)
        except Exception as e:
            logger.warning("⚠️  Could not generate interview report for %d: %s", slot.interview_id, e)

        sim_result = SimulationResult(
            interview_id         = slot.interview_id,
            room_id              = slot.room_id,
            candidate_name       = slot.candidate_name,
            candidate_connect_ms = result.connected_at_ms,
            total_duration_s     = result.duration_s,
            candidate_connected  = result.connected,
            track_published      = result.track_published,
            bot_heard            = result.bot_joined,
            error                = result.error,
            timeline             = result.timeline,
            recording_path       = rec_file,
            report_path          = str(report_path) if report_path else None,
        )
        results.append(sim_result)

        status = "✅" if ok else "❌"
        logger.info("%s Done: %s | connected=%s | track=%s | bot_seen=%s | %.0fs",
                    status, slot.candidate_name,
                    result.connected, result.track_published,
                    result.bot_joined, result.duration_s)


# ═══════════════════════════════════════════════════════════════════════════
# Step 6: Generate final report
# ═══════════════════════════════════════════════════════════════════════════

def generate_final_report(results: List[SimulationResult],
                           col_reset: MetricCollector,
                           col_candidate: MetricCollector,
                           n: int, duration_s: float,
                           wall_time_s: float) -> Path:
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"full_simulation_n{n}_{ts}.md"

    ok  = [r for r in results if r.candidate_connected and r.track_published]
    err = [r for r in results if not (r.candidate_connected and r.track_published)]

    reset_s   = col_reset.summary()
    cand_s    = col_candidate.summary()

    md = f"""# AIRA Full Simulation Report
**Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
**Interviews simulated:** {n}
**Audio duration per candidate:** {duration_s}s
**Total wall time:** {wall_time_s:.1f}s

---

## Overall Results

| Metric | Value |
|--------|-------|
| Candidates launched | {n} |
| Connected to LiveKit | {len([r for r in results if r.candidate_connected])} |
| Track published | {len([r for r in results if r.track_published])} |
| Bot presence detected | {len([r for r in results if r.bot_heard])} |
| **Full success** | **{len(ok)} / {n}** |
| Errors | {len(err)} |

---

## Room Reset Metrics
| P50 | P95 | Max | Success rate |
|-----|-----|-----|-------------|
| {reset_s.get("latency_ms",{}).get("p50","?")}ms | {reset_s.get("latency_ms",{}).get("p95","?")}ms | {reset_s.get("latency_ms",{}).get("max","?")}ms | {reset_s.get("success_rate","?")} |

## Candidate Connection Metrics
| P50 | P95 | Max | Success rate |
|-----|-----|-----|-------------|
| {cand_s.get("latency_ms",{}).get("p50","?")}ms | {cand_s.get("latency_ms",{}).get("p95","?")}ms | {cand_s.get("latency_ms",{}).get("max","?")}ms | {cand_s.get("success_rate","?")} |

---

## Per-Interview Results

| # | Room | Candidate | Connected | Track | Bot Seen | Error |
|---|------|-----------|-----------|-------|----------|-------|
"""
    for i, r in enumerate(sorted(results, key=lambda x: x.interview_id), 1):
        c  = "✅" if r.candidate_connected else "❌"
        t  = "✅" if r.track_published     else "❌"
        b  = "✅" if r.bot_heard           else "⏳"
        e  = r.error or ""
        md += f"| {i} | {r.room_id} | {r.candidate_name} | {c} | {t} | {b} | {e[:60]} |\n"

    if err:
        md += "\n---\n\n## Error Details\n\n"
        for r in err:
            md += f"### {r.room_id} ({r.candidate_name})\n"
            md += f"- Error: `{r.error}`\n"
            md += "- Timeline:\n"
            for ev in r.timeline:
                md += f"  - {ev}\n"
            md += "\n"

    md += f"\n---\n*Report: {path.name}*\n"

    with open(path, "w") as f:
        f.write(md)

    # Also save JSON
    json_path = path.with_suffix(".json")
    with open(json_path, "w") as f:
        json.dump({
            "meta"       : {"n": n, "duration_s": duration_s, "wall_time_s": wall_time_s},
            "reset"      : reset_s,
            "candidates" : cand_s,
            "results"    : [asdict(r) for r in results],
        }, f, indent=2)

    return path


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

async def run(n: int, duration_s: float, dry_run: bool = False,
              max_concurrency: int = 20):

    logger.info("=" * 70)
    logger.info("  AIRA Full Simulation  |  n=%d  |  duration=%.0fs  |  dry_run=%s",
                n, duration_s, dry_run)
    logger.info("=" * 70)

    col_reset     = MetricCollector(f"Room Reset (n={n})")
    col_candidate = MetricCollector(f"Candidate Connect (n={n})")
    results: List[SimulationResult] = []
    t_wall = time.perf_counter()

    # ── Step 1: Fetch interview slots ──────────────────────────────────
    logger.info("\n── Step 1: Fetching %d interview slots from DB…", n)
    slots = fetch_interview_slots(n)
    if not slots:
        logger.error("No interviews found. Run: python reset_interview.py %d", n)
        return
    if len(slots) < n:
        logger.warning("Only %d interviews found (requested %d)", len(slots), n)
        n = len(slots)
    logger.info("   Found %d interviews", len(slots))

    if dry_run:
        logger.info("\n[DRY RUN] Would reset %d rooms and launch %d candidates.", n, n)
        logger.info("[DRY RUN] Rooms:\n%s",
                    "\n".join(f"  {s.interview_id} | {s.room_id} | {s.candidate_name}"
                              for s in slots))
        return

    # ── Step 2: Reset rooms ────────────────────────────────────────────
    logger.info("\n── Step 2: Resetting %d rooms on %s…", n, CONNECT_BASE_URL)
    slots = await reset_all_rooms(slots, col_reset)
    col_reset.print_summary()

    if not slots:
        logger.error("All room resets failed. Aborting.")
        return
    logger.info("   %d rooms ready", len(slots))

    # ── Step 3: Fetch LiveKit tokens for candidates ────────────────────
    logger.info("\n── Step 3: Fetching LiveKit tokens for %d candidates…", len(slots))
    await fetch_candidate_tokens(slots)
    token_ok = [s for s in slots if s.livekit_token]
    logger.info("   %d/%d tokens fetched", len(token_ok), len(slots))

    # ── Step 4: Launch engine bots ─────────────────────────────────────
    logger.info("\n── Step 4: Engine bots should be started SEPARATELY.")
    logger.info("   Run in another terminal:")
    logger.info("   AIRA_MAX_PARALLEL=%d python aira_3_webrtc_orchestration.py", n)
    logger.info("   (waiting 15s for engine to start picking up interviews…)")
    await asyncio.sleep(15)

    # ── Step 5: Launch simulated candidates ───────────────────────────
    logger.info("\n── Step 5: Launching %d simulated candidates concurrently…", len(token_ok))
    logger.info("   Max concurrency: %d | Audio duration: %.0fs", max_concurrency, duration_s)

    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    sem = asyncio.Semaphore(max_concurrency)
    await asyncio.gather(*[
        run_candidate(slot, duration_s, col_candidate, results, sem, run_id=run_id)
        for slot in token_ok
    ])

    wall_time = time.perf_counter() - t_wall
    logger.info("\n── All candidates done. Wall time: %.1fs", wall_time)

    # ── Step 6: Sim metrics report (Markdown) ─────────────────────────
    col_candidate.print_summary()
    report_path = generate_final_report(
        results, col_reset, col_candidate, n, duration_s, wall_time
    )

    # ── Step 7: Per-interview HTML reports + master HTML report ───────
    logger.info("\n── Step 7: Generating master HTML report…")
    master_data = []
    for r in results:
        # Fetch score + question count from DB for master report
        try:
            import pymysql
            conn = pymysql.connect(
                host=DB_HOST, port=DB_PORT, user=DB_USER,
                passwd=DB_PASS, db=DB_NAME,
                cursorclass=pymysql.cursors.DictCursor, autocommit=True,
            )
            with conn.cursor() as cur:
                # Overall score from interviews table
                cur.execute(
                    "SELECT interview_score FROM interviews WHERE id=%s",
                    (r.interview_id,)
                )
                irow = cur.fetchone() or {}
                # Question count from interview_questions_live
                cur.execute(
                    "SELECT COUNT(*) as q_count FROM interview_questions_live WHERE interview_id=%s",
                    (r.interview_id,)
                )
                qrow = cur.fetchone() or {}
            conn.close()
            raw_score = irow.get("interview_score")
            avg_score = round(float(raw_score), 2) if raw_score is not None else "N/A"
            qs_asked  = int(qrow["q_count"]) if qrow.get("q_count") else 0
        except Exception:
            avg_score, qs_asked = "N/A", 0

        master_data.append({
            "interview_id"  : r.interview_id,
            "room_id"       : r.room_id,
            "candidate_name": r.candidate_name,
            "status"        : "Completed" if r.candidate_connected and r.bot_heard else "Partial",
            "avg_score"     : avg_score,
            "questions_asked": qs_asked,
            "recording_path": getattr(r, "recording_path", None),
            "report_path"   : getattr(r, "report_path", None),
            "duration_s"    : r.total_duration_s,
            "error"         : r.error or "",
        })

    master_path = generate_master_report(master_data, run_id=run_id)
    logger.info("\n✅ Full simulation complete!")
    logger.info("📊 Master HTML report : %s", master_path)
    logger.info("📄 Metrics MD report  : %s", report_path)
    return results


def main():
    cleanup_previous_outputs(__file__)
    ap = argparse.ArgumentParser(
        description="AIRA Full End-to-End Interview Simulation"
    )
    ap.add_argument("--n",         type=int,   default=10,
                    help="Number of concurrent interviews to simulate")
    ap.add_argument("--duration",  type=float, default=300,
                    help="Seconds of audio each simulated candidate streams")
    ap.add_argument("--max-conc",  type=int,   default=20,
                    help="Max simultaneous LiveKit connections")
    ap.add_argument("--dry-run",   action="store_true",
                    help="Preview what would happen without actually running")
    args = ap.parse_args()
    asyncio.run(run(args.n, args.duration, args.dry_run, args.max_conc))


if __name__ == "__main__":
    main()
