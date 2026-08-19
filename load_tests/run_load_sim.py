"""
load_tests/run_load_sim.py
ONE-COMMAND load simulation: N real interviews end-to-end.

What it does
────────────
1. Picks N interviews from the DB and RESETS them to fresh Connect rooms
   (reuses reset_interview.reset_one — the engine's own reset path).
2. Fetches a LiveKit token for each simulated candidate.
3. STARTS THE ENGINE itself as a managed subprocess
   (aira_3_webrtc_orchestration.py with AIRA_MAX_PARALLEL=N, AIRA_USE_LIVEKIT=true).
   The engine claims the N interviews, spawns one bot per interview, and each bot
   waits for its candidate.
4. Launches N CONVERSATIONAL candidates (candidate_conversation.py) that join the
   rooms, stream a face video, and TAKE TURNS — silent while the bot asks, then
   playing a pre-generated answer clip when the bot finishes, looping per question.
5. Waits for the engine batch to finish, tears it down, prints a summary + JSON.

Usage
─────
    python -m load_tests.run_load_sim --n 5
    python -m load_tests.run_load_sim --n 20 --max-answers 8 --max-duration 600
    python -m load_tests.run_load_sim --n 5 --seed          # seed DB first
    python -m load_tests.run_load_sim --n 5 --no-engine     # I'll start the engine myself

Prereqs: interviews exist in the DB (use --seed, or load_tests/seed_db.py), the
Connect server is reachable, and .env has LiveKit/Cartesia/Deepgram/OpenAI keys.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Engine root on sys.path so we can reuse the engine's modules + test_06 helpers.
ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))
sys.path.insert(0, str(ENGINE_ROOT / "scripts"))  # reset_interview.py lives in scripts/

from load_tests.candidate_conversation import run_conversational_candidate, ConversationResult
from load_tests.test_06_full_simulation import (
    fetch_interview_slots, reset_all_rooms, fetch_candidate_tokens, InterviewSlot, get_db,
)
from load_tests.metrics import MetricCollector
from load_tests.config import LOGS_DIR

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_load_sim")

LOGS_DIR.mkdir(parents=True, exist_ok=True)


# ── Bound each interview so the bot matches the sim ──────────────────────────
def set_interview_configs(slots, questions: int, duration_min: int):
    """Write interview_config on each interview so the BOT asks exactly
    `questions` questions and hard-caps at `duration_min` minutes — matching the
    sim. Without this the bot defaults to 15 questions and gets stuck in the
    'waiting for candidate to return' loop after the sim's last answer, so the
    interview never completes (no report, no best-face save)."""
    conf = json.dumps({
        "total_rounds": 1,
        "questions_per_round": questions,     # total incl. greeting + background
        "base_level": "medium",
        "pass_threshold_pct": 50,
        "max_duration_minutes": duration_min,  # hard cap → guarantees completion
    })
    conn = get_db()
    cur = conn.cursor()
    for s in slots:
        cur.execute("UPDATE interviews SET interview_config=%s WHERE id=%s",
                    (conf, s.interview_id))
    conn.commit()
    cur.close()
    conn.close()
    logger.info("   interview_config set: questions_per_round=%d, max_duration_minutes=%d (%d interviews)",
                questions, duration_min, len(slots))


# ── Engine subprocess management ─────────────────────────────────────────────
def start_engine(n: int, log_path: Path) -> subprocess.Popen:
    """Launch the engine as a managed subprocess that claims N interviews."""
    env = os.environ.copy()
    # Claim generously so the freshly-reset interview is picked even alongside stale
    # 'Not Started' rows from earlier local runs (the extra bots just idle-wait).
    env["AIRA_MAX_PARALLEL"] = str(max(n, 10))
    env["AIRA_USE_LIVEKIT"] = "true"
    logf = open(log_path, "w")
    logger.info("🚀 Starting engine: AIRA_MAX_PARALLEL=%d → %s", n, log_path)
    proc = subprocess.Popen(
        [sys.executable, "aira_3_webrtc_orchestration.py"],
        cwd=str(ENGINE_ROOT),
        env=env,
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    proc._logf = logf  # keep handle so it isn't GC'd / closed early
    return proc


def stop_engine(proc: subprocess.Popen, grace_s: float = 10.0):
    if proc.poll() is not None:
        logger.info("🏁 Engine already exited (code=%s)", proc.returncode)
    else:
        logger.info("🛑 Terminating engine subprocess…")
        proc.terminate()
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            logger.warning("Engine didn't stop — killing")
            proc.kill()
    try:
        proc._logf.close()
    except Exception:
        pass


# ── One candidate, instrumented ──────────────────────────────────────────────
async def _run_candidate(slot: InterviewSlot, sem: asyncio.Semaphore,
                         results: list, max_answers: int, max_duration: float,
                         answer_seconds: float):
    async with sem:
        try:
            res = await run_conversational_candidate(
                slot.room_id,
                slot.candidate_name,
                livekit_url=slot.livekit_url or None,
                livekit_token=slot.livekit_token or None,
                candidate_join_url=slot.candidate_url or None,
                interview_id=slot.interview_id,
                max_answers=max_answers,
                max_duration_s=max_duration,
                answer_max_s=answer_seconds,
            )
        except Exception as e:
            res = ConversationResult(room_id=slot.room_id, candidate_name=slot.candidate_name)
            res.error = f"crashed: {e}"
        results.append((slot, res))


# ── Main ─────────────────────────────────────────────────────────────────────
async def run(n: int, max_answers: int, max_duration: float,
              start_engine_flag: bool, seed: bool, bot_warmup_s: float,
              questions: int = 0, answer_seconds: float = 15.0):
    run_id = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    logger.info("═" * 70)
    logger.info("  AIRA LOAD SIM | n=%d | max_answers=%d | max_duration=%.0fs | engine=%s",
                n, max_answers, max_duration, "managed" if start_engine_flag else "external")
    logger.info("═" * 70)

    if seed:
        logger.info("\n── Seeding DB with interviews…")
        import load_tests.seed_db as seed_db
        await asyncio.get_event_loop().run_in_executor(None, seed_db.main, False)

    # Step 1: pick interviews
    slots = fetch_interview_slots(n)
    if not slots:
        logger.error("No interviews in DB. Run with --seed or seed_db.py first.")
        return
    logger.info("── Step 1: %d interview(s) selected", len(slots))

    # Step 2: reset to fresh Connect rooms
    logger.info("\n── Step 2: Resetting %d rooms…", len(slots))
    col_reset = MetricCollector("room_reset")
    slots = await reset_all_rooms(slots, col_reset)
    if not slots:
        logger.error("All room resets failed — aborting.")
        return
    logger.info("   %d room(s) ready", len(slots))

    # Step 3: LiveKit tokens
    logger.info("\n── Step 3: Fetching candidate LiveKit tokens…")
    await fetch_candidate_tokens(slots)
    # Task 2 local testing: mint candidate tokens locally with the .env LiveKit key (the key
    # the single LiveKit server actually accepts) instead of Connect's token, which is signed
    # with a stale/different key. Identity must NOT start with 'bot' (native presence).
    if os.getenv("SIM_LOCAL_TOKEN", "").lower() in ("1", "true", "yes"):
        from livekit.api import AccessToken, VideoGrants
        _lk_url = os.getenv("LIVEKIT_URL", "")
        _lk_key = os.getenv("LIVEKIT_API_KEY", "")
        _lk_sec = os.getenv("LIVEKIT_API_SECRET", "")
        _n = 0
        for s in slots:
            if not s.room_id:
                continue
            s.livekit_url = _lk_url
            s.livekit_token = (AccessToken(_lk_key, _lk_sec)
                               .with_identity(f"candidate-{s.room_id}")
                               .with_name(s.candidate_name or "Candidate")
                               .with_metadata('{"role":"candidate"}')
                               .with_grants(VideoGrants(room_join=True, room=s.room_id,
                                                        can_publish=True, can_subscribe=True,
                                                        can_publish_data=True))
                               .to_jwt())
            _n += 1
        logger.info("   🔑 minted %d local candidate token(s) (.env LiveKit key)", _n)
    ready = [s for s in slots if s.livekit_token]
    logger.info("   %d/%d tokens fetched", len(ready), len(slots))
    if not ready:
        logger.error("No candidate tokens — aborting.")
        return

    # Step 3.5: bound each interview to the sim (bot asks N questions, caps the
    # duration) so it completes instead of waiting forever for 15 answers.
    questions_per_round = questions if questions > 0 else max_answers
    duration_min = max(2, int(round(max_duration / 60.0)))
    logger.info("\n── Step 3.5: Setting interview_config to match the sim…")
    set_interview_configs(ready, questions_per_round, duration_min)

    # Step 4: start the engine (claims the freshly-reset interviews)
    engine = None
    if start_engine_flag:
        log_path = LOGS_DIR / f"engine_{run_id}.log"
        engine = start_engine(len(ready), log_path)
        logger.info("   Waiting %.0fs for bots to claim rooms & connect…", bot_warmup_s)
        await asyncio.sleep(bot_warmup_s)
        if engine.poll() is not None:
            logger.error("Engine exited early (code=%s) — check %s", engine.returncode, log_path)
    else:
        logger.info("\n── Step 4: Start the engine yourself now:")
        logger.info("   AIRA_MAX_PARALLEL=%d AIRA_USE_LIVEKIT=true python aira_3_webrtc_orchestration.py", len(ready))
        logger.info("   Waiting %.0fs…", bot_warmup_s)
        await asyncio.sleep(bot_warmup_s)

    # Step 5: launch conversational candidates
    logger.info("\n── Step 5: Launching %d conversational candidates…", len(ready))
    t_wall = time.perf_counter()
    results: list = []
    sem = asyncio.Semaphore(len(ready))  # all at once — that's the load
    await asyncio.gather(*[
        _run_candidate(s, sem, results, max_answers, max_duration, answer_seconds) for s in ready
    ])
    wall = time.perf_counter() - t_wall
    logger.info("\n── All candidates finished in %.1fs", wall)

    # Step 6: let the engine batch wind down
    if engine is not None:
        logger.info("── Waiting for engine batch to finish (≤120s)…")
        for _ in range(120):
            if engine.poll() is not None:
                break
            await asyncio.sleep(1)
        stop_engine(engine)

    # Step 7: report
    _report(results, wall, run_id, n)


def _report(results: list, wall: float, run_id: str, n: int):
    logger.info("\n" + "═" * 70)
    logger.info("  RESULTS")
    logger.info("─" * 70)
    logger.info("  %-22s %-7s %-6s %-6s %-7s %s",
                "room", "conn", "Qs", "Ans", "secs", "ended/error")
    rows = []
    connected = answered = bot_heard = 0
    for slot, r in results:
        ok = "✅" if r.connected else "❌"
        if r.connected: connected += 1
        if r.bot_audio_heard: bot_heard += 1
        answered += r.answers_given
        note = r.error or r.ended_reason or ""
        logger.info("  %-22s %-7s %-6d %-6d %-7.0f %s",
                    (r.room_id or "?")[:22], ok, r.questions_heard,
                    r.answers_given, r.duration_s, note[:38])
        rows.append({
            "room_id": r.room_id, "candidate": r.candidate_name,
            "connected": r.connected, "bot_audio_heard": r.bot_audio_heard,
            "questions_heard": r.questions_heard, "answers_given": r.answers_given,
            "duration_s": round(r.duration_s, 1),
            "ended_reason": r.ended_reason, "error": r.error,
        })
    logger.info("─" * 70)
    logger.info("  requested=%d  ran=%d  connected=%d  bot_heard=%d  total_answers=%d  wall=%.1fs",
                n, len(results), connected, bot_heard, answered, wall)
    logger.info("═" * 70)

    out = LOGS_DIR / f"load_sim_{run_id}.json"
    out.write_text(json.dumps({
        "run_id": run_id,
        "summary": {
            "requested": n, "ran": len(results), "connected": connected,
            "bot_heard": bot_heard, "total_answers": answered, "wall_s": round(wall, 1),
        },
        "candidates": rows,
    }, indent=2))
    logger.info("📄 JSON report: %s", out)


def main():
    ap = argparse.ArgumentParser(description="AIRA end-to-end load simulation")
    ap.add_argument("--n", type=int, default=5, help="number of concurrent interviews")
    ap.add_argument("--max-answers", type=int, default=12, help="answers per candidate before leaving")
    ap.add_argument("--max-duration", type=float, default=900.0, help="hard per-candidate cap (s)")
    ap.add_argument("--bot-warmup", type=float, default=20.0, help="seconds to wait for bots before candidates join")
    ap.add_argument("--questions", type=int, default=0,
                    help="bot questions_per_round (interview_config); 0 = match --max-answers")
    ap.add_argument("--answer-seconds", type=float, default=15.0,
                    help="cap each candidate answer to N seconds for faster sims (0 = full clip)")
    ap.add_argument("--seed", action="store_true", help="seed the DB with interviews first")
    ap.add_argument("--no-engine", action="store_true", help="don't start the engine; I'll run it myself")
    args = ap.parse_args()

    asyncio.run(run(
        n=args.n, max_answers=args.max_answers, max_duration=args.max_duration,
        start_engine_flag=not args.no_engine, seed=args.seed, bot_warmup_s=args.bot_warmup,
        questions=args.questions, answer_seconds=args.answer_seconds,
    ))


if __name__ == "__main__":
    main()
