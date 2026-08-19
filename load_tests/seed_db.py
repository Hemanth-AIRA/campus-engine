#!/usr/bin/env python3
"""
seed_db.py  —  One-time setup: insert 100 test candidates + 10 jobs + 100 interviews.

Run this ONCE before any load test:
    conda run -n aira_env python -m load_tests.seed_db
    conda run -n aira_env python -m load_tests.seed_db --dry-run   (preview only)

Creates:
    - 10 jobs  (5 technical, 3 non-technical, 2 mixed/leadership)
    - 100 candidates (10 per job profile, unique emails *test@airahr.ai)
    - 100 interviews  (1 per candidate, status = 'Not Started', no room yet)

After seeding, run simulate_interviews.py to reset+activate N of them:
    conda run -n aira_env python -m load_tests.simulate_interviews --n 10
"""
import os, sys, json, time, argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(override=True)
import pymysql

from load_tests.config import DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME

IST = timezone(timedelta(hours=5, minutes=30))
# Match the orchestrator/reset: interviews are claimed by interview_time compared
# against "now" in JOBDIVA_TIMEZONE (America/New_York). Seed in that SAME zone so
# seeded interviews are claimable and their interview_timezone is consistent.
INTERVIEW_TZ = os.getenv("JOBDIVA_TIMEZONE", "America/New_York")
RECRUITER_ID    = 3
RECRUITER_EMAIL = "Sridhar.k1@nrconsulting.com"
EMAIL_CONFIG    = '["hemanth.nalabolu@airahr.ai"]'

