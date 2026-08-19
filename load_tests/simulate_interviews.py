#!/usr/bin/env python3
"""
simulate_interviews.py  —  MASTER SIMULATION FILE
=====================================================

Picks N interviews from the DB (seeded by seed_db.py), resets them with
fresh LiveKit room tokens via the Connect server, then simultaneously
simulates N candidates joining their assigned rooms.

When aira_3_webrtc_orchestration.py is running, the real AIRA bot
joins each room and the full interview pipeline executes.

HOW TO USE
----------

Step 0 — Seed the DB (one time):
    python -m load_tests.seed_db

Step 1 — Start the AIRA engine:
    conda activate aira_env
    AIRA_MAX_PARALLEL=20 python aira_3_webrtc_orchestration.py

Step 2 — Run simulation (separate terminal):
    # 10 interviews — picks any 10 seeded rows
    python -m load_tests.simulate_interviews --n 10

    # 50 interviews — technical jobs only
    python -m load_tests.simulate_interviews --n 50 --type technical

    # 10 non-technical interviews, ramp 2s between each
    python -m load_tests.simulate_interviews --n 10 --type non_technical --ramp 2

    # Preview without connecting
    python -m load_tests.simulate_interviews --n 20 --dry-run

    # Pick a specific job
    python -m load_tests.simulate_interviews --n 10 --job hr_manager

FLAGS
-----
--n          Number of interviews to run (default: 10)
--type       Filter by job type: technical | non_technical | leadership | any (default: any)
--job        Filter by job key e.g. python_ml, hr_manager, sales_executive
--duration   Seconds of audio per candidate (default: 300 = 5 min)
--ramp       Seconds between each candidate launch (0 = all at once)
--dry-run    Print plan without touching DB or LiveKit
--no-reset   Skip room reset — reuse existing URLs (useful for retries)
"""

import asyncio, argparse, json, logging, os, random, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import pymysql
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(override=True)

from livekit import rtc
from livekit.api import AccessToken, VideoGrants

from load_tests.config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME,
    LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET,
    AUDIO_SAMPLE_RATE, AUDIO_CHANNELS, REPORTS_DIR,
)
from load_tests.audio_gen import stream_speech_like_to_source

IST          = timezone(timedelta(hours=5, minutes=30))
CONNECT_URL  = os.getenv("CONNECT_BASE_URL", os.getenv("AIRA_BASE_URL", "http://localhost:3001"))
API_KEY      = os.getenv("ADMIN_API_KEY", "dev-airakey1234")
SEED_INDEX   = Path(__file__).parent / "seed_index.json"

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SIM] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(REPORTS_DIR / "simulate_interviews.log", mode="w"),
    ]
)
log = logging.getLogger("sim")


# ═══════════════════════════════════════════════════════════════════════════════
# Data class
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class SlotResult:
    idx:            int
    interview_id:   int
    candidate_name: str
    job_title:      str
    job_type:       str
    room_id:        str    = ""
    candidate_url:  str    = ""
    # Timing
    reset_ms:       float  = 0.0
    connect_ms:     float  = 0.0
    publish_ms:     float  = 0.0
    total_s:        float  = 0.0
    # Outcomes
    reset_ok:       bool   = False
    connected:      bool   = False
    published:      bool   = False
    bot_joined:     bool   = False
    bot_spoke:      bool   = False
    error:          Optional[str] = None
    timeline:       list   = field(default_factory=list)

    def log(self, msg: str):
        ts = datetime.utcnow().strftime("%H:%M:%S")
        self.timeline.append(f"{ts} {msg}")
        log.info("[iv=%-5d %-20s] %s", self.interview_id, self.candidate_name[:20], msg)


# ═══════════════════════════════════════════════════════════════════════════════
# DB helpers
# ═══════════════════════════════════════════════════════════════════════════════
def get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def fetch_interviews(n: int, job_type: str = "any", job_key: str = "") -> List[dict]:
    """Pull N interview rows from the seeded pool."""
    # Load the seed index
    if not SEED_INDEX.exists():
        raise FileNotFoundError(
            f"seed_index.json not found. Run first:\n"
            f"  python -m load_tests.seed_db"
        )
    pool = json.loads(SEED_INDEX.read_text())

    # Filter
    if job_key:
        pool = [r for r in pool if r["job_key"] == job_key]
    elif job_type != "any":
        pool = [r for r in pool if r["job_type"] == job_type]

    if not pool:
        raise ValueError(
            f"No interviews match type={job_type!r} job={job_key!r}. "
            "Run seed_db.py again or adjust filters."
        )

    # Shuffle so we get variety across runs
    random.shuffle(pool)
    return pool[:n]


