#!/usr/bin/env python3
"""
reset_interviews.py — Seed 10 test candidates + 10 interviews (all on job_id=142),
                       create fresh meeting links for each, and print join URLs.

Usage:
    conda run -n aira_env python reset_interviews.py

What it does:
    1. Inserts (or reuses) 10 test candidates in the candidates table
    2. Inserts (or resets) 10 interviews linked to job_id=142, copying
       all config from the reference interview id=6090
    3. Calls POST /api/meetings/schedule on Connect for each interview
       to mint fresh JWT tokens + room_id
    4. Updates each interview with the fresh URLs + status "Not Started"
    5. Prints a summary table of all candidate join URLs

Run this before each load test to get a clean slate.
"""

import os, time, json, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import pymysql

load_dotenv(override=True)

# ── Config ─────────────────────────────────────────────────────────────────────
CONNECT_URL   = os.getenv("CONNECT_BASE_URL", "http://localhost:3001")
API_KEY       = os.getenv("ADMIN_API_KEY", "dev-airakey1234")
JOB_ID        = 142          # same job as interview 6090
RECRUITER_ID  = 3
RECRUITER_EMAIL = "Sridhar.k1@nrconsulting.com"
EMAIL_CONFIG  = '["hemanth.nalabolu@airahr.ai"]'
INTERVIEW_CONFIG = json.dumps({
    "base_level": "easy",
    "total_rounds": 3,
    "pass_threshold_pct": 50,
    "questions_per_round": int(os.getenv("N_QUESTIONS", "5")),
    "max_duration_minutes": 20,
    "role_experience_required": "junior"
})
MANDATORY_SKILLS  = "python, pytorch, langchain, langgraph"
OPTIONAL_SKILLS   = (
    "database management systems (MySQL or PostgreSQL), "
    "object-oriented programming (OOP) concepts and principles, "
    "Eclipse or IntelliJ IDEA integrated development environments (IDEs), "
    "Git version control system, software development life cycle (SDLC) "
    "and agile development methodologies"
)

# 10 test candidates — realistic names with unique JD IDs
TEST_CANDIDATES = [
    {"name": "Arjun Sharma",    "email": "arjun.sharma.test@airahr.ai",    "jd_id": "19190264839701"},
    {"name": "Priya Nair",      "email": "priya.nair.test@airahr.ai",      "jd_id": "19190264839702"},
    {"name": "Karan Mehta",     "email": "karan.mehta.test@airahr.ai",     "jd_id": "19190264839703"},
    {"name": "Divya Rao",       "email": "divya.rao.test@airahr.ai",       "jd_id": "19190264839704"},
    {"name": "Rohit Verma",     "email": "rohit.verma.test@airahr.ai",     "jd_id": "19190264839705"},
    {"name": "Sneha Iyer",      "email": "sneha.iyer.test@airahr.ai",      "jd_id": "19190264839706"},
    {"name": "Aditya Kumar",    "email": "aditya.kumar.test@airahr.ai",    "jd_id": "19190264839707"},
    {"name": "Meera Pillai",    "email": "meera.pillai.test@airahr.ai",    "jd_id": "19190264839708"},
    {"name": "Vikram Singh",    "email": "vikram.singh.test@airahr.ai",    "jd_id": "19190264839709"},
    {"name": "Anjali Reddy",    "email": "anjali.reddy.test@airahr.ai",    "jd_id": "19190264839710"},
]

RESUME_TEXT = """Test Candidate — Load Test Profile

Location: Hyderabad (Open to relocate)
Professional Summary:
Python developer with experience in machine learning frameworks including PyTorch,
LangChain, and LangGraph. Familiar with MySQL, Git, and agile methodologies.

Skills: Python, PyTorch, LangChain, LangGraph, MySQL, Git, OOP, Agile

Experience:
- Junior Python Developer | TechCorp | 2023–Present
  Built NLP pipelines using LangChain and PyTorch.

Education:
- B.Tech Computer Science | JNTU Hyderabad | 2023
"""

RESUME_JOB_ANALYSIS = (
    "Key qualifications found: The candidate is a Junior Python Developer with "
    "experience in PyTorch, LangChain, and LangGraph. Strong match for the role."
)

NEW_YORK = ZoneInfo("America/New_York")

