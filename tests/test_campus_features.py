#!/usr/bin/env python3
"""CAMPUS college-attraction features — config, profiles, retakes, coaching, analytics.

Proves the new configurable features behave correctly and deterministically (no LLM):
  • CampusConfig    — mode/hints/profile knobs; assessment force-disables coaching
  • profiles        — target company/role presets → config overrides
  • retakes         — prior mastery → personalized baselines + weak-first priority
  • coaching        — hint/model-answer prompt builders (pure)
  • analytics       — cohort skill gaps, readiness distribution, progress, HTML
"""
import os
from campus.config import CampusConfig, PRACTICE, ASSESSMENT
from campus.profiles import load_profile, list_profiles, profile_to_overrides
from campus.evaluation import baselines_from_prior_mastery, weak_skill_priority, build_faculty_evaluation
from campus.coaching import build_hint_prompt, build_model_answer_prompt
from campus.analytics import cohort_skill_gaps, readiness_distribution, student_progress, cohort_summary, render_cohort_html

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


def fac(**skills):
    """Build a faculty_evaluation-like bundle from skill->avg."""
    records = []
    for sk, avg in skills.items():
        records.append({"skill": sk, "score": avg})
    return build_faculty_evaluation(records)


if __name__ == "__main__":
    # ── 1. CampusConfig ──────────────────────────────────────────────
    for k in ("CAMPUS_MODE", "CAMPUS_HINTS", "CAMPUS_PROFILE"):
        os.environ.pop(k, None)
    c = CampusConfig.resolve()
    check(c.is_practice and c.gives_hints() and c.gives_model_answers(),
          "default = practice mode with coaching on")
    c2 = CampusConfig.resolve({"mode": ASSESSMENT})
    check(c2.is_assessment and not c2.coaching_on() and c2.scored_verdict(),
          "assessment mode force-disables coaching and enables scored verdict")
    os.environ["CAMPUS_MODE"] = "assessment"
    check(CampusConfig.resolve().is_assessment, "env CAMPUS_MODE=assessment respected")
    os.environ.pop("CAMPUS_MODE")
    check(CampusConfig.resolve({"escalate_conf": 0.6}).escalate_conf == 0.6,
          "per-interview override wins over default")

    # ── 2. Profiles ──────────────────────────────────────────────────
    check("product-sde" in list_profiles() and "data-analyst" in list_profiles(),
          f"profiles discovered: {list_profiles()}")
    p = load_profile("product-sde")
    ov = profile_to_overrides(p)
    check(ov["role_experience_required"] == "senior" and "System Design" in ov["mandatory_skills"],
          "product-sde profile → senior + System Design skills")
    check(load_profile("nonexistent") is None, "missing profile → None")

    # ── 3. Personalized retakes ──────────────────────────────────────
    prior = fac(Python=0.9, SQL=0.3, Testing=0.6)["skill_mastery"]
    baselines = baselines_from_prior_mastery(prior)
    check(baselines["Python"]["baseline_confidence"] == 0.9
          and baselines["Python"]["baseline_complexity"] > baselines["SQL"]["baseline_complexity"],
          "strong skill retakes at higher starting complexity than weak skill")
    check(weak_skill_priority(prior)[0] == "SQL",
          "retake prioritizes the weakest skill first (SQL)")

    # ── 4. Coaching prompt builders (pure) ───────────────────────────
    hs, hu = build_hint_prompt("What is a Python decorator?", "Python", 2)
    check("without" in hs.lower() and "decorator" in hu, "hint prompt nudges without revealing; includes the question")
    ms, mu = build_model_answer_prompt("Explain JOINs", "SQL", "I don't know")
    check("model answer" in ms.lower() and "I don't know" in mu, "model-answer prompt includes the student's answer")

    # ── 5. Analytics ─────────────────────────────────────────────────
    sessions = [
        {"student": "A", "attempt": 1, "faculty_evaluation": fac(Python=0.9, SQL=0.4)},
        {"student": "B", "attempt": 1, "faculty_evaluation": fac(Python=0.5, SQL=0.3)},
        {"student": "C", "attempt": 1, "faculty_evaluation": fac(Python=0.8, SQL=0.35)},
    ]
    gaps = cohort_skill_gaps(sessions)
    check(list(gaps.keys())[0] == "SQL" and gaps["SQL"]["pct_proficient"] == 0.0,
          "cohort gap: SQL is the weakest, 0% proficient (faculty action item)")
    dist = readiness_distribution(sessions)
    check(sum(dist.values()) == 3, "readiness distribution covers all students")
    prog = student_progress([
        {"student": "A", "attempt": 1, "faculty_evaluation": fac(Python=0.4, SQL=0.3)},
        {"student": "A", "attempt": 2, "faculty_evaluation": fac(Python=0.7, SQL=0.6)},
    ])
    check(prog["attempts"] == 2 and prog["improved"], f"student progress shows improvement {prog['curve']}")
    summ = cohort_summary(sessions)
    check("SQL" in summ["focus_skills"] and "hire" not in summ["headline"].lower(),
          "cohort summary flags focus skills, learning-oriented")
    h = render_cohort_html(sessions, "CSE-A Readiness")
    check(h.strip().startswith("<div") and "CSE-A Readiness" in h and "SQL" in h,
          "cohort HTML renders (self-contained)")

    print("\n🎉 COLLEGE-ATTRACTION FEATURES WORK (config, profiles, retakes, coaching, analytics)"
          if ok else "⚠️ FEATURE ISSUE — see above")
    import sys
    sys.exit(0 if ok else 1)
