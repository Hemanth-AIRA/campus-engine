#!/usr/bin/env python3
"""
seed_question_bank.py — LOCAL stand-in for what the campus backend will do (Task 2, testing).

For a given job in the LOCAL MySQL DB, this emulates the backend so the engine's cached-JD-question
path can be tested end to end BEFORE the real backend ships it:

  1. read the job's mandatory (+ optional) skills from `jobs`
  2. build question GROUPS — one per single skill, plus a few skill-COMBO groups
  3. generate N questions per group (GPT, with a template fallback)
  4. render each question to audio (Cartesia) → MP3 and store it in the shared Redis under
     key  qbank:{job_id}:{group}:{qid}   (the same store the engine consumes)
  5. write the `question_bank` JSON (text + Redis keys, NO audio bytes) onto the jobs row
     — adding a `question_bank` JSON column if the table doesn't have one yet
  6. also drop a local banks_audio/question_bank.<job_id>.json for inspection

The engine (question_bank_cache.QuestionBankPlan + get_audio_cache) then reads that exact shape.

Usage:
    python scripts/seed_question_bank.py --job-id 42
    python scripts/seed_question_bank.py --job-id 42 --per-group 4 --max-combos 3
    python scripts/seed_question_bank.py --job-id 42 --dry-run          # silent audio, still writes DB
    python scripts/seed_question_bank.py --job-id 42 --no-audio         # DB text only, no Cartesia/Redis
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (_ROOT, _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"), override=False)
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("seed_question_bank")

from generate_cached_audio import synthesize_pcm, _pcm_to_mp3, SAMPLE_RATE  # noqa: E402
from question_bank_cache import get_audio_cache                             # noqa: E402


def _group_key(skills) -> str:
    return "+".join(re.sub(r"\s+", "", s.lower()) for s in skills) or "general"


def _split_skills(raw: str):
    if not raw:
        return []
    parts = re.split(r"[,\n;/|]+", raw)
    out, seen = [], set()
    for p in parts:
        s = p.strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            out.append(s)
    return out


# ── question generation ──────────────────────────────────────────────────────
def gen_questions_gpt(skills, n, job_title, model):
    from openai import OpenAI
    client = OpenAI()
    focus = " and ".join(skills)
    sys_p = ("You author concise spoken technical interview questions. Return ONLY JSON "
             '{"questions": ["...", ...]} with natural, conversational questions an interviewer '
             "would say aloud — no numbering, no markdown.")
    user_p = (f"Role: {job_title or 'software engineer'}. Write {n} interview questions that assess "
              f"{focus}. Mix difficulty (a foundational, an applied, and a deeper one). "
              f"{'Make them require using the skills together.' if len(skills) > 1 else ''}")
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
        temperature=0.7, max_tokens=400, response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)
    qs = data.get("questions") or data.get("items") or []
    return [q.strip() for q in qs if isinstance(q, str) and q.strip()][:n]


def gen_questions_template(skills, n):
    s0 = skills[0]
    if len(skills) == 1:
        pool = [
            f"Can you explain {s0} and walk me through how you've used it in a project?",
            f"What are some common pitfalls when working with {s0}, and how do you avoid them?",
            f"How would you decide when {s0} is the right tool versus an alternative?",
            f"Describe a time {s0} caused a tricky bug or performance issue and how you resolved it.",
            f"What best practices do you follow when working with {s0}?",
        ]
    else:
        joined = " and ".join(skills)
        pool = [
            f"How would you use {joined} together in a real project? Walk me through your approach.",
            f"What challenges come up when combining {joined}, and how do you handle them?",
            f"Describe a system where you used both {joined} and how they fit together.",
            f"How do you keep {joined} working well together as a system grows?",
        ]
    return (pool * ((n // len(pool)) + 1))[:n]


def build_questions(skills, n, job_title, model, use_gpt):
    if use_gpt:
        try:
            qs = gen_questions_gpt(skills, n, job_title, model)
            if qs:
                return qs
            logger.warning("GPT returned no questions for %s — using templates", skills)
        except Exception as e:
            logger.warning("GPT generation failed for %s (%s) — using templates", skills, e)
    return gen_questions_template(skills, n)


# ── DB helpers ───────────────────────────────────────────────────────────────
def _connect():
    import pymysql
    from integrations.aira_sql import DB_CONFIG
    missing = [k for k in ("user", "password", "database") if not DB_CONFIG.get(k)]
    if missing:
        raise SystemExit(f"MySQL env not set: {missing} (need MYSQL_USER/MYSQL_PASS/MYSQL_DB).")
    return pymysql.connect(**DB_CONFIG)


def _ensure_question_bank_column(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name='jobs' AND column_name='question_bank'"
        )
        row = cur.fetchone()
        exists = (row["c"] if isinstance(row, dict) else row[0]) > 0
        if not exists:
            logger.info("adding jobs.question_bank JSON column (first run)")
            cur.execute("ALTER TABLE jobs ADD COLUMN question_bank JSON NULL")
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job-id", type=int, required=True)
    ap.add_argument("--per-group", type=int, default=3, help="questions per group (default 3)")
    ap.add_argument("--max-combos", type=int, default=2, help="how many skill-combo groups (default 2)")
    ap.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    ap.add_argument("--template-questions", action="store_true", help="skip GPT, use templates")
    ap.add_argument("--dry-run", action="store_true", help="silent audio (no Cartesia spend); still writes DB")
    ap.add_argument("--no-audio", action="store_true", help="DB text only — no Cartesia, no Redis")
    args = ap.parse_args()

    conn = _connect()
    with conn.cursor() as cur:
        cur.execute("SELECT id, job_title, job_description, mandatory_skills, optional_skills "
                    "FROM jobs WHERE id=%s", (args.job_id,))
        job = cur.fetchone()
    if not job:
        raise SystemExit(f"job id {args.job_id} not found in jobs table")

    mand = _split_skills(job.get("mandatory_skills"))
    if not mand:
        raise SystemExit(f"job {args.job_id} has no mandatory_skills to build a bank from")
    logger.info("job %s '%s' | mandatory skills: %s", args.job_id, job.get("job_title"), mand)

    # groups: single skills, then skill-combos (pairs)
    group_specs = [[s] for s in mand]
    for combo in itertools.islice(itertools.combinations(mand, 2), max(0, args.max_combos)):
        group_specs.append(list(combo))

    cache = None
    if not args.no_audio and not args.dry_run:
        cache = get_audio_cache()
        if not cache.available:
            logger.warning("REDIS_URL unset/redis down — audio won't be cached; engine will use live TTS")
            cache = None

    use_gpt = not args.template_questions
    groups = []
    n_q = n_audio = 0
    for skills in group_specs:
        gkey = _group_key(skills)
        questions = build_questions(skills, args.per_group, job.get("job_title"), args.model, use_gpt)
        g = {"key": gkey, "skills": skills, "questions": []}
        for i, text in enumerate(questions, 1):
            qid = f"{gkey}-q{i}"
            audio_key = f"qbank:{args.job_id}:{gkey}:{qid}"
            if not args.no_audio:
                pcm = synthesize_pcm(text, dry_run=args.dry_run)
                if cache is not None:
                    if cache.put_audio(audio_key, _pcm_to_mp3(pcm)):
                        n_audio += 1
            g["questions"].append({"id": qid, "text": text, "audio_key": audio_key, "rubric": ""})
            n_q += 1
            logger.info("  [%s] %s", gkey, text[:70])
        groups.append(g)

    question_bank = {"sample_rate": SAMPLE_RATE, "job_id": args.job_id, "groups": groups}

    # persist onto the jobs row (what /claim will carry)
    _ensure_question_bank_column(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET question_bank=%s WHERE id=%s",
                    (json.dumps(question_bank), args.job_id))
    conn.commit()
    conn.close()

    out_path = os.path.join(_ROOT, "banks_audio", f"question_bank.{args.job_id}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(question_bank, f, indent=2, ensure_ascii=False)

    logger.info("✅ job %s: %d groups, %d questions, %d audio clips in Redis | wrote jobs.question_bank + %s",
                args.job_id, len(groups), n_q, n_audio, out_path)


if __name__ == "__main__":
    main()
