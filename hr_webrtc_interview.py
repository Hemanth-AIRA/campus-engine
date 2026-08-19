"""
HR Interview — WebRTC Loop

This module implements the HR behavioral interview as a WebRTC conversation.
It reuses the existing ai_speak() and capture infrastructure from webrtc_interview,
but drives the conversation using the HRInterviewEngine instead of the technical
question planner.

Called from ask_questions_in_webrtc when round_type == "hr".
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader

from hr_interview_engine import HRInterviewEngine, HREvent
from hr_interview_config import HRInterviewConfig
from hr_evaluator import evaluate_hr_interview, compute_hr_score

logger = logging.getLogger("HRWebRTCInterview")


def _extract_answer(capture_result) -> str:
    """Extract the answer text from capture_fn result.
    
    In production, _capture_normal returns a dict: {"answer": "...", "answer_duration": ...}
    In simulation, it might return a plain string.
    """
    if capture_result is None:
        return ""
    if isinstance(capture_result, dict):
        return capture_result.get("answer", "") or ""
    if isinstance(capture_result, str):
        return capture_result
    return str(capture_result)


def run_hr_interview(
    interview: dict,
    parsed_config: dict,
    driver,
    room_id: str,
    process_id: int,
    interview_id: int,
    candidate_name: str,
    job_description: str,
    conversation_timeline: list,
    ai_speak_fn,
    capture_fn,
    add_timeline_fn,
    thread_manager=None,
):
    """
    Run the HR behavioral interview loop inside WebRTC.

    This function replaces the technical question loop when round_type == "hr".
    It uses the same ai_speak/capture infrastructure but drives conversation
    through the HRInterviewEngine.

    Args:
        interview: Full interview dict from DB
        parsed_config: The interview_config JSON (already parsed)
        driver: WebRTC Selenium driver
        room_id: Room ID for WebRTC
        process_id: Process ID for logging
        interview_id: Interview ID in DB
        candidate_name: Candidate's name
        job_description: JD text
        conversation_timeline: Shared timeline list
        ai_speak_fn: Reference to ai_speak() from webrtc_interview
        capture_fn: Reference to _capture_normal() from webrtc_interview
        add_timeline_fn: Reference to add_to_conversation_timeline()
        thread_manager: InterviewThreadManager instance (optional)

    Returns:
        dict: {
            "transcript": [...],
            "evaluation": {...},
            "completion_status": {...},
            "conversation_timeline": [...],
        }
    """
    log_prefix = f"[process id: {process_id}][room id: {room_id}]"
    openai_client = OpenAI()

    # ── Build HR config from interview_config JSON ──────────────────
    hr_config_raw = parsed_config.get("hr_config", {})
    topics = hr_config_raw.get("topics", [
        "intro", "ownership", "conflict", "teamwork",
        "motivation", "adaptability", "reflection",
    ])
    hr_config = HRInterviewConfig(
        max_duration_minutes=hr_config_raw.get("max_duration", 20),
        min_duration_minutes=hr_config_raw.get("min_duration", 8),
        min_responses=hr_config_raw.get("min_responses", 6),
        topics=topics,
    )
    hr_config.log_config(prefix=f"{log_prefix} ")

    # ── Initialize engine ───────────────────────────────────────────
    engine = HRInterviewEngine(interview_id=interview_id, config=hr_config)

    logger.info(f"🎯 {log_prefix} HR Interview Engine started")

    # ── HR Greeting ─────────────────────────────────────────────────
    greeting = (
        f"Hey {candidate_name}, thanks so much for being here! So this part of the interview "
        f"is a little different from the technical side — there's no right or wrong answers, "
        f"no trick questions, nothing like that. I'm genuinely just looking to get to know you "
        f"as a person — how you think, how you work with people, what motivates you. "
        f"So just be yourself, take your time, and let's have a real conversation. Sound good?"
    )
    ai_speak_fn(greeting, driver, room_id, process_id)
    add_timeline_fn(
        conversation_timeline, "hr_greeting", greeting, "",
        None, room_id, process_id
    )

    # Wait for candidate acknowledgment
    try:
        ack_raw = capture_fn(driver, room_id, process_id)
        ack_answer = _extract_answer(ack_raw)
        if ack_answer and ack_answer.strip():
            add_timeline_fn(
                conversation_timeline, "hr_greeting_response", greeting,
                ack_answer, None, room_id, process_id
            )
            logger.info(f"👤 {log_prefix} Candidate greeting response: {ack_answer[:100]}")
    except Exception as e:
        logger.warning(f"⚠️ {log_prefix} Greeting capture failed (non-blocking): {e}")

    # ── Main HR conversation loop ───────────────────────────────────
    question_number = 0
    interview_start = time.time()
    max_duration_seconds = hr_config.max_duration_minutes * 60

    while not engine.is_complete():
        elapsed = time.time() - interview_start
        if elapsed >= max_duration_seconds:
            logger.info(f"⏰ {log_prefix} HR interview time limit reached: {elapsed/60:.1f}m")
            break

        # Select next topic
        topic = engine.select_next_topic()

        if topic == "closing":
            # Closing question
            question = engine.generate_question("closing")
            question_number += 1
            logger.info(f"🤖 {log_prefix} HR Q{question_number} [CLOSING]: {question[:80]}...")

            ai_speak_fn(question, driver, room_id, process_id)
            add_timeline_fn(
                conversation_timeline, f"hr_closing",
                question, "", question_number, room_id, process_id
            )

            # Capture closing answer
            try:
                answer_raw = capture_fn(driver, room_id, process_id)
                answer = _extract_answer(answer_raw)
                if answer and answer.strip():
                    engine.record_answer("closing", question, answer)
                    add_timeline_fn(
                        conversation_timeline, f"hr_closing_response",
                        question, answer, question_number, room_id, process_id
                    )
            except Exception as e:
                logger.warning(f"⚠️ {log_prefix} Closing capture failed: {e}")

            break

        # Generate question (Phase 1 or Phase 2)
        question = engine.generate_question(topic)

        if question is None:
            # Phase 2: LLM exploration
            question = engine.generate_exploration_question(topic, llm_client=openai_client)
            entry_type = "exploration"
            phase_label = "EXPLORE"
        else:
            entry_type = "primary"
            phase_label = topic.upper()

        question_number += 1
        logger.info(f"🤖 {log_prefix} HR Q{question_number} [{phase_label}]: {question[:80]}...")

        # Speak the question
        ai_speak_fn(question, driver, room_id, process_id)
        add_timeline_fn(
            conversation_timeline, f"hr_{topic}",
            question, "", question_number, room_id, process_id
        )

        # Capture candidate response
        try:
            answer_raw = capture_fn(driver, room_id, process_id)
            answer = _extract_answer(answer_raw)
        except Exception as e:
            logger.error(f"❌ {log_prefix} Capture failed for HR Q{question_number}: {e}")
            answer = ""

        if not answer or not answer.strip():
            logger.warning(f"⚠️ {log_prefix} Empty response for HR Q{question_number}")
            answer = "[No response]"

        logger.info(f"👤 {log_prefix} HR A{question_number}: {answer[:100]}...")

        add_timeline_fn(
            conversation_timeline, f"hr_{topic}_response",
            question, answer, question_number, room_id, process_id
        )

        # Record and check result: 'repeat', 'probe', or 'none'
        result = engine.record_answer(topic, question, answer, entry_type=entry_type)

        if result == "repeat":
            # Candidate asked to repeat — warmly re-ask the same question
            repeat_msg = engine.get_repeat_response(question)
            logger.info(f"🔄 {log_prefix} Repeating question for candidate")

            ai_speak_fn(repeat_msg, driver, room_id, process_id)
            add_timeline_fn(
                conversation_timeline, f"hr_{topic}_repeat",
                repeat_msg, "", question_number, room_id, process_id
            )

            try:
                repeat_raw = capture_fn(driver, room_id, process_id)
                repeat_answer = _extract_answer(repeat_raw)
                if repeat_answer and repeat_answer.strip():
                    engine.record_answer(topic, repeat_msg, repeat_answer, entry_type=entry_type)
                    add_timeline_fn(
                        conversation_timeline, f"hr_{topic}_repeat_response",
                        repeat_msg, repeat_answer, question_number, room_id, process_id
                    )
                    logger.info(f"👤 {log_prefix} HR Repeat A: {repeat_answer[:80]}...")
            except Exception as e:
                logger.warning(f"⚠️ {log_prefix} Repeat capture failed: {e}")

        elif result == "probe":
            probe = engine.get_followup_probe()
            logger.info(f"🔍 {log_prefix} HR Follow-up probe: {probe[:60]}...")

            ai_speak_fn(probe, driver, room_id, process_id)
            add_timeline_fn(
                conversation_timeline, f"hr_{topic}_probe",
                probe, "", question_number, room_id, process_id
            )

            try:
                followup_raw = capture_fn(driver, room_id, process_id)
                followup_answer = _extract_answer(followup_raw)
                if followup_answer and followup_answer.strip():
                    engine.record_followup(topic, probe, followup_answer)
                    add_timeline_fn(
                        conversation_timeline, f"hr_{topic}_probe_response",
                        probe, followup_answer, question_number, room_id, process_id
                    )
                    logger.info(f"👤 {log_prefix} HR Follow-up A: {followup_answer[:80]}...")
            except Exception as e:
                logger.warning(f"⚠️ {log_prefix} Follow-up capture failed: {e}")

    # ── Interview complete ──────────────────────────────────────────
    completion = engine.get_completion_status()
    logger.info(
        f"📋 {log_prefix} HR Interview Complete: "
        f"phase={completion.get('phase')}, "
        f"topics={completion['topics_covered']}/{completion['topics_total']}, "
        f"responses={completion['responses']}, "
        f"explorations={completion.get('explorations', 0)}"
    )

    # ── Farewell ────────────────────────────────────────────────────
    farewell = (
        f"Well {candidate_name}, I've really enjoyed getting to know you today. "
        f"You've shared some really thoughtful answers, and I appreciate you being "
        f"so open with me. Our team will be reviewing everything and we'll follow up "
        f"with next steps soon. In the meantime, thanks again for your time — "
        f"I hope you have a great rest of your day!"
    )
    ai_speak_fn(farewell, driver, room_id, process_id)
    add_timeline_fn(
        conversation_timeline, "hr_farewell", farewell, "",
        None, room_id, process_id
    )

    # ── Run evaluation ──────────────────────────────────────────────
    evaluation_result = None
    try:
        transcript_text = engine.get_transcript_text()

        # Load dimensions for evaluation
        from hr_interview_config import DEFAULT_DIMENSIONS
        dimensions = DEFAULT_DIMENSIONS

        evaluation_result = evaluate_hr_interview(
            transcript_text=transcript_text,
            dimensions=dimensions,
            job_title=interview.get("job_title", ""),
        )
        if evaluation_result:
            overall = compute_hr_score(evaluation_result)
            evaluation_result["overall_score"] = overall
            logger.info(
                f"✅ {log_prefix} HR Evaluation complete: score={overall}/5"
            )
    except Exception as e:
        logger.error(f"❌ {log_prefix} HR Evaluation failed: {e}", exc_info=True)

    # ── Generate HR Report ──────────────────────────────────────────
    report_path = None
    try:
        report_path = _generate_hr_report(
            interview=interview,
            engine=engine,
            completion=completion,
            evaluation=evaluation_result,
            interview_id=interview_id,
            candidate_name=candidate_name,
            job_description=job_description,
            log_prefix=log_prefix,
        )
        logger.info(f"📄 {log_prefix} HR Report saved: {report_path}")
    except Exception as e:
        logger.error(f"❌ {log_prefix} HR Report generation failed: {e}", exc_info=True)

    return {
        "transcript": engine.get_transcript(),
        "evaluation": evaluation_result,
        "completion_status": completion,
        "conversation_timeline": conversation_timeline,
        "report_path": report_path,
    }


def _generate_hr_report(
    interview: dict,
    engine,
    completion: dict,
    evaluation: dict,
    interview_id: int,
    candidate_name: str,
    job_description: str,
    log_prefix: str = "",
) -> str:
    """
    Render the HR report template and save to reports/ directory.
    Returns the file path.
    """
    # Load template
    template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template("aira_hr_report_template.html.j2")

    # Compute overall score
    from hr_evaluator import compute_hr_score
    overall_score = compute_hr_score(evaluation) if evaluation else 0

    # Build template data
    # Convert dimension scores from LLM's 1-10 scale → 0-5 for display consistency
    dimension_scores_5 = []
    for dim in (evaluation.get("scores", []) if evaluation else []):
        dimension_scores_5.append({
            "dimension_key": dim.get("dimension_key", ""),
            "dimension_name": dim.get("dimension_name", ""),
            "score": round(dim.get("score", 5) / 2.0, 1),  # 1-10 → 0.5-5
            "justification": dim.get("justification", ""),
        })

    template_data = {
        "candidate_name": candidate_name,
        "candidate_email": interview.get("candidate_email", ""),
        "job_title": interview.get("job_title", ""),
        "interview_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "overall_score": overall_score,
        "summary": evaluation.get("summary", "Evaluation not available.") if evaluation else "Evaluation not available.",
        "topics_covered": completion.get("topics_covered", 0),
        "topics_total": completion.get("topics_total", 0),
        "total_responses": completion.get("responses", 0),
        "explorations": completion.get("explorations", 0),
        "duration_minutes": completion.get("duration_minutes", 0),
        "completion_status": completion.get("status", "unknown"),
        "phase": completion.get("phase", 1),
        "dimension_scores": dimension_scores_5,
        "strengths": evaluation.get("strengths", []) if evaluation else [],
        "concerns": evaluation.get("concerns", []) if evaluation else [],
        "transcript": engine.get_transcript(),
        "logo_base64": _get_logo_base64(),
    }

    # Render HTML
    html_content = template.render(**template_data)

    # Save to reports/
    os.makedirs("reports", exist_ok=True)
    timestamp = int(time.time())
    candidate_id = interview.get("candidate_jd_id", interview_id)
    filename = f"reports/{candidate_id}_hr_report_{timestamp}.html"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    return filename


def _get_logo_base64() -> str:
    """Try to load the AIRA logo as base64."""
    try:
        import base64
        logo_path = os.path.join(os.path.dirname(__file__), "aira_logo.png")
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
    except Exception:
        pass
    return ""