def reset_interview_in_db(cur, interview_id: int, room: dict):
    """Reset one interview row with fresh URLs and 'Not Started' status."""
    cur.execute(
        """UPDATE interviews SET
           interview_status       = 'Not Started',
           interview_score        = 0.0,
           interview_analysis     = NULL,
           interview_verdict      = NULL,
           interview_questions    = NULL,
           candidate_present      = 1,
           current_round_number   = 1,
           number_of_tabs_switched = 0,
           sentinel_analysis      = NULL,
           recording_enabled      = 1,
           room_id                = %s,
           bot_join_url           = %s,
           candidate_join_url     = %s,
           recruiter_join_url     = %s,
           sentinel_url           = %s,
           updated_at             = NOW()
           WHERE id = %s""",
        (
            room["room_id"], room["bot_url"],
            room["candidate_url"], room["recruiter_url"],
            room["sentinel_url"], interview_id,
        )
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Connect server: create fresh meeting room
# ═══════════════════════════════════════════════════════════════════════════════
def create_meeting(candidate_name: str, interview_id: int) -> dict:
    """POST /api/meetings/schedule — get fresh room_id + JWT URLs."""
    start_iso = (
        datetime.now(IST) - timedelta(minutes=1)
    ).strftime("%Y-%m-%dT%H:%M:%S.00")

    resp = requests.post(
        f"{CONNECT_URL}/api/meetings/schedule",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={
            "start_iso":        start_iso,
            "duration_minutes": 60,
            "tz":               "Asia/Kolkata",
            "title":            f"AIRA Sim #{interview_id} — {candidate_name}",
            "candidate_name":   candidate_name,
            "recruiter_name":   "SimRecruiter",
            "bot_name":         "AIRA Bot",
            "observer_name":    "AIRA Sentinel",
        },
        timeout=15,
    )
    if resp.status_code != 201:
        raise RuntimeError(
            f"Meeting creation failed ({resp.status_code}): {resp.text[:200]}"
        )

    data = resp.json()
    candidate_url = data["roles"]["candidate"]["joinUrl"]
    # Make sure livekit=1 flag is present (triggers LiveKit path in frontend)
    if "livekit=" not in candidate_url:
        sep = "&" if "?" in candidate_url else "?"
        candidate_url += f"{sep}livekit=1"

    return {
        "room_id":       data["roomId"],
        "bot_url":       data["roles"]["bot"]["joinUrl"],
        "candidate_url": candidate_url,
        "recruiter_url": data["roles"].get("recruiter", {}).get("joinUrl", ""),
        "sentinel_url":  data["roles"].get("observer",  {}).get("joinUrl", ""),
    }


def extract_livekit_token(candidate_url: str) -> str:
    """Extract the LiveKit JWT from the candidate join URL."""
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(candidate_url).query)
    token = qs.get("token", qs.get("t", [None]))[0]
    if not token:
        raise ValueError(f"No token in URL: {candidate_url[:120]}")
    return token


def _local_candidate_token(room_id: str, candidate_name: str) -> str:
    """Mint a candidate LiveKit token locally with the .env API key/secret — the key the
    LiveKit server actually accepts. Used when SIM_LOCAL_TOKEN=1 to bypass a Connect server
    that signs tokens with a different/stale LiveKit key. Identity must NOT start with 'bot'
    so the engine's native presence detection classifies it as the candidate."""
    at = (AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
          .with_identity(f"candidate-{room_id}")
          .with_name(candidate_name or "Candidate")
          .with_metadata('{"role":"candidate"}')
          .with_grants(VideoGrants(room_join=True, room=room_id,
                                   can_publish=True, can_subscribe=True, can_publish_data=True)))
    return at.to_jwt()