# ── DB connection ───────────────────────────────────────────────────────────────
def get_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", 3306)),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASS"),
        database=os.getenv("MYSQL_DB"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def upsert_candidate(cur, cand: dict) -> int:
    """Insert candidate if not exists (match on email). Returns candidate id."""
    cur.execute(
        "SELECT id FROM candidates WHERE candidate_email = %s",
        (cand["email"],)
    )
    existing = cur.fetchone()
    if existing:
        return existing["id"]

    cur.execute(
        """INSERT INTO candidates
           (candidate_jd_id, candidate_name, candidate_phone, candidate_email, resume_text)
           VALUES (%s, %s, NULL, %s, %s)""",
        (cand["jd_id"], cand["name"], cand["email"], RESUME_TEXT)
    )
    return cur.lastrowid


def upsert_interview(cur, candidate_id: int, candidate_name: str, jd_id: str, room: dict) -> int:
    """
    Find existing test interview for this candidate+job, or insert a new one.
    Returns interview id.
    """
    now_ny = (datetime.now(NEW_YORK) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        "SELECT id FROM interviews WHERE candidate_id=%s AND job_id=%s",
        (candidate_id, JOB_ID)
    )
    existing = cur.fetchone()
    if existing:
        iid = existing["id"]
        # Reset it fully — fresh URLs, clean slate
        cur.execute(
            """UPDATE interviews SET
               interview_status='Not Started',
               interview_score=0.0,
               interview_analysis=NULL,
               interview_verdict=NULL,
               interview_questions=NULL,
               candidate_present=NULL,
               current_round_number=1,
               number_of_tabs_switched=0,
               sentinel_analysis=NULL,
               interview_time=%s,
               interview_timezone='America/New_York',
               zone='America/New_York',
               room_id=%s,
               bot_join_url=%s,
               candidate_join_url=%s,
               recruiter_join_url=%s,
               sentinel_url=%s,
               updated_at=NOW()
               WHERE id=%s""",
            (
                now_ny,
                room["room_id"], room["bot_url"],
                room["candidate_url"], room["recruiter_url"],
                room["sentinel_url"], iid,
            )
        )
        return iid

    # Insert new — room_id is NOT NULL so must be provided at insert time
    cur.execute(
        """INSERT INTO interviews (
            candidate_id, job_id, recruiter_id, recruiter_email,
            resume_text, resume_job_analysis, resume_match_score,
            interview_time, interview_timezone,
            interview_status, interview_score,
            is_scheduled, is_email_sent, is_jd_interview_updated,
            mandatory_skills, has_mandatory_skills,
            optional_skills, interview_config, interview_mode,
            zone, recording_enabled, sentinel_enabled,
            email_config, round_type, current_round_number,
            number_of_tabs_switched,
            room_id, bot_join_url, candidate_join_url,
            recruiter_join_url, sentinel_url,
            created_at, updated_at
           ) VALUES (
            %s, %s, %s, %s,
            %s, %s, 45.0,
            %s, 'America/New_York',
            'Not Started', 0.0,
            1, 1, 1,
            %s, 1,
            %s, %s, 'Connect',
            'America/New_York', 1, 1,
            %s, 'technical', 1,
            0,
            %s, %s, %s,
            %s, %s,
            NOW(), NOW()
           )""",
        (
            candidate_id, JOB_ID, RECRUITER_ID, RECRUITER_EMAIL,
            RESUME_TEXT, RESUME_JOB_ANALYSIS,
            now_ny,
            MANDATORY_SKILLS,
            OPTIONAL_SKILLS, INTERVIEW_CONFIG,
            EMAIL_CONFIG,
            room["room_id"], room["bot_url"], room["candidate_url"],
            room["recruiter_url"], room["sentinel_url"],
        )
    )
    return cur.lastrowid


def create_meeting(candidate_name: str, idx: int) -> dict:
    """Call Connect /api/meetings/schedule. Returns {room_id, bot_url, candidate_url}."""
    start_iso = (datetime.now(NEW_YORK) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.00")
    resp = requests.post(
        f"{CONNECT_URL}/api/meetings/schedule",
        headers={"X-API-Key": API_KEY, "Content-Type": "application/json"},
        json={
            "start_iso": start_iso,
            "duration_minutes": 60,
            "tz": "America/New_York",
            "title": f"AIRA Load Test {idx+1} — {candidate_name}",
            "candidate_name": candidate_name,
            "recruiter_name": "LoadTest",
            "bot_name": "AIRA Bot",
            "observer_name": "AIRA Sentinel",
        },
        timeout=15,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"Meeting creation failed ({resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    candidate_url = data["roles"]["candidate"]["joinUrl"]
    if "livekit=" not in candidate_url:
        sep = "&" if "?" in candidate_url else "?"
        candidate_url += f"{sep}livekit=1"

    return {
        "room_id":       data["roomId"],
        "bot_url":       data["roles"]["bot"]["joinUrl"],
        "candidate_url": candidate_url,
        "recruiter_url": data["roles"].get("recruiter", {}).get("joinUrl", ""),
        "sentinel_url":  data["roles"].get("observer", {}).get("joinUrl", ""),
    }


def update_interview_urls(cur, interview_id: int, room: dict):
    cur.execute(
        """UPDATE interviews SET
           room_id=%s,
           bot_join_url=%s,
           candidate_join_url=%s,
           recruiter_join_url=%s,
           sentinel_url=%s,
           interview_status='Not Started',
           updated_at=NOW()
           WHERE id=%s""",
        (
            room["room_id"], room["bot_url"],
            room["candidate_url"], room["recruiter_url"],
            room["sentinel_url"], interview_id,
        )
    )


def reset_stale_interviews(cur, conn):
    """Reset any interviews stuck in Queued/InProgress so they get re-claimed."""
    cur.execute("""
        UPDATE interviews
        SET interview_status = 'Not Started'
        WHERE candidate_id IN (
            SELECT id FROM candidates WHERE candidate_email LIKE '%test@airahr.ai'
        )
        AND interview_status IN ('Queued', 'InProgress-Initiated', 'InProgress')
    """)
    if cur.rowcount:
        print(f"♻️  Reset {cur.rowcount} stale interview(s) back to Not Started")
    conn.commit()


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    cands = TEST_CANDIDATES[:int(os.getenv("N_INTERVIEWS", str(len(TEST_CANDIDATES))))]
    print("=" * 70)
    print("🚀 AIRA Load Test — Interview Seeder")
    print(f"   Job ID : {JOB_ID}")
    print(f"   Count  : {len(cands)} interviews")
    print(f"   Connect: {CONNECT_URL}")
    print("=" * 70)
    print()

    conn = get_conn()
    cur  = conn.cursor()

    # Clear any stale Queued/InProgress from previous runs first
    reset_stale_interviews(cur, conn)

    results = []

    for idx, cand in enumerate(cands):
        print(f"[{idx+1:02d}/{len(cands)}] {cand['name']} ... ", end="", flush=True)

        try:
            # 1. Upsert candidate
            candidate_id = upsert_candidate(cur, cand)
            conn.commit()   # commit candidate first (safe to re-use on retry)

            # 2. Create meeting first (room_id is NOT NULL — must exist at INSERT time)
            room = create_meeting(cand["name"], idx)

            # 3. Upsert/reset interview with URLs already populated
            interview_id = upsert_interview(cur, candidate_id, cand["name"], cand["jd_id"], room)

            conn.commit()

            results.append({
                "idx":          idx + 1,
                "name":         cand["name"],
                "candidate_id": candidate_id,
                "interview_id": interview_id,
                "room_id":      room["room_id"],
                "candidate_url": room["candidate_url"],
            })

            print(f"✅  interview_id={interview_id}  room={room['room_id']}")

        except Exception as e:
            conn.rollback()
            print(f"❌  ERROR: {e}")
            results.append({
                "idx": idx + 1, "name": cand["name"],
                "error": str(e)
            })

        # Slight stagger so Connect doesn't rate-limit
        time.sleep(0.3)

    cur.close()
    conn.close()

    # ── Summary ────────────────────────────────────────────────────────────────
    ok  = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]

    print()
    print("=" * 70)
    print(f"✅ {len(ok)} interviews ready  |  ❌ {len(err)} failed")
    print("=" * 70)
    print()
    print("📋 CANDIDATE JOIN URLS (open each in a browser tab):")
    print()
    for r in ok:
        print(f"  [{r['idx']:02d}] {r['name']:<20}  interview_id={r['interview_id']}")
        print(f"       {r['candidate_url']}")
        print()

    if err:
        print("❌ FAILED:")
        for r in err:
            print(f"  [{r['idx']:02d}] {r['name']}: {r['error']}")

    print()
    print("💡 Now run the orchestrator:")
    print(f"   AIRA_MAX_PARALLEL={len(ok)} conda run -n aira_env python aira_3_webrtc_orchestration.py")
    print()
    print("💡 And in another terminal, start the monitor:")
    print(f"   conda run -n aira_env python load_test_monitor.py --label '{len(ok)}-concurrent' --pname aira_3_webrtc")
    print()

    # Save interview IDs to a file for easy reference
    ids = [r["interview_id"] for r in ok]
    with open("load_test_interview_ids.txt", "w") as f:
        f.write("\n".join(str(i) for i in ids) + "\n")
    print(f"📄 Interview IDs saved to: load_test_interview_ids.txt")


if __name__ == "__main__":
    main()
