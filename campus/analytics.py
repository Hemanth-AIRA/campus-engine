"""
Campus ANALYTICS for faculty / placement cells.

Aggregates per-session faculty evaluations (each = campus_evaluation.build_faculty_evaluation
output) across a batch of students into the things a college actually acts on:

  • COHORT SKILL GAPS   — for each skill, the batch's average mastery and % of students
                          proficient → "62% of the batch is weak on DBMS", so faculty can
                          plan remediation.
  • READINESS DISTRIBUTION — how many students sit in each mastery band.
  • STUDENT PROGRESS    — a single student's mastery across repeated attempts (does the
                          tool actually move the needle — the ROI story).

Pure aggregation + a self-contained HTML artifact (no external assets), fully testable.
A "session" is: {"student", "attempt"?, "timestamp"?, "faculty_evaluation": <bundle>}.
"""
from typing import List, Dict
import html as _html

from campus.evaluation import mastery_label, PROFICIENT_CUTOFF


def _sessions_mastery(sessions: List[Dict]):
    for s in sessions:
        fe = s.get("faculty_evaluation") or {}
        yield s, (fe.get("skill_mastery") or {})


def cohort_skill_gaps(sessions: List[Dict]) -> Dict[str, Dict]:
    """Per-skill: avg mastery across students, % proficient, n. Sorted weakest-first."""
    agg: Dict[str, List[float]] = {}
    for _s, mastery in _sessions_mastery(sessions):
        for skill, m in mastery.items():
            agg.setdefault(skill, []).append(float(m.get("avg", 0.0)))
    out = {}
    for skill, avgs in agg.items():
        avg = sum(avgs) / len(avgs) if avgs else 0.0
        prof = sum(1 for a in avgs if a >= PROFICIENT_CUTOFF)
        out[skill] = {
            "avg": round(avg, 3),
            "level": mastery_label(avg),
            "n_students": len(avgs),
            "pct_proficient": round(100.0 * prof / len(avgs), 1) if avgs else 0.0,
        }
    return dict(sorted(out.items(), key=lambda kv: kv[1]["avg"]))


def readiness_distribution(sessions: List[Dict]) -> Dict[str, int]:
    dist = {"Advanced": 0, "Proficient": 0, "Developing": 0, "Beginning": 0}
    for s, mastery in _sessions_mastery(sessions):
        avgs = [float(m.get("avg", 0.0)) for m in mastery.values()]
        overall = sum(avgs) / len(avgs) if avgs else 0.0
        dist[mastery_label(overall)] += 1
    return dist


def student_progress(sessions_for_student: List[Dict]) -> Dict:
    """Overall mastery per attempt (ordered) for one student → the improvement curve."""
    ordered = sorted(sessions_for_student, key=lambda s: (s.get("attempt", 0), s.get("timestamp", "")))
    curve = []
    for s in ordered:
        mastery = (s.get("faculty_evaluation") or {}).get("skill_mastery") or {}
        avgs = [float(m.get("avg", 0.0)) for m in mastery.values()]
        curve.append(round(sum(avgs) / len(avgs), 3) if avgs else 0.0)
    delta = round(curve[-1] - curve[0], 3) if len(curve) >= 2 else 0.0
    return {"attempts": len(curve), "curve": curve, "improvement": delta,
            "improved": delta > 0.0}


def cohort_summary(sessions: List[Dict]) -> Dict:
    gaps = cohort_skill_gaps(sessions)
    weak = [s for s, g in gaps.items() if g["avg"] < PROFICIENT_CUTOFF]
    return {
        "n_students": len(sessions),
        "skill_gaps": gaps,
        "readiness_distribution": readiness_distribution(sessions),
        "focus_skills": weak[:5],
        "headline": (f"{len(sessions)} students assessed. "
                     + (f"Batch is weakest on: {', '.join(weak[:3])}." if weak
                        else "Batch is broadly proficient.")),
    }


def render_cohort_html(sessions: List[Dict], title: str = "Cohort Readiness") -> str:
    """Self-contained HTML (inline styles, no external assets) for a faculty dashboard."""
    summary = cohort_summary(sessions)
    gaps, dist = summary["skill_gaps"], summary["readiness_distribution"]

    def bar(pct, color):
        return (f'<div style="background:#eee;border-radius:4px;overflow:hidden;height:14px">'
                f'<div style="width:{pct:.0f}%;height:14px;background:{color}"></div></div>')

    def band_color(level):
        return {"Advanced": "#1a7f37", "Proficient": "#3fb950",
                "Developing": "#d29922", "Beginning": "#cf222e"}.get(level, "#8b949e")

    rows = "".join(
        f'<tr><td style="padding:6px 10px">{_html.escape(sk)}</td>'
        f'<td style="padding:6px 10px"><span style="color:{band_color(g["level"])};font-weight:600">{g["level"]}</span></td>'
        f'<td style="padding:6px 10px">{g["avg"]:.2f}</td>'
        f'<td style="padding:6px 10px;min-width:120px">{bar(g["pct_proficient"], band_color(g["level"]))}'
        f'<small>{g["pct_proficient"]:.0f}% proficient (n={g["n_students"]})</small></td></tr>'
        for sk, g in gaps.items()
    )
    dist_html = " · ".join(f'<b>{k}</b>: {v}' for k, v in dist.items())
    return f"""<div style="font-family:system-ui,Segoe UI,Roboto,sans-serif;max-width:760px;margin:24px auto;color:#1f2328">
  <h2 style="margin:0 0 4px">{_html.escape(title)}</h2>
  <p style="color:#57606a;margin:0 0 16px">{_html.escape(summary['headline'])}</p>
  <p style="margin:0 0 16px">Readiness distribution — {dist_html}</p>
  <table style="border-collapse:collapse;width:100%;border:1px solid #d0d7de;border-radius:6px">
    <thead><tr style="background:#f6f8fa;text-align:left">
      <th style="padding:6px 10px">Skill</th><th style="padding:6px 10px">Cohort level</th>
      <th style="padding:6px 10px">Avg</th><th style="padding:6px 10px">Proficiency</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <p style="color:#57606a;margin:16px 0 0"><b>Faculty focus:</b> {_html.escape(', '.join(summary['focus_skills']) or 'none — broadly proficient')}</p>
</div>"""