# ═══════════════════════════════════════════════════════════════════════════════
# Single candidate simulation
# ═══════════════════════════════════════════════════════════════════════════════
async def simulate_candidate(slot: SlotResult, duration_s: float) -> SlotResult:
    t_total = time.perf_counter()

    # ── 1. Join LiveKit using the candidate token ──────────────────────
    try:
        if os.getenv("SIM_LOCAL_TOKEN", "").lower() in ("1", "true", "yes"):
            token = _local_candidate_token(slot.room_id, slot.candidate_name)
            slot.log("🔑 using locally-minted candidate token (.env LiveKit key)")
        else:
            token = extract_livekit_token(slot.candidate_url)
    except Exception as e:
        slot.error = f"Token extract error: {e}"
        slot.log(f"❌ {slot.error}")
        slot.total_s = time.perf_counter() - t_total
        return slot

    room           = rtc.Room()
    bot_seen       = asyncio.Event()
    bot_audio_seen = asyncio.Event()

    @room.on("participant_connected")
    def on_participant(participant):
        slot.log(f"👤 {participant.identity} joined")
        if "bot" in participant.identity.lower():
            slot.bot_joined = True
            bot_seen.set()

    @room.on("track_subscribed")
    def on_track(track, pub, participant):
        if "bot" in participant.identity.lower() and str(track.kind) == "audio":
            slot.bot_spoke = True
            bot_audio_seen.set()
            slot.log("🔈 Bot audio heard!")

    @room.on("disconnected")
    def on_disc(reason):
        slot.log(f"🔌 Disconnected ({reason})")

    t0 = time.perf_counter()
    try:
        await room.connect(LIVEKIT_URL, token)
        slot.connect_ms = (time.perf_counter() - t0) * 1000
        slot.connected  = True
        slot.log(f"✅ Connected in {slot.connect_ms:.0f}ms | room={slot.room_id[-12:]}")
    except Exception as e:
        slot.error = f"Connect error: {e}"
        slot.log(f"❌ {slot.error}")
        slot.total_s = time.perf_counter() - t_total
        return slot

    # ── 2. Publish mic track ───────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        audio_src = rtc.AudioSource(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        mic       = rtc.LocalAudioTrack.create_audio_track("mic", audio_src)
        await room.local_participant.publish_track(mic)
        slot.publish_ms = (time.perf_counter() - t0) * 1000
        slot.published  = True
        slot.log(f"🎤 Mic published in {slot.publish_ms:.0f}ms")
    except Exception as e:
        slot.error = f"Publish error: {e}"
        slot.log(f"❌ {slot.error}")
        await room.disconnect()
        slot.total_s = time.perf_counter() - t_total
        return slot

    # ── 3. Stream audio + wait for bot events ─────────────────────────
    slot.log(f"🔊 Streaming {duration_s:.0f}s of audio…")
    try:
        audio_task = asyncio.create_task(
            stream_speech_like_to_source(audio_src, duration_s=duration_s)
        )

        # Wait up to 30s for bot to join
        try:
            await asyncio.wait_for(asyncio.shield(bot_seen.wait()), timeout=30.0)
            slot.log("🤖 Bot joined!")
        except asyncio.TimeoutError:
            slot.log("⚠️  Bot not seen after 30s")

        # Wait up to 60s for bot audio
        try:
            await asyncio.wait_for(asyncio.shield(bot_audio_seen.wait()), timeout=60.0)
        except asyncio.TimeoutError:
            slot.log("⚠️  Bot audio not heard within 60s")

        await audio_task
        slot.log("✅ Audio stream complete")

    except Exception as e:
        slot.error = f"Stream error: {e}"
        slot.log(f"❌ {slot.error}")

    # ── 4. Leave ───────────────────────────────────────────────────────
    slot.total_s = time.perf_counter() - t_total
    slot.log(f"👋 Leaving after {slot.total_s:.1f}s")
    try:
        await room.disconnect()
    except Exception:
        pass

    return slot


