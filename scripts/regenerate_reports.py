#!/usr/bin/env python3
"""
Report Regenerator — Generates Summary, Detailed & Sentinel PDF reports from DB data.

Looks up interview_history by room_id, fetches structured data from interview_report_data,
reconstructs template_data, renders HTML via Jinja2, converts to PDF, and saves to reports_saved/.

Usage:
    python regenerate_reports.py AIRA-20260410-0606-3DAA
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
logger = logging.getLogger("regenerate_reports")

from interview_template_handler import InterviewReportTemplate
from integrations.MSEmailClient import convert_interview_html_to_pdf
import sentinel_report_data_processor
import integrations.aira_sql as airadb

# ─── Output folder ───
OUTPUT_DIR = "reports_saved"


def fetch_report_context(room_id: str):
    """
    Fetch all data needed to regenerate reports for a given room_id.

    Joins:  interview_history (by room_id) → interview_report_data (by interview_id)
            interview_history → candidates → jobs

    Returns:
        tuple: (report_data, history_dict, candidate_dict, job_dict) or Nones
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

        # 2. Get structured report data from interview_report_data
        report_data = airadb.get_interview_report_data(interview_id)
        if not report_data:
            logger.error(f"❌ No interview_report_data found for interview_id={interview_id}")
            return None, None, None, None
        logger.info(f"📊 Loaded report data: score={report_data.get('interview_final_score')}, verdict={report_data.get('verdict_status')}")

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
            "resume_match_score": history.resume_match_score,
            "resume_job_analysis": history.resume_job_analysis,
            "recruiter_name": history.recruiter_name,
            "recruiter_email": history.recruiter_email,
            "mandatory_skills": history.mandatory_skills,
            "has_mandatory_skills": history.has_mandatory_skills,
            "best_face": history.best_face,
            "authorship_scores_summary": history.authorship_scores_summary,
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

        return report_data, history_dict, candidate_dict, job_dict

    except Exception as e:
        logger.error(f"❌ Error fetching report context: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None, None, None, None
    finally:
        session.close()


# ─── Metric colour mapping (stripped on save for ETL, restored here for rendering) ───
METRIC_COLORS = {
    "Technical Accuracy": "#4a90e2",
    "Problem Solving": "#50c878",
    "Communication": "#ff9500",
    "Completeness": "#9b59b6",
    "Practical Application": "#e74c3c",
    "Code Quality": "#3498db",
}


def _add_colors(metrics_list):
    """Restore hex colours to a metrics list for template rendering."""
    return [
        {"name": m.get("name", ""), "score": m.get("score", 0),
         "color": METRIC_COLORS.get(m.get("name", ""), "#666")}
        for m in (metrics_list or [])
    ]


def build_template_data(report_data, history, candidate, job):
    """
    Reconstruct the full template_data dict (identical shape to generate_template_data output)
    from the structured DB columns + FK-joined tables.
    """

    # ── Candidate photo (bytea → base64) ──
    candidate_photo_base64 = ""
    if history.get("best_face"):
        try:
            raw = history["best_face"]
            # Normalize bytea: handle memoryview, hex-encoded, plain bytes
            if isinstance(raw, memoryview):
                raw = bytes(raw)
            elif isinstance(raw, str):
                if raw.startswith('\\x'):
                    raw = bytes.fromhex(raw[2:])
                else:
                    raw = raw.encode('latin-1')
            else:
                raw = bytes(raw)
            if raw and len(raw) > 0:
                encoded = base64.b64encode(raw).decode("utf-8")
                candidate_photo_base64 = f"data:image/jpeg;base64,{encoded}"
        except Exception:
            pass

    # ── Resume score normalisation ──
    resume_score = report_data.get("resume_analysis_score", 0.0) or 0.0
    if resume_score > 5:
        resume_score /= 20.0
    resume_score = min(5.0, max(0.0, resume_score))

    final_score = report_data.get("interview_final_score", 0.0) or 0.0
    passed = report_data.get("verdict_passed", False)
    verdict_text = report_data.get("verdict_status", "FAILED INTERVIEW ROUND")

    strengths = report_data.get("strengths", []) or []
    weaknesses = report_data.get("weaknesses", []) or []

    # ── Restore colours on per-question metrics ──
    questions_list = report_data.get("questions_data", []) or []
    for q in questions_list:
        q["metrics"] = _add_colors(q.get("metrics", []))
    avg_metrics = _add_colors(report_data.get("avg_metrics", []))

    conversation_timeline = report_data.get("conversation_timeline", []) or []

    interview_time = history.get("interview_time") or datetime.now()
    if isinstance(interview_time, str):
        try:
            interview_time = datetime.strptime(interview_time, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            interview_time = datetime.now()

    template_handler = InterviewReportTemplate()
    aira_logo_url = template_handler._get_aira_logo()

    template_data = {
        "header": {
            "title": "AIRA",
            "subtitle": "Your AI Hiring Agent<br>Autonomous Interview & Resume Analysis"
        },
        "candidate": {
            "name": candidate.get("candidate_name", "Unknown"),
            "email": candidate.get("candidate_email", ""),
            "phone": "None",
            "photo_url": candidate_photo_base64
        },
        "client": {
            "company": job.get("company_name", "Unknown Company"),
            "position": job.get("job_title", "Unknown Position")
        },
        "interview": {
            "date": interview_time.strftime("%Y-%m-%d"),
            "time": interview_time.strftime("%I:%M %p"),
            "timezone": history.get("interview_timezone", "UTC"),
            "duration": report_data.get("interview_duration", "N/A"),
            "questions_count": report_data.get("questions_asked_count", 0),
            "total_questions": report_data.get("total_questions_configured", 15),
            "mode": history.get("interview_mode"),
            "room_id": history.get("room_id", "")
        },
        "resume_analysis": {
            "score": round(resume_score, 1),
            "recommendation": report_data.get("resume_analysis_text") or "Resume analysis not available."
        },
        "interview_analysis": {
            "score": round(final_score, 1),
            "recommendation": report_data.get("interview_analysis_text") or "Interview analysis not available."
        },
        "verdict": {
            "passed": passed,
            "icon": "✓" if passed else "✗",
            "status": verdict_text,
            "subtitle": (
                "Recommend proceeding to the next stage of the interview process."
                if passed else
                "Candidate requires further evaluation or training before proceeding."
            ),
            "strengths": {
                "title": "✓ Key Strengths",
                "items": strengths[:5] if strengths else ["Technical Knowledge", "Experience"]
            },
            "areas": {
                "title": "⚠ Areas for Development",
                "items": weaknesses[:3] if weaknesses else ["Communication", "Confidence"]
            },
            "overall_assessment": report_data.get("overall_assessment") or "Assessment not available."
        },
        "overview": {
            "summary": report_data.get("performance_summary") or "Performance overview not available.",
            "metrics": avg_metrics
        },
        "mandatory": {
            "strengths": strengths[:3] if strengths else ["Technical Skills"],
            "improvements": weaknesses[:3] if weaknesses else ["Experience"],
            "has_mandatory_skills": report_data.get("has_mandatory_skills", False),
            "mandatory_skills": report_data.get("mandatory_skills_text"),
            "disclaimer": "This report is generated by AIRA (Your AI Hiring Agent) based on transcript, audio analysis, and resume parsing. It is intended for internal recruitment use only and should be reviewed by a human prior to final hiring decisions."
        },
        "questions": {
            "header": {
                "title": "AIRA - Detailed Interview Analysis Report",
                "subtitle": "Comprehensive Question-by-Question Assessment",
                "description": "This section contains detailed analysis and scoring for each technical question asked during the interview"
            },
            "list": questions_list,
            "conversation_timeline": conversation_timeline
        },
        "footer": {
            "text": "This report is generated by AIRA (Your AI Hiring Agent). For questions or support, contact support@airahr.ai"
        },
        "aira_logo_url": aira_logo_url
    }

    # ── Optional sections ──
    mandatory_eval = report_data.get("mandatory_skills_evaluation")
    if mandatory_eval:
        template_data["mandatory_skills_evaluation"] = mandatory_eval

    authorship = report_data.get("authorship_analysis")
    if authorship:
        template_data["authorship_analysis"] = authorship

    behavioral = report_data.get("behavioral_metrics")
    template_data["behavioral_metrics"] = behavioral or {
        "authenticity_score": None,
        "tabs_switched": 0,
        "face_swaps_detected": 0,
        "multiple_faces_detected": 0,
        "sentinel_score": None,
    }

    return template_data


def regenerate_reports(room_id: str):
    """
    Main entry: look up room_id → fetch data → render HTML → convert to PDF → save.

    Saves 3 PDFs (Summary, Detailed, Sentinel) to reports_saved/<room_id>/.
    """
    logger.info("=" * 70)
    logger.info(f"📄 REPORT REGENERATION — room_id={room_id}")
    logger.info("=" * 70)

    # ── 1  Fetch structured data ──
    report_data, history, candidate, job = fetch_report_context(room_id)
    if not report_data or not history:
        logger.error("❌ Cannot regenerate — missing data")
        return None

    candidate_name = candidate.get("candidate_name", "Unknown")
    logger.info(
        f"📊 Candidate={candidate_name}, "
        f"Position={job.get('job_title')}, "
        f"Score={report_data.get('interview_final_score')}, "
        f"Verdict={report_data.get('verdict_status')}"
    )

    # ── 2  Build template_data ──
    template_data = build_template_data(report_data, history, candidate, job)

    # ── 3  Render HTML (Summary + Detailed) ──
    template_handler = InterviewReportTemplate()
    summary_html = template_handler.summary_template.render(**template_data)
    detailed_html = template_handler.template.render(**template_data)
    logger.info(f"✅ Summary HTML rendered: {len(summary_html):,} chars")
    logger.info(f"✅ Detailed HTML rendered: {len(detailed_html):,} chars")

    # ── 4  Render Sentinel HTML ──
    sentinel_html = None
    try:
        sentinel_html = sentinel_report_data_processor.start_generation(room_id)
        if sentinel_html:
            logger.info(f"✅ Sentinel HTML rendered: {len(sentinel_html):,} chars")
        else:
            logger.warning("⚠️ Sentinel report returned empty — skipping sentinel PDF")
    except Exception as e:
        logger.warning(f"⚠️ Sentinel report generation failed (non-critical): {e}")

    # ── 5  Create output directory ──
    out_dir = os.path.join(OUTPUT_DIR, room_id)
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = candidate_name.replace(" ", "_")

    results = {}

    # ── 6  Convert to PDF — Summary ──
    summary_pdf = os.path.join(out_dir, f"{safe_name}_Summary_Report_{timestamp}.pdf")
    pdf_path = convert_interview_html_to_pdf(summary_html, summary_pdf, candidate_name)
    if pdf_path:
        results["summary_pdf"] = pdf_path
        logger.info(f"✅ Summary  PDF saved: {pdf_path}")
    else:
        logger.error("❌ Summary PDF conversion failed")

    # ── 7  Convert to PDF — Detailed ──
    detailed_pdf = os.path.join(out_dir, f"{safe_name}_Detailed_Report_{timestamp}.pdf")
    pdf_path = convert_interview_html_to_pdf(detailed_html, detailed_pdf, candidate_name)
    if pdf_path:
        results["detailed_pdf"] = pdf_path
        logger.info(f"✅ Detailed PDF saved: {pdf_path}")
    else:
        logger.error("❌ Detailed PDF conversion failed")

    # ── 8  Convert to PDF — Sentinel ──
    if sentinel_html:
        sentinel_pdf = os.path.join(out_dir, f"{safe_name}_Sentinel_Report_{timestamp}.pdf")
        pdf_path = convert_interview_html_to_pdf(sentinel_html, sentinel_pdf, candidate_name)
        if pdf_path:
            results["sentinel_pdf"] = pdf_path
            logger.info(f"✅ Sentinel PDF saved: {pdf_path}")
        else:
            logger.error("❌ Sentinel PDF conversion failed")

    # ── 9  Also save raw HTML (useful for debugging) ──
    for name, html in [("summary", summary_html), ("detailed", detailed_html)]:
        html_path = os.path.join(out_dir, f"{safe_name}_{name}_report_{timestamp}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
    if sentinel_html:
        html_path = os.path.join(out_dir, f"{safe_name}_sentinel_report_{timestamp}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(sentinel_html)

    logger.info("=" * 70)
    logger.info(f"✅ ALL REPORTS SAVED → {out_dir}/")
    logger.info("=" * 70)

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Regenerate interview reports (PDF) from interview_report_data table",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python regenerate_reports.py AIRA-20260410-0606-3DAA
        """
    )
    parser.add_argument("room_id", type=str, help="Room ID to regenerate reports for (e.g. AIRA-20260410-0606-3DAA)")

    args = parser.parse_args()

    print("\n" + "=" * 60)
    print(f"  AIRA Report Regenerator")
    print(f"  Room ID: {args.room_id}")
    print("=" * 60)

    results = regenerate_reports(args.room_id)

    if results:
        print(f"\n✅ Reports generated successfully in {OUTPUT_DIR}/{args.room_id}/:")
        for name, path in results.items():
            size_kb = os.path.getsize(path) / 1024
            print(f"   📋 {name}: {os.path.basename(path)} ({size_kb:.0f} KB)")
    else:
        print("\n❌ Report regeneration failed. Check logs above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