# ═══════════════════════════════════════════════════════════════════════
# 10 JOBS  (5 technical, 3 non-technical, 2 leadership/mixed)
# ═══════════════════════════════════════════════════════════════════════
JOBS = [
    # ── Technical ──────────────────────────────────────────────────────
    {
        "key": "python_ml",
        "title": "Python ML Engineer",
        "type": "technical",
        "mandatory_skills": "python, pytorch, langchain, langgraph",
        "optional_skills": "MySQL, Git, OOP, Agile, FastAPI",
        "interview_config": {
            "base_level": "medium",
            "total_rounds": 3,
            "pass_threshold_pct": 55,
            "questions_per_round": 5,
            "max_duration_minutes": 25,
            "role_experience_required": "mid"
        },
        "resume_template": (
            "Python ML Engineer | PyTorch, LangChain, LangGraph | {name} | "
            "5 years experience in ML pipeline development. Deployed production "
            "NLP models using LangChain agents, fine-tuned PyTorch models on "
            "custom datasets, built FastAPI backends."
        ),
        "jd_analysis": (
            "Strong technical match: candidate has hands-on PyTorch and LangChain "
            "experience. Leadership and communication skills to be assessed."
        ),
    },
    {
        "key": "frontend_react",
        "title": "Senior React Frontend Developer",
        "type": "technical",
        "mandatory_skills": "react, javascript, typescript, css",
        "optional_skills": "Next.js, Redux, GraphQL, Jest, Webpack",
        "interview_config": {
            "base_level": "medium",
            "total_rounds": 3,
            "pass_threshold_pct": 55,
            "questions_per_round": 5,
            "max_duration_minutes": 25,
            "role_experience_required": "senior"
        },
        "resume_template": (
            "Senior Frontend Developer | React, TypeScript | {name} | "
            "6 years building high-performance SPAs. Led migration from Angular "
            "to React, implemented design systems, optimised Core Web Vitals."
        ),
        "jd_analysis": (
            "Candidate has strong React ecosystem experience. TypeScript depth "
            "and testing practices need evaluation."
        ),
    },
    {
        "key": "devops_cloud",
        "title": "DevOps / Cloud Engineer",
        "type": "technical",
        "mandatory_skills": "kubernetes, docker, AWS, terraform, CI/CD",
        "optional_skills": "Helm, ArgoCD, Prometheus, Grafana, Python scripting",
        "interview_config": {
            "base_level": "medium",
            "total_rounds": 3,
            "pass_threshold_pct": 50,
            "questions_per_round": 5,
            "max_duration_minutes": 25,
            "role_experience_required": "mid"
        },
        "resume_template": (
            "DevOps Engineer | Kubernetes, AWS | {name} | "
            "Managed multi-cluster K8s deployments, wrote Terraform modules for "
            "AWS infra, built GitOps pipelines with ArgoCD and GitHub Actions."
        ),
        "jd_analysis": (
            "Strong infrastructure background. Cloud cost optimisation and "
            "incident management experience to be probed."
        ),
    },
    {
        "key": "backend_java",
        "title": "Backend Java Developer",
        "type": "technical",
        "mandatory_skills": "java, spring boot, microservices, SQL",
        "optional_skills": "Kafka, Redis, Docker, JUnit, Maven",
        "interview_config": {
            "base_level": "easy",
            "total_rounds": 3,
            "pass_threshold_pct": 50,
            "questions_per_round": 5,
            "max_duration_minutes": 20,
            "role_experience_required": "junior"
        },
        "resume_template": (
            "Java Backend Developer | Spring Boot | {name} | "
            "Built RESTful microservices with Spring Boot and JPA. "
            "Integrated Kafka for event-driven communication, used Redis for caching."
        ),
        "jd_analysis": "Good foundational Java skills. Microservices design patterns to be explored.",
    },
    {
        "key": "data_analyst",
        "title": "Data Analyst",
        "type": "technical",
        "mandatory_skills": "SQL, Python, Excel, data visualisation",
        "optional_skills": "Tableau, Power BI, pandas, scikit-learn, Statistics",
        "interview_config": {
            "base_level": "easy",
            "total_rounds": 2,
            "pass_threshold_pct": 50,
            "questions_per_round": 6,
            "max_duration_minutes": 20,
            "role_experience_required": "junior"
        },
        "resume_template": (
            "Data Analyst | SQL, Python | {name} | "
            "Created executive dashboards in Tableau, wrote complex SQL queries "
            "for business reporting, built Python ETL pipelines."
        ),
        "jd_analysis": "Solid analytical skills. Statistical reasoning and business acumen to be assessed.",
    },
    # ── Non-Technical ───────────────────────────────────────────────────
    {
        "key": "hr_manager",
        "title": "HR Manager",
        "type": "non_technical",
        "mandatory_skills": "talent acquisition, employee relations, HRMS",
        "optional_skills": "HRIS, payroll, training & development, compliance, labour law",
        "interview_config": {
            "base_level": "medium",
            "total_rounds": 2,
            "pass_threshold_pct": 60,
            "questions_per_round": 6,
            "max_duration_minutes": 25,
            "role_experience_required": "senior"
        },
        "resume_template": (
            "HR Manager | Talent Acquisition, Employee Relations | {name} | "
            "8 years in HR across IT and BFSI sectors. Managed hiring drives of "
            "500+ across India, built performance appraisal systems, handled "
            "disciplinary procedures in compliance with labour law."
        ),
        "jd_analysis": "Experienced HR professional. Culture fit and conflict resolution skills to be probed.",
    },
    {
        "key": "sales_executive",
        "title": "Sales Executive (B2B SaaS)",
        "type": "non_technical",
        "mandatory_skills": "B2B sales, CRM, lead generation, negotiation",
        "optional_skills": "Salesforce, cold calling, account management, SaaS, pipeline management",
        "interview_config": {
            "base_level": "easy",
            "total_rounds": 2,
            "pass_threshold_pct": 55,
            "questions_per_round": 6,
            "max_duration_minutes": 20,
            "role_experience_required": "mid"
        },
        "resume_template": (
            "Sales Executive | B2B SaaS | {name} | "
            "Achieved 140% of quota for 3 consecutive quarters. Managed 50+ "
            "enterprise accounts, shortened sales cycles using consultative selling, "
            "proficient with Salesforce CRM."
        ),
        "jd_analysis": "Strong sales track record. Objection handling and SaaS domain fit to be evaluated.",
    },
    {
        "key": "content_writer",
        "title": "Content Writer / Copywriter",
        "type": "non_technical",
        "mandatory_skills": "content writing, SEO, research, editing",
        "optional_skills": "WordPress, HubSpot, social media, email marketing, storytelling",
        "interview_config": {
            "base_level": "easy",
            "total_rounds": 2,
            "pass_threshold_pct": 50,
            "questions_per_round": 5,
            "max_duration_minutes": 20,
            "role_experience_required": "junior"
        },
        "resume_template": (
            "Content Writer | SEO, Copywriting | {name} | "
            "Produced 200+ long-form articles ranking on Google page 1. "
            "Wrote email campaigns achieving 35% open rate. Proficient in "
            "HubSpot, WordPress, and keyword research tools."
        ),
        "jd_analysis": "Good writing portfolio. Tone adaptability and brevity under time pressure to be tested.",
    },
    # ── Leadership / Mixed ──────────────────────────────────────────────
    {
        "key": "engineering_manager",
        "title": "Engineering Manager",
        "type": "leadership",
        "mandatory_skills": "team leadership, system design, agile, stakeholder management",
        "optional_skills": "Python, cloud architecture, hiring, roadmapping, OKRs",
        "interview_config": {
            "base_level": "hard",
            "total_rounds": 3,
            "pass_threshold_pct": 65,
            "questions_per_round": 5,
            "max_duration_minutes": 30,
            "role_experience_required": "senior"
        },
        "resume_template": (
            "Engineering Manager | {name} | "
            "Led a 15-person engineering team delivering a SaaS platform serving "
            "2M users. Drove quarterly OKRs, managed cross-functional stakeholders, "
            "mentored junior engineers. Technical background in Python and AWS."
        ),
        "jd_analysis": "Strong leadership and delivery track record. Conflict resolution and technical depth to be assessed.",
    },
    {
        "key": "product_manager",
        "title": "Product Manager",
        "type": "leadership",
        "mandatory_skills": "product strategy, user research, roadmapping, stakeholder management",
        "optional_skills": "SQL, Jira, Figma, A/B testing, go-to-market, analytics",
        "interview_config": {
            "base_level": "hard",
            "total_rounds": 3,
            "pass_threshold_pct": 60,
            "questions_per_round": 5,
            "max_duration_minutes": 30,
            "role_experience_required": "senior"
        },
        "resume_template": (
            "Product Manager | {name} | "
            "Owned end-to-end product lifecycle for a B2C mobile app with 5M+ "
            "downloads. Drove 40% DAU growth via data-driven feature prioritisation. "
            "Collaborated with design, engineering, and marketing teams."
        ),
        "jd_analysis": "Strong product instinct. Prioritisation frameworks and data-driven decision-making to be probed.",
    },
]