# ═══════════════════════════════════════════════════════════════════════════════
# Report
# ═══════════════════════════════════════════════════════════════════════════════
def generate_report(slots: List[SlotResult], wall_s: float) -> Path:
    n        = len(slots)
    ok       = [s for s in slots if s.connected and s.published]
    bot_ok   = [s for s in slots if s.bot_joined]
    spoke_ok = [s for s in slots if s.bot_spoke]

    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = REPORTS_DIR / f"sim_n{n}_{ts}.md"

    conn_t = sorted(s.connect_ms for s in ok) or [0]
    def p(lst, pct):
        import math
        i = int(math.ceil(len(lst)*pct/100))-1
        return lst[max(0,i)]

    md = f"""# AIRA Interview Simulation — {n} Candidates
**Date:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  
**Wall time:** {wall_s:.1f}s  |  **Interviews attempted:** {n}

---

## Summary

| Metric | Count | % |
|--------|-------|---|
| Interviews reset (room created) | {sum(1 for s in slots if s.reset_ok)} | {sum(1 for s in slots if s.reset_ok)/n*100:.0f}% |
| Connected to LiveKit | {len(ok)} | {len(ok)/n*100:.0f}% |
| Track published | {sum(1 for s in slots if s.published)} | {sum(1 for s in slots if s.published)/n*100:.0f}% |
| AIRA bot joined room | {len(bot_ok)} | {len(bot_ok)/n*100:.0f}% |
| **AIRA bot spoke** | **{len(spoke_ok)}** | **{len(spoke_ok)/n*100:.0f}%** |

## Connect Latency
| p50 | p90 | p95 | p99 | max |
|-----|-----|-----|-----|-----|
| {p(conn_t,50):.0f}ms | {p(conn_t,90):.0f}ms | {p(conn_t,95):.0f}ms | {p(conn_t,99):.0f}ms | {max(conn_t):.0f}ms |

---

## Per-Candidate Detail

| # | Name | Job | Type | iv_id | Room | Reset | Connected | Bot | Spoke |
|---|------|-----|------|-------|------|-------|-----------|-----|-------|
"""
    for s in sorted(slots, key=lambda x: x.idx):
        rst = "✅" if s.reset_ok   else "❌"
        con = "✅" if s.connected  else "❌"
        bot = "✅" if s.bot_joined else "⏳"
        spk = "✅" if s.bot_spoke  else "🔇"
        md += (f"| {s.idx+1} | {s.candidate_name} | {s.job_title[:20]} | "
               f"{s.job_type[:10]} | {s.interview_id} | "
               f"`{s.room_id[-10:] if s.room_id else 'none'}` | "
               f"{rst} | {con} | {bot} | {spk} |\n")

    errs = [s for s in slots if s.error]
    if errs:
        md += "\n---\n\n## Errors\n\n"
        for s in errs:
            md += f"**{s.candidate_name}** (iv={s.interview_id}): `{s.error}`\n\n"

    md += f"\n---\n*Report: `{path.name}`*\n"
    path.write_text(md)
    path.with_suffix(".json").write_text(json.dumps({
        "n": n, "wall_s": wall_s,
        "results": [asdict(s) for s in slots],
    }, indent=2))
    return path


