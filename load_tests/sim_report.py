"""
load_tests/sim_report.py
Post-simulation report generator.

After all interviews complete, queries the DB for each interview and:
  1. Generates a per-interview HTML analysis report (using existing engine report logic)
  2. Includes the recording link in each report
  3. Generates a master HTML summary table of all interviews

Usage (called automatically by test_06, or standalone):
    python -m load_tests.sim_report --run-id <id>
"""
from __future__ import annotations
import os, sys, json, logging, argparse
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

import pymysql

logger = logging.getLogger("sim_report")

SIM_REPORTS_DIR = Path(__file__).parent / "sim_reports"
SIM_REPORTS_DIR.mkdir(exist_ok=True)

# ── DB connection ─────────────────────────────────────────────────────────────
def _get_db():
    return pymysql.connect(
        host   = os.getenv("MYSQL_HOST", "localhost"),
        port   = int(os.getenv("MYSQL_PORT", 3306)),
        user   = os.getenv("MYSQL_USER", "root"),
        passwd = os.getenv("MYSQL_PASS", ""),
        db     = os.getenv("MYSQL_DB",   "aira_db"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _fetch_interview(conn, interview_id: int) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT i.*,
                   c.candidate_name, c.candidate_email,
                   j.job_title, j.job_description
            FROM interviews i
            LEFT JOIN candidates c ON i.candidate_id = c.id
            LEFT JOIN jobs       j ON i.job_id       = j.id
            WHERE i.id = %s
        """, (interview_id,))
        return cur.fetchone()


def _fetch_responses(conn, interview_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT question_number, question_text, answer_text,
                   confidence_score, skill, summary, evaluation_tags
            FROM interview_questions_live
            WHERE interview_id = %s
            ORDER BY question_number
        """, (interview_id,))
        return cur.fetchall() or []


# ── Per-interview HTML report ─────────────────────────────────────────────────
def _score_color(score) -> str:
    try:
        s = float(score)
        if s >= 7:  return "#22c55e"
        if s >= 5:  return "#f59e0b"
        return "#ef4444"
    except Exception:
        return "#94a3b8"


def generate_interview_report(interview_id: int,
                               recording_path: Optional[str] = None,
                               run_id: str = "") -> Path:
    """Generate a self-contained HTML report for one interview. Returns the file path."""
    conn = _get_db()
    interview = _fetch_interview(conn, interview_id)
    responses = _fetch_responses(conn, interview_id)
    conn.close()

    if not interview:
        logger.warning("Interview %d not found in DB", interview_id)
        return None

    name       = interview.get('candidate_name') or "Unknown"
    job_title  = interview.get("job_title", "N/A")
    room_id    = interview.get("room_id", "N/A")
    status     = interview.get("interview_status", "N/A")
    started_at = interview.get("updated_at", "")
    email      = interview.get("candidate_email", "")

    # Overall score — from interviews.interview_score
    overall_score = interview.get("interview_score")
    if overall_score is not None:
        try:
            avg_score = round(float(overall_score), 2)
        except Exception:
            avg_score = "N/A"
    else:
        # Fallback: average confidence_score from questions
        scores = [r.get("confidence_score") for r in responses if r.get("confidence_score") is not None]
        try:
            avg_score = round(sum(float(s) for s in scores) / len(scores), 2) if scores else "N/A"
        except Exception:
            avg_score = "N/A"

    # Recording section
    if recording_path and Path(recording_path).exists():
        rec_html = f"""
        <div class="section recording">
          <h2>🎬 Recording</h2>
          <video controls width="100%" style="border-radius:8px;max-height:400px;">
            <source src="file://{recording_path}" type="video/mp4">
            Your browser does not support the video tag.
          </video>
          <p class="meta">File: <code>{recording_path}</code></p>
        </div>"""
    elif recording_path:
        rec_html = f'<div class="section recording"><h2>🎬 Recording</h2><p class="meta">Recording file pending: <code>{recording_path}</code></p></div>'
    else:
        rec_html = '<div class="section recording"><h2>🎬 Recording</h2><p class="meta">Recording not available for this interview.</p></div>'

    # Q&A rows from interview_questions_live
    qa_rows = ""
    for r in responses:
        sc = r.get("confidence_score", "N/A")
        color = _score_color(sc)
        answer = r.get('answer_text', '') or ''
        summary_text = r.get('summary', '') or ''
        skill_name = r.get('skill', '') or ''
        qa_rows += f"""
        <tr>
          <td class="qnum">Q{r.get('question_number','?')}</td>
          <td class="qtext">{r.get('question_text','')}</td>
          <td class="answer">{answer[:500]}</td>
          <td class="score" style="color:{color};font-weight:700">{sc}</td>
          <td class="skill">{skill_name}</td>
          <td class="notes">{summary_text[:300]}</td>
        </tr>"""

    avg_color = _score_color(avg_score)
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIRA Interview Report — {name}</title>
<style>
  :root {{
    --bg:#0f172a; --surface:#1e293b; --border:#334155;
    --text:#e2e8f0; --muted:#94a3b8;
    --green:#22c55e; --yellow:#f59e0b; --red:#ef4444; --blue:#38bdf8;
    --accent:#6366f1;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text); padding:32px; }}
  h1 {{ font-size:1.8rem; font-weight:700; margin-bottom:4px; }}
  h2 {{ font-size:1.1rem; font-weight:600; color:var(--blue); margin-bottom:16px; }}
  .header {{ border-bottom:1px solid var(--border); padding-bottom:24px; margin-bottom:24px; }}
  .meta {{ font-size:.85rem; color:var(--muted); margin-top:4px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:.8rem; font-weight:600; background:var(--accent); color:#fff; margin-left:8px; }}
  .score-hero {{ font-size:3rem; font-weight:800; color:{avg_color}; }}
  .section {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:24px; }}
  .info-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); gap:16px; }}
  .info-item label {{ font-size:.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }}
  .info-item p {{ font-size:.95rem; font-weight:500; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
  th {{ background:#0f172a; color:var(--muted); text-align:left; padding:10px 12px; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; border-bottom:1px solid var(--border); }}
  td {{ padding:12px; border-bottom:1px solid var(--border); vertical-align:top; }}
  tr:last-child td {{ border-bottom:none; }}
  .qnum {{ font-weight:700; color:var(--accent); width:40px; }}
  .qtext {{ color:var(--muted); width:20%; }}
  .answer {{ width:30%; }}
  .score {{ width:70px; text-align:center; font-size:1.05rem; }}
  .skill {{ width:120px; font-size:.8rem; color:var(--blue); }}
  .notes {{ width:25%; font-size:.82rem; color:var(--muted); }}
  .time {{ width:60px; text-align:right; color:var(--muted); }}
  footer {{ text-align:center; color:var(--muted); font-size:.8rem; margin-top:32px; }}
  .recording video {{ box-shadow:0 4px 24px rgba(0,0,0,.5); }}
  code {{ background:#0f172a; padding:2px 6px; border-radius:4px; font-size:.8rem; }}
</style>
</head>
<body>
<div class="header">
  <h1>AIRA Interview Report <span class="badge">{status}</span></h1>
  <p class="meta">Generated: {now_str} &nbsp;|&nbsp; Run: {run_id} &nbsp;|&nbsp; Room: {room_id}</p>
</div>

<div class="section">
  <div class="info-grid">
    <div class="info-item"><label>Candidate</label><p>{name}</p></div>
    <div class="info-item"><label>Email</label><p>{email or '—'}</p></div>
    <div class="info-item"><label>Role</label><p>{job_title}</p></div>
    <div class="info-item"><label>Interview ID</label><p>#{interview_id}</p></div>
    <div class="info-item"><label>Status</label><p>{status}</p></div>
    <div class="info-item"><label>Completed</label><p>{started_at}</p></div>
    <div class="info-item">
      <label>Overall Score</label>
      <p class="score-hero">{avg_score}<span style="font-size:1.2rem;color:var(--muted)">/10</span></p>
    </div>
    <div class="info-item"><label>Questions</label><p>{len(responses)}</p></div>
  </div>
</div>

{rec_html}

<div class="section">
  <h2>📋 Question-by-Question Analysis</h2>
  {"<p class='meta'>No responses recorded.</p>" if not responses else f'''
  <table>
    <thead>
      <tr><th>#</th><th>Question</th><th>Answer</th><th>Score</th><th>Skill</th><th>Summary</th></tr>
    </thead>
    <tbody>{qa_rows}</tbody>
  </table>'''}
</div>

<footer>AIRA Engine Load Test &nbsp;·&nbsp; {now_str}</footer>
</body>
</html>"""

    out_path = SIM_REPORTS_DIR / f"interview_{interview_id}_{room_id}.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("📄 Report written: %s", out_path)
    return out_path


# ── Master summary report ─────────────────────────────────────────────────────
def generate_master_report(results: list[dict], run_id: str = "") -> Path:
    """
    Generate a master HTML table of all interviews in this simulation run.

    results: list of dicts with keys:
        interview_id, room_id, candidate_name, status, avg_score,
        questions_asked, recording_path, report_path, duration_s, error
    """
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total    = len(results)
    completed = sum(1 for r in results if not r.get("error"))
    scores   = [r["avg_score"] for r in results if isinstance(r.get("avg_score"), (int, float))]
    overall_avg = round(sum(scores) / len(scores), 2) if scores else "N/A"

    rows = ""
    for r in results:
        sc    = r.get("avg_score", "N/A")
        color = _score_color(sc)
        rep   = r.get("report_path")
        rec   = r.get("recording_path")
        rep_link = f'<a href="file://{rep}" style="color:#38bdf8">📄 Report</a>' if rep else "—"
        rec_link = f'<a href="file://{rec}" style="color:#38bdf8">🎬 Video</a>' if rec and Path(rec).exists() else ("⏳ Pending" if rec else "—")
        err   = r.get("error", "")
        rows += f"""
        <tr>
          <td>#{r.get('interview_id','?')}</td>
          <td>{r.get('candidate_name','')}</td>
          <td><code style="font-size:.8rem">{r.get('room_id','')}</code></td>
          <td>{r.get('status','')}</td>
          <td style="color:{color};font-weight:700;text-align:center">{sc}</td>
          <td style="text-align:center">{r.get('questions_asked','?')}</td>
          <td style="text-align:center">{r.get('duration_s',''):.0f}s</td>
          <td style="text-align:center">{rep_link}</td>
          <td style="text-align:center">{rec_link}</td>
          <td style="color:#ef4444;font-size:.8rem">{err}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIRA Simulation Master Report — {run_id}</title>
<style>
  :root {{
    --bg:#0f172a; --surface:#1e293b; --border:#334155;
    --text:#e2e8f0; --muted:#94a3b8; --blue:#38bdf8; --accent:#6366f1;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:'Inter',system-ui,sans-serif; background:var(--bg); color:var(--text); padding:32px; }}
  h1 {{ font-size:2rem; font-weight:800; margin-bottom:4px; }}
  .meta {{ font-size:.85rem; color:var(--muted); margin-bottom:24px; }}
  .stats {{ display:flex; gap:24px; margin-bottom:32px; flex-wrap:wrap; }}
  .stat {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px 28px; min-width:140px; }}
  .stat label {{ font-size:.75rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }}
  .stat p {{ font-size:2rem; font-weight:800; margin-top:4px; color:var(--blue); }}
  table {{ width:100%; border-collapse:collapse; background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; }}
  th {{ background:#0f172a; color:var(--muted); text-align:left; padding:12px 14px; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em; }}
  td {{ padding:12px 14px; border-bottom:1px solid var(--border); font-size:.9rem; vertical-align:middle; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:rgba(99,102,241,.05); }}
  code {{ background:#0f172a; padding:2px 6px; border-radius:4px; font-size:.78rem; }}
  a {{ text-decoration:none; }}
  footer {{ text-align:center; color:var(--muted); font-size:.8rem; margin-top:32px; }}
</style>
</head>
<body>
<h1>🤖 AIRA Simulation Report</h1>
<p class="meta">Run ID: <strong>{run_id}</strong> &nbsp;·&nbsp; Generated: {now_str}</p>

<div class="stats">
  <div class="stat"><label>Total Rooms</label><p>{total}</p></div>
  <div class="stat"><label>Completed</label><p>{completed}</p></div>
  <div class="stat"><label>Failed</label><p>{total - completed}</p></div>
  <div class="stat"><label>Avg Score</label><p>{overall_avg}</p></div>
</div>

<table>
  <thead>
    <tr>
      <th>ID</th><th>Candidate</th><th>Room</th><th>Status</th>
      <th>Score</th><th>Qs</th><th>Duration</th>
      <th>Report</th><th>Recording</th><th>Error</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>

<footer>AIRA Engine Load Test &nbsp;·&nbsp; {now_str}</footer>
</body>
</html>"""

    out_path = SIM_REPORTS_DIR / f"master_report_{run_id}.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("📊 Master report written: %s", out_path)
    return out_path


# ── CLI entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--interview-id", type=int, help="Generate report for a single interview")
    parser.add_argument("--run-id", default="manual", help="Run ID label")
    args = parser.parse_args()

    if args.interview_id:
        path = generate_interview_report(args.interview_id, run_id=args.run_id)
        print(f"Report: {path}")
