"""
Campus faculty-evaluation layer (Phase 5).

The HR report answers "hire or not?" (a final score + PASSED/FAILED verdict). For college
prep + faculty evaluation that's the wrong question. This module reframes the SAME per-answer
signal the engine already produces (evaluate_answer's confidence_score, and the per-question
scores in the analysis blocks) into LEARNING signal:

  • per-skill MASTERY   — how well each JD skill was demonstrated, as a named band
  • PROGRESSION         — did the student improve across the session / rounds
  • formative RECOMMENDATIONS — concrete "practice X" guidance for the weak skills
  • a readiness SUMMARY — mastery-oriented, never hire/no-hire

Pure functions, no LLM, fully deterministic — so the faculty report is reproducible and testable.
"""
from typing import List, Dict

# Mastery bands over a 0-1 score (checked high → low).
MASTERY_BANDS = [
    (0.85, "Advanced"),
    (0.70, "Proficient"),
    (0.45, "Developing"),
    (0.0,  "Beginning"),
]
PROFICIENT_CUTOFF = 0.70  # at/above this a skill is considered "solid" for prep purposes


def normalize_score(score) -> float:
    """Coerce a score onto 0-1 regardless of the source scale (0-1, 0-5, or 0-100)."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return 0.0
    if s > 5:          # looks like 0-100
        s = s / 100.0
    elif s > 1:        # looks like 0-5
        s = s / 5.0
    return max(0.0, min(1.0, s))


def mastery_label(score01: float) -> str:
    for cutoff, label in MASTERY_BANDS:
        if score01 >= cutoff:
            return label
    return "Beginning"


def compute_skill_mastery(records: List[Dict]) -> Dict[str, Dict]:
    """records: [{"skill": str, "score": <any scale>}]. Returns
    {skill: {"avg": 0-1, "level": str, "n": int}} sorted weakest-first for faculty triage."""
    by_skill: Dict[str, List[float]] = {}
    for r in records:
        skill = (r.get("skill") or "").strip()
        if not skill:
            continue
        by_skill.setdefault(skill, []).append(normalize_score(r.get("score")))
    out = {}
    for skill, scores in by_skill.items():
        avg = sum(scores) / len(scores) if scores else 0.0
        out[skill] = {"avg": round(avg, 3), "level": mastery_label(avg), "n": len(scores)}
    # weakest first
    return dict(sorted(out.items(), key=lambda kv: kv[1]["avg"]))


def baselines_from_prior_mastery(skill_mastery: Dict[str, Dict]) -> Dict[str, Dict]:
    """Personalized retake: seed each skill's STARTING difficulty from the student's prior
    mastery. Strong prior mastery → start at higher complexity + confidence; weak → start
    easier so the retake scaffolds. Feeds compute_difficulty_vector's skill_baseline.
    Returns {skill: {"baseline_confidence": 0-1, "baseline_complexity": ~1-4}}."""
    out = {}
    for skill, m in skill_mastery.items():
        avg = float(m.get("avg", 0.5)) if isinstance(m, dict) else float(m)
        # map mastery [0,1] → a starting complexity in [1,4] (strong skill starts harder)
        complexity = max(1.0, min(4.0, round(1.0 + avg * 3.0, 2)))
        out[skill] = {"baseline_confidence": round(avg, 3), "baseline_complexity": complexity}
    return out


def weak_skill_priority(skill_mastery: Dict[str, Dict]) -> List[str]:
    """Order skills weakest-first, so a personalized retake spends its budget where the
    student is weakest."""
    return [s for s, _ in sorted(
        skill_mastery.items(),
        key=lambda kv: (kv[1].get("avg", 0.5) if isinstance(kv[1], dict) else kv[1]))]


def formative_recommendations(skill_mastery: Dict[str, Dict], limit: int = 5) -> List[str]:
    """Concrete practice guidance for the skills below the proficiency cutoff (weakest first)."""
    recs = []
    for skill, m in skill_mastery.items():
        if m["avg"] < PROFICIENT_CUTOFF:
            if m["level"] == "Beginning":
                how = "start with the fundamentals and worked examples"
            else:  # Developing
                how = "practice applying it to small problems and explaining your reasoning aloud"
            recs.append(f"Practice {skill} ({m['level']}) — {how}.")
        if len(recs) >= limit:
            break
    return recs


def progression(records: List[Dict]) -> Dict:
    """Compare the first half of answers to the second half → improving / steady / needs-work."""
    scores = [normalize_score(r.get("score")) for r in records if r.get("score") is not None]
    if len(scores) < 2:
        return {"trend": "insufficient-data", "early": None, "late": None, "delta": 0.0}
    mid = len(scores) // 2
    early = sum(scores[:mid]) / max(1, mid)
    late = sum(scores[mid:]) / max(1, len(scores) - mid)
    delta = round(late - early, 3)
    trend = "improving" if delta >= 0.08 else ("declining" if delta <= -0.08 else "steady")
    return {"trend": trend, "early": round(early, 3), "late": round(late, 3), "delta": delta}


def readiness_summary(skill_mastery: Dict[str, Dict]) -> Dict:
    """Overall, learning-oriented summary — NOT a hire/no-hire verdict."""
    if not skill_mastery:
        return {"overall": 0.0, "level": "Beginning", "skills_assessed": 0,
                "proficient_count": 0, "headline": "No skills assessed."}
    avgs = [m["avg"] for m in skill_mastery.values()]
    overall = sum(avgs) / len(avgs)
    proficient = sum(1 for m in skill_mastery.values() if m["avg"] >= PROFICIENT_CUTOFF)
    total = len(skill_mastery)
    headline = (
        f"{mastery_label(overall)} overall — solid on {proficient} of {total} skills. "
        + ("Ready to move on to harder material." if overall >= PROFICIENT_CUTOFF
           else "Keep practicing the flagged skills before advancing.")
    )
    return {
        "overall": round(overall, 3),
        "level": mastery_label(overall),
        "skills_assessed": total,
        "proficient_count": proficient,
        "headline": headline,
    }


def build_faculty_evaluation(records: List[Dict]) -> Dict:
    """One-call bundle for the report layer: mastery + progression + recommendations + summary."""
    mastery = compute_skill_mastery(records)
    return {
        "skill_mastery": mastery,
        "progression": progression(records),
        "recommendations": formative_recommendations(mastery),
        "summary": readiness_summary(mastery),
    }


def skill_records_from(analysis_blocks, question_history: Dict[int, list]) -> List[Dict]:
    """Join per-question analysis scores to the skill(s) each question tested.

    analysis_blocks: list of {"id"/"question_number", "overall_score"/"metrics", ...}
    question_history: SkillTracker._question_history — {question_number: [skills]}
    A multi-skill question credits each of its skills with that question's score.
    """
    records = []
    for block in (analysis_blocks or []):
        if not isinstance(block, dict) or block.get("flag"):
            continue
        qno = block.get("id", block.get("question_number"))
        score = block.get("overall_score")
        if score is None and isinstance(block.get("metrics"), list) and block["metrics"]:
            score = sum(m.get("score", 0) for m in block["metrics"]) / len(block["metrics"])
        skills = question_history.get(qno) or question_history.get(str(qno)) or []
        for skill in skills:
            records.append({"skill": skill, "score": score, "qno": qno})
    return records
