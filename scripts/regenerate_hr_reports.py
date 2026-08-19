#!/usr/bin/env python3
"""
HR Report Regenerator — Generates HR Behavioral Interview PDF reports from DB data.

Looks up interview_history by room_id, fetches structured HR data from hr_interview_report_data,
reconstructs template_data, renders HTML via Jinja2, converts to PDF, and saves to reports_saved/.

Usage:
    python regenerate_hr_reports.py AIRA-20260430-1604-9A4C
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import os
import sys
import json
import logging
import argparse
import base64
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

from logger_config import setup_logging
setup_logging()
logger = logging.getLogger("regenerate_hr_reports")

from integrations.MSEmailClient import convert_interview_html_to_pdf
import integrations.aira_sql as airadb
from jinja2 import Environment, FileSystemLoader

# ─── Output folder ───
OUTPUT_DIR = "reports_saved"


def fetch_hr_report_context(room_id: str):
    """
    Fetch all data needed to regenerate HR reports for a given room_id.

    Joins:  interview_history (by room_id) → hr_interview_report_data (by interview_id)
            interview_history → candidates → jobs

    Returns:
        tuple: (hr_report_data, history_dict, candidate_dict, job_dict) or Nones
    """
    session = airadb.Session()
    try:
        # 1. Find interview_history by room_id
        history = session.query(airadb.InterviewHistory).filter_by(room_id=room_id).first()
        if not history:
            logger.error(f"❌ No interview_history record found for room_id={room_id}")
            return None, None, None, None

        interview_id = history.interview_id
        logger.info(f"📌 Found interview_history: id={history.id}, interview_id={interview_id}, room_id={room_id}")

        # 2. Get structured HR report data
        hr_report_data = airadb.get_hr_report_data(interview_id)
        if not hr_report_data:
            logger.error(f"❌ No hr_interview_report_data found for interview_id={interview_id}")
            return None, None, None, None
        logger.info(
            f"📊 Loaded HR report data: score={hr_report_data.get('overall_score')}, "
            f"verdict={hr_report_data.get('verdict_status')}"
        )

        # 3. Get candidate
        candidate = session.query(airadb.Candidate).filter_by(id=history.candidate_id).first()

        # 4. Get job
        job = session.query(airadb.Job).filter_by(id=history.job_id).first()

        # ── Convert to dicts ──
        history_dict = {
            "interview_id": history.interview_id,
            "candidate_id": history.candidate_id,
            "job_id": history.job_id,
            "room_id": history.room_id,
            "interview_time": history.interview_time,
            "interview_timezone": history.interview_timezone,
            "interview_mode": history.interview_mode,
            "interview_score": history.interview_score,
            "interview_verdict": history.interview_verdict,
            "interview_status": history.interview_status,
            "recruiter_name": history.recruiter_name,
            "recruiter_email": history.recruiter_email,
        }

        candidate_dict = {
            "id": candidate.id,
            "candidate_name": candidate.candidate_name,
            "candidate_email": candidate.candidate_email,
            "candidate_jd_id": candidate.candidate_jd_id,
        } if candidate else {"candidate_name": "Unknown", "candidate_email": "", "candidate_jd_id": ""}

        job_dict = {
            "id": job.id,
            "job_title": job.job_title,
            "job_description": job.job_description,
            "company_name": job.company_name,
        } if job else {"job_title": "Unknown Position", "company_name": "Unknown Company"}

        return hr_report_data, history_dict, candidate_dict, job_dict

    except Exception as e:
        logger.error(f"❌ Error fetching HR report context: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, None, None, None
    finally:
        session.close()


def _get_logo_base64() -> str:
    """Try to load the AIRA logo as base64."""
    logo_paths = [
        os.path.join("interview_reports", "AIRA_LOGO.jpg"),
        os.path.join("images", "AIRA_LOGO.jpg"),
        "AIRA_LOGO.jpg",
    ]
    for path in logo_paths:
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    return base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                pass
    return ""


def build_hr_template_data(hr_data, history, candidate, job):
    """
    Reconstruct the full template_data dict for the HR Jinja2 template
    from the structured DB columns + FK-joined tables.
    """
    interview_time = history.get("interview_time") or datetime.now()
    if isinstance(interview_time, str):
        try:
            interview_time = datetime.strptime(interview_time, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            interview_time = datetime.now()

    overall_score = hr_data.get("overall_score", 0)
    verdict_passed = hr_data.get("verdict_passed", False)

    template_data = {
        "candidate_name": candidate.get("candidate_name", "Unknown"),
        "candidate_email": candidate.get("candidate_email", ""),
        "job_title": job.get("job_title", "Unknown Position"),
        "interview_date": interview_time.strftime("%Y-%m-%d %H:%M"),
        "overall_score": overall_score,
        "summary": hr_data.get("summary") or "Evaluation not available.",
        "topics_covered": hr_data.get("topics_covered", 0),
        "topics_total": hr_data.get("topics_total", 0),
        "total_responses": hr_data.get("total_responses", 0),
        "explorations": hr_data.get("explorations", 0),
        "duration_minutes": hr_data.get("duration_minutes", 0),
        "completion_status": hr_data.get("completion_status", "unknown"),
        "phase": hr_data.get("phase", 1),
        "dimension_scores": hr_data.get("dimension_scores", []),
        "strengths": hr_data.get("strengths", []),
        "concerns": hr_data.get("concerns", []),
        "transcript": hr_data.get("transcript", []),
        "logo_base64": _get_logo_base64(),
    }

    return template_data


def regenerate_hr_reports(room_id: str):
    """
    Main entry: look up room_id → fetch HR data → render HTML → convert to PDF → save.

    Saves HR Report PDF to reports_saved/<room_id>/.
    """
    logger.info("=" * 70)
    logger.info(f"📄 HR REPORT REGENERATION — room_id={room_id}")
    logger.info("=" * 70)

    # ── 1  Fetch structured data ──
    hr_data, history, candidate, job = fetch_hr_report_context(room_id)
    if not hr_data or not history:
        logger.error("❌ Cannot regenerate HR report — missing data")
        return None

    candidate_name = candidate.get("candidate_name", "Unknown")
    logger.info(
        f"📊 Candidate={candidate_name}, "
        f"Position={job.get('job_title')}, "
        f"Score={hr_data.get('overall_score')}/5, "
        f"Verdict={hr_data.get('verdict_status')}"
    )

    # ── 2  Build template_data ──
    template_data = build_hr_template_data(hr_data, history, candidate, job)

    # ── 3  Render HTML ──
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template("aira_hr_report_template.html.j2")
    html_content = template.render(**template_data)
    logger.info(f"✅ HR Report HTML rendered: {len(html_content):,} chars")

    # ── 4  Create output directory ──
    out_dir = os.path.join(OUTPUT_DIR, room_id)
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = candidate_name.replace(" ", "_")

    results = {}

    # ── 5  Convert to PDF ──
    hr_pdf = os.path.join(out_dir, f"{safe_name}_HR_Report_{timestamp}.pdf")
    pdf_path = convert_interview_html_to_pdf(html_content, hr_pdf, candidate_name)
    if pdf_path:
        results["hr_pdf"] = pdf_path
        logger.info(f"✅ HR Report PDF saved: {pdf_path}")
    else:
        logger.error("❌ HR Report PDF conversion failed")

    # ── 6  Save raw HTML too ──
    html_path = os.path.join(out_dir, f"{safe_name}_hr_report_{timestamp}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    results["hr_html"] = html_path
    logger.info(f"✅ HR Report HTML saved: {html_path}")

    logger.info("=" * 70)
    logger.info(f"✅ HR REPORT SAVED → {out_dir}/")
    logger.info("=" * 70)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Regenerate HR behavioral interview reports (PDF) from hr_interview_report_data table",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python regenerate_hr_reports.py AIRA-20260430-1604-9A4C
        """
    )
    parser.add_argument("room_id", type=str, help="Room ID to regenerate HR reports for (e.g. AIRA-20260430-1604-9A4C)")

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(f"  AIRA HR Report Regenerator")
    print(f"  Room ID: {args.room_id}")
    print("=" * 60)

    results = regenerate_hr_reports(args.room_id)

    if results:
        print(f"\n✅ HR Reports generated successfully in {OUTPUT_DIR}/{args.room_id}/:")
        for name, path in results.items():
            size_kb = os.path.getsize(path) / 1024
            print(f"   📋 {name}: {os.path.basename(path)} ({size_kb:.0f} KB)")
    else:
        print("\n❌ HR Report regeneration failed. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
