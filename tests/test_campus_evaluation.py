#!/usr/bin/env python3
"""CAMPUS Phase 5 — faculty evaluation layer proof.

Reframes per-answer scores into learning signal: per-skill mastery bands, progression,
formative "practice X" recommendations, and a readiness summary that is NOT hire/no-hire.
Pure + deterministic.
"""
from campus.evaluation import (
    normalize_score, mastery_label, compute_skill_mastery, formative_recommendations,
    progression, readiness_summary, build_faculty_evaluation, skill_records_from,
    PROFICIENT_CUTOFF,
)

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


if __name__ == "__main__":
    # normalize handles 0-1, 0-5, 0-100.
    check(normalize_score(0.8) == 0.8 and normalize_score(4) == 0.8 and normalize_score(80) == 0.8,
          "normalize_score coerces 0-1 / 0-5 / 0-100 to the same 0-1 value")

    check(mastery_label(0.9) == "Advanced" and mastery_label(0.75) == "Proficient"
          and mastery_label(0.5) == "Developing" and mastery_label(0.2) == "Beginning",
          "mastery_label bands map correctly")

    # Per-skill mastery: strong Python, weak SQL, multi-question skills averaged.
    records = [
        {"skill": "Python", "score": 0.9}, {"skill": "Python", "score": 0.8},
        {"skill": "SQL", "score": 0.3}, {"skill": "SQL", "score": 0.4},
        {"skill": "Testing", "score": 0.6},
    ]
    m = compute_skill_mastery(records)
    check(m["Python"]["level"] == "Advanced" and m["Python"]["n"] == 2, "Python → Advanced (avg of 2)")
    check(m["SQL"]["level"] == "Beginning", "SQL → Beginning")
    check(list(m.keys())[0] == "SQL", "mastery sorted weakest-first (SQL leads) for faculty triage")

    # Recommendations target the weak skills only, with actionable guidance.
    recs = formative_recommendations(m)
    check(any("SQL" in r for r in recs) and not any("Python" in r for r in recs),
          "recommendations flag SQL to practice but not the proficient Python")
    check(all("Practice" in r for r in recs), "recommendations are actionable ('Practice ...')")

    # Progression: rising scores → improving.
    rising = [{"skill": "X", "score": s} for s in (0.3, 0.4, 0.6, 0.8)]
    check(progression(rising)["trend"] == "improving", "rising scores → improving")
    falling = [{"skill": "X", "score": s} for s in (0.8, 0.7, 0.4, 0.3)]
    check(progression(falling)["trend"] == "declining", "falling scores → declining")

    # Readiness summary is learning-oriented (no hire/no-hire language).
    s = readiness_summary(m)
    check("hire" not in s["headline"].lower() and "fail" not in s["headline"].lower(),
          "readiness summary avoids hire/no-hire language")
    check(s["proficient_count"] == sum(1 for v in m.values() if v["avg"] >= PROFICIENT_CUTOFF),
          "readiness counts proficient skills")

    # Join analysis blocks (0-5 scores) to skill history.
    blocks = [
        {"id": 2, "overall_score": 4.5, "flag": False},
        {"id": 3, "overall_score": 1.5, "flag": False},
        {"id": 4, "overall_score": 2.5, "flag": True},   # flagged → excluded
    ]
    history = {2: ["Python"], 3: ["SQL"], 4: ["Docker"]}
    recs2 = skill_records_from(blocks, history)
    check(len(recs2) == 2 and {r["skill"] for r in recs2} == {"Python", "SQL"},
          "skill_records_from joins scores↔skills and drops flagged blocks")
    fac = build_faculty_evaluation(recs2)
    check(fac["skill_mastery"]["Python"]["level"] == "Advanced" and fac["skill_mastery"]["SQL"]["level"] == "Beginning",
          "end-to-end: 0-5 scores → per-skill mastery bands")

    print("\n🎉 CAMPUS FACULTY EVALUATION WORKS (per-skill mastery, progression, formative recs)"
          if ok else "⚠️ EVALUATION LAYER ISSUE — see above")
    import sys
    sys.exit(0 if ok else 1)