# ═══════════════════════════════════════════════════════════════════════
# 100 CANDIDATES  (10 per job)
# ═══════════════════════════════════════════════════════════════════════
CANDIDATE_POOL = {
    "python_ml":        ["Arjun Sharma","Priya Nair","Karan Mehta","Divya Rao","Rohit Verma","Sneha Iyer","Aditya Kumar","Meera Pillai","Vikram Singh","Anjali Reddy"],
    "frontend_react":   ["Rahul Gupta","Pooja Desai","Sameer Joshi","Lakshmi Devi","Nikhil Tiwari","Riya Bansal","Saurabh Yadav","Kavya Menon","Amit Chauhan","Preeti Soni"],
    "devops_cloud":     ["Suresh Babu","Ananya Krishnan","Manish Saxena","Shalini Thakur","Praveen Nambiar","Aparna George","Sachin Wagh","Rekha Desai","Dinesh Patil","Usha Patel"],
    "backend_java":     ["Manoj Sharma","Sunita Rao","Abhishek Mishra","Deepa Nair","Ramesh Kumar","Geeta Bhatt","Harish Narayanan","Madhuri Jain","Biju Thomas","Sarita Tripathi"],
    "data_analyst":     ["Sandeep Yadav","Ritu Agarwal","Gaurav Dubey","Pallavi Saxena","Naresh Verma","Sonal Aggarwal","Deepak Rajan","Anjana Nambiar","Rajesh Srivastava","Nisha Das"],
    "hr_manager":       ["Swapna Reddy","Venkat Iyer","Anitha Krishnan","Sunil Kapoor","Jayashree Rao","Mahesh Hegde","Revathi Subramaniam","Satish Menon","Bhavna Tiwari","Arun Choudhary"],
    "sales_executive":  ["Vivek Chandra","Simi Pillai","Tarun Bhatia","Manu Aggarwal","Sharath Kumar","Asha Balan","Rajeev Pandey","Neethu Varghese","Arvind Sharma","Kavitha Suresh"],
    "content_writer":   ["Shreya Ghosh","Parth Shah","Roshni Thomas","Kunal Malhotra","Sunaina Nair","Rishabh Verma","Trisha Mehta","Siddharth Gupta","Neelam Arora","Rohan Das"],
    "engineering_manager":["Pradeep Nair","Vasantha Reddy","Sudheer Kumar","Lalitha Devi","Naveen Joshi","Archana Rao","Ramakrishna Iyer","Jaya Prabhu","Shyam Sundar","Hema Latha"],
    "product_manager":  ["Siddhesh Patil","Radha Krishnan","Aakash Mehta","Sowmya Nair","Balaji Rao","Prathima Reddy","Hemant Joshi","Saranya Krishnan","Dinesh Sharma","Kamla Devi"],
}

# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def email_for(name: str, job_key: str) -> str:
    slug = name.lower().replace(" ", ".").replace("'", "")
    return f"{slug}.{job_key}.test@airahr.ai"


def jd_id_for(name: str, job_key: str) -> str:
    import hashlib
    h = hashlib.md5(f"{name}-{job_key}".encode()).hexdigest()
    return f"191902{h[:8].upper()}"


def upsert_candidate(cur, name: str, job_key: str) -> int:
    email = email_for(name, job_key)
    cur.execute("SELECT id FROM candidates WHERE candidate_email = %s", (email,))
    row = cur.fetchone()
    if row:
        return row["id"]
    resume = f"Load test candidate — {name} ({job_key.replace('_',' ').title()})"
    cur.execute(
        """INSERT INTO candidates
           (candidate_jd_id, candidate_name, candidate_phone, candidate_email, resume_text)
           VALUES (%s, %s, NULL, %s, %s)""",
        (jd_id_for(name, job_key), name, email, resume)
    )
    return cur.lastrowid


def upsert_job(cur, job: dict) -> int:
    """Look up by job_title — return existing id or insert new."""
    cur.execute("SELECT id FROM jobs WHERE job_title = %s LIMIT 1", (job["title"],))
    row = cur.fetchone()
    if row:
        return row["id"]
    import hashlib, random
    jd_ref = f"SIM-{hashlib.md5(job['title'].encode()).hexdigest()[:8].upper()}"
    cur.execute(
        """INSERT INTO jobs (job_jd_ref_number, job_title, job_description,
                             company_name, job_status,
                             mandatory_skills, has_mandatory_skills,
                             created_at)
           VALUES (%s, %s, %s, %s, 'Active', %s, 1, NOW())""",
        (jd_ref, job["title"],
         f"Load test job — {job['title']} ({job['type']})",
         "AIRA Load Test Corp",
         job["mandatory_skills"])
    )
    return cur.lastrowid


