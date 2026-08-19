#!/usr/bin/env python3
"""
reset_interview.py — Reset N interviews with fresh meeting links.

Usage:
    python reset_interview.py           # prompts for count
    python reset_interview.py 3         # reset 3 interviews (no prompt)

What it does:
    1. Asks how many interviews to reset (or takes count from CLI arg)
    2. Fetches the N most-recent interviews from the DB
    3. Creates a fresh meeting room on Connect for each one
    4. Updates DB with new room_id, URLs, status = Not Started
    5. Prints a clean table of candidate join URLs
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import os, sys, json, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Config ──────────────────────────────────────────────────────────────────
CONNECT_URL = os.getenv("CONNECT_BASE_URL", "http://localhost:3001")
API_KEY     = os.getenv("ADMIN_API_KEY", "dev-airakey1234")
IST         = timezone(timedelta(hours=5, minutes=30))
# Match the orchestrator: it claims interviews by comparing interview_time
# against "now" in JOBDIVA_TIMEZONE (America/New_York). Write interview_time in
# this SAME zone and stamp interview_timezone to match, so claims are correct
# year-round (DST-safe via ZoneInfo, not a hardcoded UTC offset).
INTERVIEW_TZ = os.getenv("JOBDIVA_TIMEZONE", "America/New_York")

# ── DB layer — LOCAL uses direct pymysql; HUB API uses aira_sql DAOs ──────────
# aira_sql._IS_LOCAL is True on Mac (direct DB) and False on deployments that
# talk to the Java hub. We honour that same switch here so reset works in both.
import pymysql
import integrations.aira_sql as _aira_sql  # DB_CONFIG (local) + DAOs (_IS_LOCAL, update_interview, get_all_interviews)

_IS_LOCAL = _aira_sql._IS_LOCAL

# Manifest written by seed_abs_interviews.py — used in HUB-API mode where the
# hub has no working "list interviews" endpoint.
MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "seeded_abs_interviews.json")

def _load_seed_manifest():
    """Return the list of {id, candidate_name} seeded interviews, or []."""
    try:
        with open(MANIFEST_PATH) as f:
            return (json.load(f) or {}).get("interviews", [])
    except FileNotFoundError:
        return []
    except Exception:
        return []

def get_db():
    """Returns a PyMySQL connection using the same DB_CONFIG as aira_sql/orchestrator."""
    return pymysql.connect(**_aira_sql.DB_CONFIG)

def fetch_interviews(n: int):
    """Return the N most-recent interviews (any status) with candidate name.

    Returns a list of dicts with keys: id, interview_status, candidate_name.
    LOCAL → direct SQL; HUB API → GET /interviews via aira_sql.get_all_interviews.
    """
    if _IS_LOCAL:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    i.id,
                    i.interview_status,
                    i.room_id,
                    c.candidate_name,
                    i.candidate_id,
                    i.job_id
                FROM interviews i
                JOIN candidates c ON c.id = i.candidate_id
                ORDER BY i.id DESC
                LIMIT %s
            """, (n,))
            rows = cur.fetchall()
        conn.close()
        return list(rows)

    # ── HUB API ──: the hub has no working "list interviews" endpoint
    # (GET /interviews 500s; POST /interviews/claim mutates rows). Read the
    # manifest that seed_abs_interviews.py wrote instead, so we reset exactly
    # the seeded abs interviews. Newest first (highest id).
    manifest = _load_seed_manifest()
    if not manifest:
        raise RuntimeError(
            "No seed manifest found (seeded_abs_interviews.json). In HUB-API "
            "mode, run  python seed_abs_interviews.py  first — the hub has no "
            "list-interviews endpoint to fall back on."
        )
    items = sorted(manifest, key=lambda iv: iv.get("id") or 0, reverse=True)[:n]
    return [{
        "id":               iv.get("id"),
        "interview_status": "seeded",
        "candidate_name":   iv.get("candidate_name") or f"abs-{iv.get('id')}",
    } for iv in items]