# ═══════════════════════════════════════════════════════════════════════════════
# Main orchestrator
# ═══════════════════════════════════════════════════════════════════════════════
async def run(n: int, job_type: str, job_key: str, duration_s: float,
              ramp_s: float, dry_run: bool, no_reset: bool, max_conc: int):

    log.info("=" * 65)
    log.info("  AIRA Interview Simulation")
    log.info("  Interviews    : %d", n)
    log.info("  Job type      : %s", job_type)
    log.info("  Audio duration: %.0fs", duration_s)
    log.info("  Ramp          : %.1fs", ramp_s)
    log.info("  Connect URL   : %s", CONNECT_URL)
    log.info("=" * 65)

    # ── Pick interviews from seed pool ─────────────────────────────────
    rows = fetch_interviews(n, job_type=job_type, job_key=job_key)
    if len(rows) < n:
        log.warning("Only %d interviews available for filters — using all", len(rows))
        n = len(rows)

    log.info("\n  Interviews selected:")
    for i, r in enumerate(rows):
        log.info("  [%02d] iv=%-6d %-25s | %s (%s)",
                 i+1, r["interview_id"], r["candidate_name"],
                 r["job_title"][:30], r["job_type"])

    if dry_run:
        log.info("\n[DRY RUN] — no DB writes or LiveKit connections made")
        return []

    # ── Build slot objects ─────────────────────────────────────────────
    slots = [
        SlotResult(
            idx           = i,
            interview_id  = r["interview_id"],
            candidate_name= r["candidate_name"],
            job_title     = r["job_title"],
            job_type      = r["job_type"],
        )
        for i, r in enumerate(rows)
    ]

    # ── Reset interviews (create fresh rooms) ─────────────────────────
    if not no_reset:
        log.info("\n  Resetting %d interviews via Connect server…", n)
        conn = get_conn()
        cur  = conn.cursor()

        for slot in slots:
            t0 = time.perf_counter()
            try:
                room = create_meeting(slot.candidate_name, slot.interview_id)
                reset_interview_in_db(cur, slot.interview_id, room)
                conn.commit()
                slot.room_id      = room["room_id"]
                slot.candidate_url= room["candidate_url"]
                slot.reset_ok     = True
                slot.reset_ms     = (time.perf_counter() - t0) * 1000
                slot.log(f"🔑 Room created: {room['room_id']} ({slot.reset_ms:.0f}ms)")
            except Exception as e:
                conn.rollback()
                slot.error = f"Reset error: {e}"
                slot.log(f"❌ {slot.error}")
            time.sleep(0.2)   # slight stagger to avoid Connect rate-limit

        cur.close()
        conn.close()
    else:
        # Load existing URLs from DB
        conn = get_conn()
        cur  = conn.cursor()
        for slot in slots:
            cur.execute(
                "SELECT room_id, candidate_join_url FROM interviews WHERE id=%s",
                (slot.interview_id,)
            )
            row = cur.fetchone()
            if row and row["room_id"]:
                slot.room_id       = row["room_id"]
                slot.candidate_url = row["candidate_join_url"] or ""
                slot.reset_ok      = True
                slot.log(f"♻️  Reusing existing room: {row['room_id']}")
            else:
                slot.error = "No room_id in DB — run without --no-reset"
                slot.log(f"❌ {slot.error}")
        cur.close()
        conn.close()

    ready = [s for s in slots if s.reset_ok]
    log.info("\n  ✅ %d/%d interviews ready | ❌ %d failed", len(ready), n, n - len(ready))
    if not ready:
        log.error("No interviews ready — aborting")
        return slots

    log.info("\n  ⚠️  Make sure aira_3_webrtc_orchestration.py is running!")
    log.info("  Waiting 3s before launching candidates…\n")
    await asyncio.sleep(3)

    # ── Launch candidates ──────────────────────────────────────────────
    sem = asyncio.Semaphore(max_conc)
    t_wall = time.perf_counter()
    results: List[SlotResult] = []

    async def launch_one(slot: SlotResult):
        async with sem:
            result = await simulate_candidate(slot, duration_s)
            results.append(result)
            flag = "✅" if result.connected and result.published else "❌"
            bot  = "🤖✅" if result.bot_joined else "🤖❌"
            spk  = "🔈✅" if result.bot_spoke  else "🔈❌"
            log.info("%s [%02d/%-2d] %-25s | room=%s | con=%dms | %s %s",
                     flag, result.idx+1, n, result.candidate_name[:25],
                     result.room_id[-10:] if result.room_id else "none",
                     result.connect_ms, bot, spk)

    if ramp_s > 0:
        tasks = []
        for slot in ready:
            tasks.append(asyncio.create_task(launch_one(slot)))
            await asyncio.sleep(ramp_s)
        await asyncio.gather(*tasks)
    else:
        await asyncio.gather(*[launch_one(s) for s in ready])

    wall_s = time.perf_counter() - t_wall
    results.sort(key=lambda r: r.idx)

    # ── Print summary ──────────────────────────────────────────────────
    ok    = [r for r in results if r.connected and r.published]
    bots  = [r for r in results if r.bot_joined]
    spoke = [r for r in results if r.bot_spoke]

    log.info("\n%s", "=" * 65)
    log.info("  SIMULATION COMPLETE")
    log.info("  Connected    : %d/%d  (%.0f%%)", len(ok),    n, len(ok)/n*100)
    log.info("  Bot joined   : %d/%d  (%.0f%%)", len(bots),  n, len(bots)/n*100)
    log.info("  Bot spoke    : %d/%d  (%.0f%%)", len(spoke), n, len(spoke)/n*100)
    log.info("  Wall time    : %.1fs", wall_s)

    report = generate_report(results, wall_s)
    log.info("  Report       : %s", report)
    log.info("=" * 65)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(
        description="Simulate N candidates joining real AIRA interviews",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # 10 mixed interviews
  python -m load_tests.simulate_interviews --n 10

  # 50 technical interviews all at once
  python -m load_tests.simulate_interviews --n 50 --type technical

  # 20 non-technical, ramp 1 per second
  python -m load_tests.simulate_interviews --n 20 --type non_technical --ramp 1

  # Specific job
  python -m load_tests.simulate_interviews --n 10 --job hr_manager

  # Preview
  python -m load_tests.simulate_interviews --n 30 --dry-run
        """
    )
    ap.add_argument("--n",        type=int,   default=10)
    ap.add_argument("--type",     default="any",
                    choices=["any","technical","non_technical","leadership"],
                    dest="job_type")
    ap.add_argument("--job",      default="",  dest="job_key",
                    help="Specific job key e.g. python_ml, hr_manager, sales_executive")
    ap.add_argument("--duration", type=float, default=300,
                    help="Audio duration per candidate in seconds (default 300)")
    ap.add_argument("--ramp",     type=float, default=0,
                    help="Seconds between each candidate launch (0=all at once)")
    ap.add_argument("--max-conc", type=int,   default=50,
                    dest="max_conc",
                    help="Max simultaneous LiveKit connections (default 50)")
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--no-reset", action="store_true",
                    help="Skip room reset — reuse existing DB URLs")
    args = ap.parse_args()

    asyncio.run(run(
        n          = args.n,
        job_type   = args.job_type,
        job_key    = args.job_key,
        duration_s = args.duration,
        ramp_s     = args.ramp,
        dry_run    = args.dry_run,
        no_reset   = args.no_reset,
        max_conc   = args.max_conc,
    ))


if __name__ == "__main__":
    main()