def upsert_interview(cur, candidate_id: int, job_id: int, job: dict) -> int:
    """Insert or reset interview for candidate+job. status = 'Not Started'."""
    cur.execute(
        "SELECT id FROM interviews WHERE candidate_id=%s AND job_id=%s",
        (candidate_id, job_id)
    )
    row = cur.fetchone()
    config_json = json.dumps(job["interview_config"])

    if row:
        # Reset: clear results but keep room_id as placeholder (NOT NULL)
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
               mandatory_skills=%s,
               optional_skills=%s,
               interview_config=%s,
               bot_join_url=NULL,
               candidate_join_url=NULL,
               recruiter_join_url=NULL,
               sentinel_url=NULL,
               updated_at=NOW()
               WHERE id=%s""",
            (job["mandatory_skills"], job["optional_skills"], config_json, row["id"])
        )
        return row["id"]

    # room_id is NOT NULL — use a placeholder until simulate resets it
    import uuid
    placeholder_room = f"SEED-{uuid.uuid4().hex[:12].upper()}"
    now_et = (datetime.now(ZoneInfo(INTERVIEW_TZ)) - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
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
            room_id,
            created_at, updated_at
           ) VALUES (
            %s, %s, %s, %s,
            %s, %s, 75.0,
            %s, 'America/New_York',
            'Not Started', 0.0,
            1, 1, 1,
            %s, 1,
            %s, %s, 'Connect',
            'America/New_York', 1, 1,
            %s, %s, 1,
            0,
            %s,
            NOW(), NOW()
           )""",
        (
            candidate_id, job_id, RECRUITER_ID, RECRUITER_EMAIL,
            job["resume_template"].format(name="Load Test Candidate"),
            job["jd_analysis"],
            now_et,
            job["mandatory_skills"],
            job["optional_skills"],
            config_json,
            EMAIL_CONFIG,
            job["type"],
            placeholder_room,
        )
    )
    return cur.lastrowid


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main(dry_run: bool = False):
    print("=" * 65)
    print("  AIRA Load Test DB Seeder")
    print(f"  Jobs: {len(JOBS)}  |  Candidates: 100  |  Interviews: 100")
    if dry_run:
        print("  MODE: DRY RUN — no DB writes")
    print("=" * 65)

    if dry_run:
        for job in JOBS:
            names = CANDIDATE_POOL[job["key"]]
            print(f"\n  [{job['type'].upper():12}] {job['title']}")
            for name in names:
                print(f"      {name:28} {email_for(name, job['key'])}")
        print(f"\n  Total: {sum(len(v) for v in CANDIDATE_POOL.values())} candidates")
        return

    conn = get_conn()
    cur  = conn.cursor()

    job_ids          = {}   # job_key → db id
    inserted_jobs    = 0
    inserted_cands   = 0
    inserted_ivs     = 0
    total_interviews = []

    for job in JOBS:
        # ── Job ─────────────────────────────────────────────────────────
        try:
            job_id = upsert_job(cur, job)
            conn.commit()
            job_ids[job["key"]] = job_id
            inserted_jobs += 1
            print(f"  [JOB] {job['title']:40} id={job_id}")
        except Exception as e:
            conn.rollback()
            print(f"  [JOB] ❌ {job['title']}: {e}")
            continue

        # ── Candidates + Interviews ──────────────────────────────────────
        for name in CANDIDATE_POOL[job["key"]]:
            try:
                cand_id = upsert_candidate(cur, name, job["key"])
                conn.commit()
                inserted_cands += 1

                iv_id = upsert_interview(cur, cand_id, job_id, job)
                conn.commit()
                inserted_ivs += 1

                total_interviews.append({
                    "interview_id": iv_id,
                    "candidate_id": cand_id,
                    "candidate_name": name,
                    "job_key": job["key"],
                    "job_title": job["title"],
                    "job_type": job["type"],
                    "job_id": job_id,
                })
                print(f"       ✅ {name:28} cand={cand_id}  iv={iv_id}")

            except Exception as e:
                conn.rollback()
                print(f"       ❌ {name}: {e}")

        print()

    cur.close()
    conn.close()

    # ── Save index ─────────────────────────────────────────────────────
    index_path = Path(__file__).parent / "seed_index.json"
    index_path.write_text(json.dumps(total_interviews, indent=2))

    print("=" * 65)
    print(f"  ✅ Jobs inserted/updated   : {inserted_jobs}")
    print(f"  ✅ Candidates inserted     : {inserted_cands}")
    print(f"  ✅ Interviews seeded       : {inserted_ivs}")
    print(f"  📄 Index saved to          : {index_path}")
    print("=" * 65)
    print()
    print("  Next step — run the simulation:")
    print("    python -m load_tests.simulate_interviews --n 10")
    print("    python -m load_tests.simulate_interviews --n 50")
    print()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Preview without DB writes")
    args = ap.parse_args()
    main(dry_run=args.dry_run)