def update_interview_db(interview_id: int, room_id: str,
                        bot_url: str, candidate_url: str, recruiter_url: str,
                        sentinel_url: str = ""):
    # Orchestrator claims interviews where interview_time <= beforeTime, computed
    # in JOBDIVA_TIMEZONE. Write interview_time in that SAME zone (DST-correct)
    # and stamp interview_timezone so the two never disagree.
    now_dt = datetime.now(ZoneInfo(INTERVIEW_TZ)) - timedelta(minutes=2)
    now_et = now_dt.strftime("%Y-%m-%d %H:%M:%S")   # MySQL DATETIME literal (local)

    if not _IS_LOCAL:
        # ── HUB API ──: PUT /interview/update via the same DAO the engine uses.
        # Hub parses interview_time as a Java LocalDateTime → use the "T"
        # separator, no tz offset, no microseconds.
        _aira_sql.update_interview(interview_id, {
            "room_id":            room_id,
            "bot_join_url":       bot_url,
            "candidate_join_url": candidate_url,
            "recruiter_join_url": recruiter_url,
            "sentinel_url":       sentinel_url,
            "interview_status":   "Not Started",
            "interview_time":     now_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            "interview_timezone": INTERVIEW_TZ,
        })
        return

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE interviews
            SET room_id            = %s,
                bot_join_url       = %s,
                candidate_join_url = %s,
                recruiter_join_url = %s,
                sentinel_url       = %s,
                interview_status   = 'Not Started',
                interview_time     = %s,
                interview_timezone = %s,
                updated_at         = NOW()
            WHERE id = %s
        """, (room_id, bot_url, candidate_url, recruiter_url,
               sentinel_url, now_et, INTERVIEW_TZ, interview_id))
        updated = cur.rowcount
    conn.commit()
    conn.close()
    if updated == 0:
        raise RuntimeError(f"UPDATE matched 0 rows for interview_id={interview_id} — check DB connection")

# ── Create one meeting on Connect ────────────────────────────────────────────
def create_meeting(candidate_name: str):
    start_iso = (datetime.now(ZoneInfo(INTERVIEW_TZ)) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.00")
    resp = requests.post(
        f"{CONNECT_URL}/api/meetings/schedule",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={
            "start_iso"        : start_iso,
            "duration_minutes" : 60,
            "tz"               : INTERVIEW_TZ,
            "title"            : "AIRA Interview",
            "candidate_name"   : candidate_name,
            "recruiter_name"   : "AIRA Recruiter",
            "bot_name"         : "AIRA",
            "observer_name"    : "AIRA Sentinel",
            "roles"            : ["candidate", "recruiter", "bot", "Sentinel"],
        },
        timeout=15,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"HTTP : {resp.text[:300]}")
    return resp.json()

def add_livekit_param(url: str) -> str:
    if "livekit=" not in url:
        sep = "&" if "?" in url else "?"
        return url + f"{sep}livekit=1"
    return url

def reset_one(interview_id: int, candidate_name: str) -> dict:
    """
    Schedule a fresh meeting and update the DB for one interview.
    Returns dict with room_id, candidate_url, bot_url, recruiter_url.
    Used by test_06_full_simulation.py and any programmatic caller.
    """
    data          = create_meeting(candidate_name)
    roles         = data.get("roles", {})
    room_id       = data.get("roomId", "")
    candidate_url = add_livekit_param(roles.get("candidate", {}).get("joinUrl", ""))
    bot_url       = roles.get("bot",       {}).get("joinUrl", "")
    recruiter_url = roles.get("recruiter", {}).get("joinUrl", "")
    sentinel_url  = roles.get("Sentinel",  {}).get("joinUrl", "") \
                 or roles.get("sentinel",  {}).get("joinUrl", "")

    update_interview_db(interview_id, room_id, bot_url, candidate_url,
                        recruiter_url, sentinel_url)

    return {
        "room_id"      : room_id,
        "candidate_url": candidate_url,
        "bot_url"      : bot_url,
        "recruiter_url": recruiter_url,
        "sentinel_url" : sentinel_url,
    }

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Determine count
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            print("❌ Usage: python reset_interview.py [number]")
            sys.exit(1)
    else:
        try:
            raw = input("How many interviews do you want to reset? ").strip()
            count = int(raw)
        except (ValueError, KeyboardInterrupt):
            print("\n❌ Invalid number. Exiting.")
            sys.exit(1)

    if count <= 0:
        print("❌ Must be at least 1.")
        sys.exit(1)

    print(f"\n🔍 Fetching {count} most-recent interview(s) from DB...")
    interviews = fetch_interviews(count)

    if not interviews:
        print("❌ No interviews found in DB.")
        sys.exit(1)

    if len(interviews) < count:
        print(f"⚠️  Only {len(interviews)} interview(s) found — resetting all of them.")
        count = len(interviews)

    # Show preview and reset immediately
    print(f"\n{'─'*60}")
    print(f"  {'#':<4} {'ID':<8} {'Candidate':<22} {'Current Status'}")
    print(f"{'─'*60}")
    for i, iv in enumerate(interviews, 1):
        print(f"  {i:<4} {iv['id']:<8} {iv['candidate_name']:<22} {iv['interview_status']}")
    print(f"{'─'*60}")

    # Reset each
    print(f"\n🔄 Creating fresh meeting links on {CONNECT_URL}...\n")
    results = []

    for i, iv in enumerate(interviews, 1):
        iid  = iv["id"]
        name = iv["candidate_name"]
        try:
            data = create_meeting(name)

            roles         = data["roles"]
            room_id       = data["roomId"]
            bot_url       = roles["bot"]["joinUrl"]
            candidate_url = add_livekit_param(roles["candidate"]["joinUrl"])
            recruiter_url = roles["recruiter"]["joinUrl"]
            sentinel_url  = roles.get("Sentinel", {}).get("joinUrl", "") \
                         or roles.get("sentinel", {}).get("joinUrl", "")

            update_interview_db(iid, room_id, bot_url, candidate_url,
                                recruiter_url, sentinel_url)

            results.append({
                "num"       : i,
                "id"        : iid,
                "name"      : name,
                "room_id"   : room_id,
                "candidate" : candidate_url,
                "ok"        : True,
            })
            print(f"  ✅ [{i}/{count}] Interview {iid} — {name} → {room_id}")

        except Exception as e:
            results.append({"num": i, "id": iid, "name": name, "ok": False, "error": str(e)})
            print(f"  ❌ [{i}/{count}] Interview {iid} — {name} → FAILED: {e}")

    # Summary
    ok  = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]

    print(f"\n{'═'*70}")
    print(f"  RESET COMPLETE — {len(ok)}/{count} succeeded")
    print(f"{'═'*70}\n")

    if ok:
        print("📋 CANDIDATE JOIN URLS (open each in a separate browser tab):\n")
        for r in ok:
            print(f"  [{r['num']}] {r['name']} (ID {r['id']})")
            print(f"      {r['candidate']}")
            print()

    if bad:
        print("❌ FAILED:")
        for r in bad:
            print(f"  Interview {r['id']} — {r['name']}: {r['error']}")

    print(f"{'═'*70}")

if __name__ == "__main__":
    main()
