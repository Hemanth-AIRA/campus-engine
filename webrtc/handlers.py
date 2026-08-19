"""Presence / absence / mute handlers, face + video capture, and room operations.
Split out of webrtc.interview for modularity. Leaf module: depends on webrtc.audio,
webrtc.analysis, webrtc.livekit_adapter; the interview loop imports these back."""
import os
import re
import io
import time
import base64
import random
import logging
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import requests
import integrations.aira_sql as airadb
from answer_analysis_integration import trigger_answer_analysis_async
from webrtc.livekit_adapter import get_livekit_adapter, USE_CARTESIA
from webrtc.analysis import openai_client
from webrtc.audio import (ai_speak, _streaming_continuation, record_transcribe_webrtc_audio,
                          record_transcribe_webrtc_streaming, USE_CACHED_ACKS)

logger = logging.getLogger("webrtc.handlers")
DG_STREAMING = os.getenv("DG_STREAMING", "true").lower() == "true"
SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL", "")
base_url = os.getenv("CONNECT_BASE_URL", "")


def _capture_face_after_ack():
    """Open a best-face capture window just after an ack — the candidate just
    finished answering and is attentive/facing the camera. The engine's
    FaceDetector samples the next few frames and keeps the best face for the
    whole interview (saved to interviews.best_face at the end). Replaces the old
    Sentinel best-face extraction. Best-effort: never raises into the flow."""
    try:
        adapter = get_livekit_adapter()
        if adapter and getattr(adapter, 'bot', None) and adapter.bot._face_detector:
            adapter.bot._face_detector.request_capture()
    except Exception:
        pass


def _capture_face_during_answer():
    """Open an ANSWER-capture window — the candidate is answering a question and
    facing the camera. The engine's FaceDetector keeps the best answer-time face
    (preferred) plus a first-20s baseline, and the better one becomes best_face.
    Called while the candidate answers Q1 & Q2. Best-effort: never raises."""
    try:
        adapter = get_livekit_adapter()
        if adapter and getattr(adapter, 'bot', None) and adapter.bot._face_detector:
            adapter.bot._face_detector.request_answer_capture()
    except Exception:
        pass


def _write_best_face_file(room_id: str, process_id: int = 0):
    """Write the engine-captured best face to images/<room_id>_best_face.png so
    the report can embed it directly — no DB round-trip. The same file is read
    at archive time to persist best_face to the DB via the hub API. Returns the
    path on success, else None."""
    try:
        adapter = get_livekit_adapter()
        fd = adapter.bot._face_detector if (adapter and getattr(adapter, 'bot', None)) else None
        jpeg = fd.best_face_jpeg if fd else None
        if not jpeg:
            return None
        os.makedirs("images", exist_ok=True)
        path = os.path.join("images", f"{room_id}_best_face.png")
        # Captured as JPEG; re-encode to PNG so the .png path is valid for the
        # HTML→PDF renderer.
        Image.open(io.BytesIO(jpeg)).save(path, "PNG")
        logger.info("🛡[process id: %s][room id: %s] Best face written: %s (%d bytes jpeg)",
                    process_id, room_id, path, len(jpeg))
        return path
    except Exception as e:
        logger.warning("⚠️[process id: %s][room id: %s] Could not write best face file: %s",
                       process_id, room_id, e)
        return None


def read_best_face_bytes(room_id: str):
    """Read images/<room_id>_best_face.png as raw bytes (for DB persistence at
    archive time). Returns bytes or None."""
    try:
        path = os.path.join("images", f"{room_id}_best_face.png")
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


def get_room_participants(room_id,process_id):
    try:
        api_url = f"{base_url}/api/rooms/{room_id}"
        resp = requests.get(api_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('participants', [])
        else:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Room API returned {resp.status_code} for room {room_id}")
            return []
    except Exception as e:
        logger.warning(f"[process id: {process_id}][room id: {room_id}] Could not get room participants: {e}")
        return []


def check_video_status(room_id, process_id, skill_states=None):
    """
    Check if candidate's camera is turned off via API.
    
    Returns:
        bool: True if video is off, False if video is on
    """
    try:
        api_url = f"{base_url}/api/rooms/{room_id}"
        resp = requests.get(api_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            candidate_video_off = next(
                (participant['videoOff'] for participant in data['participants'] 
                 if participant['role'] == 'candidate'), 
                None
            )
            return candidate_video_off if candidate_video_off is not None else True
        else:
            logger.warning(
                f"[process id: {process_id}][room id: {room_id}] "
                f"Room API returned {resp.status_code} for room {room_id}"
            )
            return True
    except Exception as e:
        logger.warning(
            f"[process id: {process_id}][room id: {room_id}] "
            f"Could not get room participants: {e}"
        )
        return True


def check_face_visibility(process_id, room_id, interview_id):
    """
    Check if candidate's face is visible via the in-memory face detector.
    
    The face detector runs on LiveKit video frames inside aira_bot_client.py.
    No DB query needed — detection and consumption are in the same process.
    
    Returns:
        bool: True if face is visible, False if not visible
    """
    try:
        adapter = get_livekit_adapter()
        if adapter and hasattr(adapter, 'bot') and adapter.bot._face_detector:
            is_present = adapter.bot._face_detector.is_present
            logger.debug(
                "[process id: %s][room id: %s][FACE CHECK] "
                "In-memory face_detector.is_present = %s",
                process_id, room_id, is_present,
            )
            return is_present
        else:
            logger.debug(
                "[process id: %s][room id: %s][FACE CHECK] "
                "No face detector available — defaulting to visible",
                process_id, room_id,
            )
            return True  # No detector → assume visible
    except Exception as e:
        logger.warning(
            "[process id: %s][room id: %s][FACE CHECK] "
            "Error reading face detector: %s",
            process_id, room_id, e,
        )
        return True  # Default: candidate visible


def video_check(process_id, room_id, interview_id, video_status_obj):
    """
    Check video camera on/off status only. Updates shared VideoStatus object.
    Face visibility is NOT polled here — it's checked on-demand at reflex time.
    
    Returns:
        tuple: (is_video_off, None)
    """
    # Check video off via API
    is_video_off = check_video_status(room_id, process_id)
    
    # Update shared status object (face visibility not updated here)
    video_status_obj.update_status(is_video_off)
    
    return is_video_off, None


def _get_presence(room_id,process_id, **kwargs):
    """Return (is_candidate, recruiter_name, is_recruiter). Never fails."""
    try:
        participants = get_room_participants(room_id,process_id) or []
    except Exception as e:
        logger.exception("[process id: %s][room id: %s] presence check failed: %s",process_id,room_id, e)
        participants = []

    is_candidate = any((p.get("role") or "").lower() == "candidate" for p in participants)
    recruiter_entry = next((p for p in participants if (p.get("role") or "").lower() == "recruiter"), None)
    is_recruiter = recruiter_entry is not None
    recruiter_name = (recruiter_entry or {}).get("name") or "recruiter"
    return is_candidate, recruiter_name, is_recruiter


def _notify_recruiter_candidate_left(candidate_name, recruiter_name, driver, room_id, already_notified=False,process_id=0):
    """Notify recruiter once that candidate left and we’ll wait 5 minutes."""
    if already_notified:
        return True
    ai_speak(
        f"{recruiter_name}, I see {candidate_name} has disconnected. "
        "I’ll hold the room for 5 minutes in case they rejoin. "
        "Please feel free to nudge them.",
        driver,room_id,process_id
    )
    return True


def _wait_for_rejoin_or_timeout(room_id, total_wait_sec=300, poll_sec=5,process_id=0):
    """
    Wait up to total_wait_sec for candidate to be present again.
    Returns True if candidate rejoined, else False.
    """
    deadline = datetime.now() + timedelta(seconds=total_wait_sec)
    while datetime.now() < deadline:
        is_candidate, _, _ = _get_presence(room_id,process_id)
        if is_candidate:
            return True
        time.sleep(poll_sec)
    return False


def handle_candidate_absent_block(candidate_name, driver, room_id, process_id, current_question_no, conversation_timeline, reflex_context=None, interview_id=None):
    """
    Candidate has left the meeting:
      - Wait up to 5 minutes for them to return
      - Give periodic warnings every 30 seconds
      - If they return, resume interview
      - If 5 minutes elapse, terminate interview
    Returns (answer, repeat_qtn, candidate_exit, silence_time, bot_note_reason, speculative_reflex_result, speculative_hit, speech_end_time).
    """
    exit_messages = [
        f"It appears you've stepped away, {candidate_name}. I'll wait for you to return.",
        f"{candidate_name}, I notice you've left the meeting. Please rejoin when you're ready.",
        f"I see you've disconnected, {candidate_name}. I'll give you some time to reconnect.",
        f"{candidate_name}, looks like you've left. I'll be here when you return.",
    ]
    
    initial_msg = random.choice(exit_messages)
    ai_speak(initial_msg, driver, room_id, process_id)
    add_to_conversation_timeline(
        conversation_timeline, 
        'warning', 
        initial_msg, 
        "", 
        current_question_no, 
        room_id, 
        process_id
    )
    
    time_now = datetime.now()
    deadline = time_now + timedelta(minutes=5)
    last_prompt_ts = datetime.now()
    silence_time = 0
    
    while datetime.now() < deadline:
        is_candidate, _, _ = _get_presence(room_id, process_id)
        
        # Candidate returned!
        if is_candidate:
            return_messages = [
                f"Welcome back, {candidate_name}! Let's continue where we left off.",
                f"Great to have you back, {candidate_name}. Let's resume the interview.",
                f"{candidate_name}, you're back! Let's pick up from where we were.",
                f"Good to see you again, {candidate_name}. Let's continue our conversation.",
            ]
            
            return_msg = random.choice(return_messages)
            ai_speak(return_msg, driver, room_id, process_id)
            add_to_conversation_timeline(
                conversation_timeline, 
                'resume', 
                return_msg, 
                "", 
                current_question_no, 
                room_id, 
                process_id
            )
            
            # Try to capture their response
            retry_text = ""
            speculative_reflex_result = None
            speculative_hit = False
            speech_end_time = None
            
            if reflex_context and current_question_no >= 2:
                answer_dict, speculative_reflex_result, _, speculative_hit, speech_end_time, _ = \
                    _capture_with_speculative_reflex(
                        driver, room_id, process_id,
                        reflex_context['engine'], reflex_context['question_to_ask'], 
                        reflex_context['jd_summary'], reflex_context['mandatory_skills'], 
                        current_question_no, reflex_context['recent_acks'],
                        silence_ms_idle=30000,
                        interview_id=interview_id
                    )
                if answer_dict:
                    retry_text = answer_dict.get("answer", "")
            else:
                 retry_text_dict = _capture_muted_window(driver, room_id, process_id)
                 if retry_text_dict:
                     retry_text=retry_text_dict["answer"]
                     if retry_text and retry_text.strip():
                         speech_end_time = time.time()

            if retry_text and retry_text.strip():
                add_to_conversation_timeline(
                    conversation_timeline, 
                    'technical_qa', 
                    "", 
                    retry_text.strip(), 
                    current_question_no, 
                    room_id, 
                    process_id
                )
                return retry_text.strip(), False, False, silence_time, "EXIT", speculative_reflex_result, speculative_hit, speech_end_time
            else:
                # They returned but are muted - hand off to muted flow
                return handle_candidate_present_but_muted_block(
                    driver, room_id, candidate_name, process_id, 
                    current_question_no, conversation_timeline,
                    reflex_context=reflex_context
                )
        
        # Still absent - periodic reminders every 30 seconds
        if (datetime.now() - last_prompt_ts).total_seconds() >= 30:
            time_remaining = (deadline - datetime.now()).total_seconds()
            minutes_remaining = int(time_remaining // 60)
            
            if minutes_remaining > 0:
                waiting_messages = [
                    f"Still waiting for you to rejoin, {candidate_name}.",
                    f"{candidate_name}, I'm here whenever you're ready to continue.",
                    f"Waiting for your return, {candidate_name}.",
                ]
                waiting_msg = random.choice(waiting_messages)
                ai_speak(waiting_msg, driver, room_id, process_id)
                add_to_conversation_timeline(
                    conversation_timeline, 
                    'warning', 
                    waiting_msg, 
                    "", 
                    current_question_no, 
                    room_id, 
                    process_id
                )
            
            last_prompt_ts = datetime.now()
        
        # Track silence time
        if (datetime.now() - time_now).total_seconds() >= 60:
            silence_time = (datetime.now() - time_now).total_seconds()
        
        time.sleep(3)
    
    # 5 minutes elapsed - terminate interview
    termination_messages = [
        f"I'm sorry, {candidate_name}, but you haven't returned within the allowed time. "
        "We'll need to end this session. Please contact support if this was a technical issue.",
        
        f"{candidate_name}, I've waited for 5 minutes but you haven't rejoined. "
        "This interview session must be terminated. If you experienced technical difficulties, "
        "please reach out to our team.",
        
        f"Unfortunately, {candidate_name}, the maximum wait time has elapsed. "
        "This interview is now concluded. If there was a connection problem, "
        "please contact our support team to reschedule.",
    ]
    
    termination_msg = random.choice(termination_messages)
    ai_speak(termination_msg, driver, room_id, process_id)
    add_to_conversation_timeline(
        conversation_timeline, 
        'termination', 
        termination_msg, 
        "", 
        current_question_no, 
        room_id, 
        process_id
    )
    
    answer = f"BOT NOTE: candidate exited at {time_now.strftime('%H:%M:%S')} and did not return within 5 minutes. Interview terminated."
    add_to_conversation_timeline(
        conversation_timeline, 
        'termination', 
        "", 
        answer, 
        current_question_no, 
        room_id, 
        process_id
    )
    
    return answer, False, True, silence_time, "EXIT", None, False, None


def handle_candidate_present_but_muted_block(
    driver, room_id, candidate_name, process_id, current_question_no, conversation_timeline,
    analysis_params=None, reflex_context=None, interview_id=None
):
    """
    Candidate present but silent:
      - Inform candidate we'll listen 30s; re-prompt every ~30s.
      - Keep listening up to 5 minutes total.
      - If at any time candidate leaves → switch to 'absent' flow (and follow those rules).
      - If still no audio at 5 minutes → NOTE muted and proceed to next question (do NOT end meeting).
    Returns (answer, repeat_qtn, candidate_exit, silence_time, bot_note_reason, speculative_reflex_result, speculative_hit, speech_end_time).
    """
    # Initial gentle prompt
    initial_mute_messages = [
        "I didn't catch your audio. Please check your microphone and try again.",
        "I can't hear you. Please ensure your microphone is unmuted and working.",
        "Your audio isn't coming through. Please check your microphone settings.",
        "I'm not receiving any audio from you. Please verify your microphone is on.",
    ]
    
    initial_msg = random.choice(initial_mute_messages)
    ai_speak(initial_msg, driver, room_id, process_id)
    add_to_conversation_timeline(
        conversation_timeline, 
        'warning', 
        initial_msg, 
        "", 
        current_question_no, 
        room_id, 
        process_id
    )
    
    time_now=datetime.now()
    deadline = time_now + timedelta(minutes=5)
    last_prompt_ts = datetime.now()
    silence_time=0
    
    while datetime.now() < deadline:
        is_candidate, _, _ = _get_presence(room_id,process_id)

        # If candidate leaves during this window → treat as 'absent' case immediately
        if not is_candidate:
            leave_msg = f"{candidate_name}, I notice you've left the meeting. Switching to wait mode."
            add_to_conversation_timeline(
                conversation_timeline, 
                'warning', 
                leave_msg, 
                "", 
                current_question_no, 
                room_id, 
                process_id
            )
            # Handoff to the absent flow
            return handle_candidate_absent_block(
                candidate_name, driver, room_id, process_id, 
                current_question_no, conversation_timeline,
                reflex_context=reflex_context,
                interview_id=interview_id
            )

        # Try capture speech
        retry_text = ""
        retry_duration = 0
        speculative_reflex_result = None
        speculative_hit = False
        speech_end_time = None
        
        # Use speculative reflex if context available (fast lane for retries)
        if reflex_context and current_question_no >= 2:
            answer_dict, speculative_reflex_result, _, speculative_hit, speech_end_time, _ = \
                _capture_with_speculative_reflex(
                    driver, room_id, process_id,
                    reflex_context['engine'], reflex_context['question_to_ask'], 
                    reflex_context['jd_summary'], reflex_context['mandatory_skills'], 
                    current_question_no, reflex_context['recent_acks'],
                    silence_ms_idle=30000,  # Wait up to 30s in muted flow
                    interview_id=interview_id
                )
            if answer_dict:
                retry_text = answer_dict.get("answer", "")
                retry_duration = answer_dict.get("answer_duration", 0)
        else:
            # Fallback for Q1 or missing context
            retry_text_dict = _capture_muted_window(driver, room_id, process_id)
            if retry_text_dict:
                retry_text=retry_text_dict["answer"]
                retry_duration=retry_text_dict.get('duration', 0)
                if retry_text and retry_text.strip():
                     speech_end_time = time.time()

        if retry_text and retry_text.strip():
            success_messages = [
                "Great, I can hear you now!",
                "Perfect, your audio is working!",
                "Thank you, I've got your audio!",
            ]
            success_msg = random.choice(success_messages)
            add_to_conversation_timeline(
                conversation_timeline, 
                'resume', 
                success_msg, 
                "", 
                current_question_no, 
                room_id, 
                process_id
            )
            add_to_conversation_timeline(
                conversation_timeline, 
                'technical_qa', 
                "", 
                retry_text.strip(), 
                current_question_no, 
                room_id, 
                process_id
            )
            
            # ═══════════════════════════════════════════════════════════════════════
            # ✅ Trigger answer analysis after audio resume
            # ═══════════════════════════════════════════════════════════════════════
            if analysis_params and retry_text.strip() and not retry_text.strip().startswith("BOT NOTE:"):
                try:
                    from answer_analysis_integration import trigger_answer_analysis_async
                    
                    trigger_answer_analysis_async(
                        session_id=f"interview_{analysis_params.get('interview_id')}",
                        question_id=f"Q{current_question_no}",
                        question_text=analysis_params.get('question_text', ''),
                        question_type=analysis_params.get('question_type', 'technical'),
                        answer_text=retry_text.strip(),
                        answer_duration_sec=retry_duration,
                        resume_text=analysis_params.get('resume', ''),
                        session_context=analysis_params.get('session_context', {})
                    )
                    logger.info(
                        f"✓ [process id: {process_id}][room id: {room_id}] "
                        f"Analysis triggered for Q{current_question_no} (after audio resume)"
                    )
                except Exception as e:
                    logger.warning(
                        f"[process id: {process_id}][room id: {room_id}] "
                        f"Analysis trigger failed after audio resume (non-critical): {e}"
                    )
            
            return retry_text.strip(), False, False, silence_time, "MUTE", speculative_reflex_result, speculative_hit, speech_end_time
        
        # Track silence time
        if (datetime.now() - time_now).total_seconds() >= 60:
            silence_time = (datetime.now() - time_now).total_seconds()
        
        # Re-prompt every ~30 seconds while still present but silent
        if (datetime.now() - last_prompt_ts).total_seconds() >= 30:
            time_remaining = (deadline - datetime.now()).total_seconds()
            minutes_remaining = int(time_remaining // 60)
            
            if minutes_remaining > 0:
                periodic_mute_messages = [
                    "I still can't hear you. Please unmute your microphone; I'm still listening.",
                    "Still no audio. Please check that your microphone is unmuted.",
                    "I'm still waiting for your audio. Please verify your mic is working.",
                    "Your audio still isn't coming through. Please check your microphone.",
                ]
                periodic_msg = random.choice(periodic_mute_messages)
                ai_speak(periodic_msg, driver, room_id, process_id)
                add_to_conversation_timeline(
                    conversation_timeline, 
                    'warning', 
                    periodic_msg, 
                    "", 
                    current_question_no, 
                    room_id, 
                    process_id
                )
            
            last_prompt_ts = datetime.now()

        time.sleep(3)

    # 5 minutes elapsed still no audio → do NOT end interview; just skip this question with NOTE
    final_mute_msg = "I haven't been able to hear you for 5 minutes. We'll move to the next question."
    ai_speak(final_mute_msg, driver, room_id, process_id)
    add_to_conversation_timeline(
        conversation_timeline, 
        'warning', 
        final_mute_msg, 
        "", 
        current_question_no, 
        room_id, 
        process_id
    )
    
    answer = "BOT NOTE: no answer recorded as candidate is muted."
    add_to_conversation_timeline(
        conversation_timeline, 
        'warning', 
        "", 
        answer, 
        current_question_no, 
        room_id, 
        process_id
    )
    
    return answer, False, False, silence_time, "MUTE", None, False, None


def handle_no_answer_block(
    candidate_name, driver, room_id, process_id, current_question_no, conversation_timeline, 
    exit_count=0, analysis_params=None, reflex_context=None, interview_id=None
):
    """
    Entry point for your original:
        if not answer or not answer.strip():
            ... logic ...
    Implements your exact rules with precise outcomes.
    Returns (answer, repeat_qtn, candidate_exit, silence_time, bot_note_reason).
    
    Args:
        analysis_params: Optional dict with keys {interview_id, question_text, question_type, resume, session_context}
                         Passed to muted handler for triggering analysis when audio resumes.
    """
    is_candidate, _, _ = _get_presence(room_id, process_id)
    if not is_candidate:
        exit_count = exit_count + 1
        # Case 1: Candidate left (recruiter may or may not be present)
        return handle_candidate_absent_block(
            candidate_name, driver, room_id, process_id, 
            current_question_no, conversation_timeline,
            reflex_context=reflex_context,
            interview_id=interview_id
        )
    else:
        # Case 2: Candidate present but no audio (muted)
        return handle_candidate_present_but_muted_block(
            driver, room_id, candidate_name, process_id, 
            current_question_no, conversation_timeline,
            analysis_params=analysis_params,
            reflex_context=reflex_context,
            interview_id=interview_id
        )


def _capture_normal(driver, room_id, process_id, skill_states=None):
    # 5s idle before triggering mute handler (was 10s — felt broken/frozen to candidates)
    # 3s after any speech activity ends
    capture_fn = record_transcribe_webrtc_streaming if DG_STREAMING else record_transcribe_webrtc_audio
    return capture_fn(
        room_id, process_id,
        driver,
        silence_ms_idle=5000,
        silence_ms_active=3000,
    )


def _capture_muted_window(driver, room_id, process_id, skill_states=None):
    # Up to 30s if no audio at all; once audio appears, tail is 3s
    capture_fn = record_transcribe_webrtc_streaming if DG_STREAMING else record_transcribe_webrtc_audio
    return capture_fn(
        room_id, process_id,
        driver,
        silence_ms_idle=30000,
        silence_ms_active=3000,
    )


def _capture_with_speculative_reflex(
    driver, room_id, process_id,
    reflex_engine, question_to_ask, job_description,
    mandatory_skills, current_question_no, recent_acks,
    silence_ms_idle=10000,
    followup_chain_count=0,
    max_followups_for_question=2,
    interview_id=None,
    quota_forcing=False
):
    """
    Speculative reflex via JS early-return architecture.
    
    Flow:
    1. Call streaming with speculative=True → JS returns early at 1s silence
    2. Immediately fire reflex in background thread (using early transcript)
    3. Call _streaming_continuation() → waits for remaining 2s silence + Deepgram close
    4. Compare early vs final transcript:
       - Match → use pre-computed reflex (0ms perceived latency!)
       - Mismatch → re-run reflex with final transcript
    
    Returns: (answer_dict, reflex_result, spoken_filler, speculative_hit, speech_end_time, followup_reflex_result)
    """
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import logging
    logger = logging.getLogger(__name__)
    log_prefix = f"[process id: {process_id}][room id: {room_id}]"

    # Turn boundary: the ack + question for this turn are done and we're about to
    # listen, so release the held Cartesia speak-lease — freeing the slot for other
    # interviews. (The lease is held across ack→question so no filler intrudes.)
    try:
        _adp = get_livekit_adapter()
        if _adp:
            _adp.release_speak_lease()
    except Exception:
        pass

    # Turn boundary: the ack + question for this turn are done and we're about to
    # listen, so release the held Cartesia speak-lease — freeing the slot for other
    # interviews. (The lease is held across ack→question so no filler intrudes.)
    try:
        _adp = get_livekit_adapter()
        if _adp:
            _adp.release_speak_lease()
    except Exception:
        pass
    
    # ── Step 1: Record with speculative=True (returns early at 1s silence) ──
    # silence_ms_active=2000 is safe because re-silence loop handles mid-thought pauses
    spec_start = time.time()
    early_result = record_transcribe_webrtc_streaming(
        room_id, process_id, driver,
        speculative=True,
        silence_ms_active=3000,
        silence_ms_idle=10000
    )
    
    # Check if we got an early return
    if not early_result or not early_result.get("earlyReturn"):
        # No early return — candidate was silent or speculative didn't trigger
        # The result is already the final result
        logger.info(f"📋 {log_prefix} NO EARLY RETURN — using direct result")
        return early_result, None, None, False, None, None
    
    early_transcript = early_result.get("answer", "").strip()
    logger.info(
        f"📡 {log_prefix} EARLY TRANSCRIPT: '{early_transcript[:80]}...' | "
        f"elapsed={time.time()-spec_start:.2f}s"
    )
    
    # ── Step 2: Fire speculative reflex in background ──
    speculative_executor = ThreadPoolExecutor(max_workers=3)  # main + follow-up + face-visibility
    
    # Fire face visibility check in parallel
    face_visible_future = speculative_executor.submit(
        check_face_visibility, process_id, room_id, interview_id
    ) if interview_id else None
    
    def _speculative_reflex():
        """Speculative reflex — fires intent + streaming ack + TTS in one shot.
        
        For Q2+ (main questions):
          1. Fire intent_only + ack_stream in PARALLEL
          2. As ack tokens stream, detect first natural break (~30-50 chars)
          3. Fire TTS for that first chunk immediately
          4. Return result with intent, full ack, and prefetched first-chunk audio
        
        For Q0/Q1 (greeting/warmup): uses specialized non-streaming reflex.
        """
        if current_question_no == 1:
            result = reflex_engine.generate_q1_reflex(
                transcript=early_transcript,
                current_question=question_to_ask,
                jd_summary=job_description,
                round_number=1,
                room_id=room_id,
                process_id=process_id
            )
            # For Q1, fire TTS for first chunk after getting the result
            ack_text = result.get("ack", "")
            if ack_text:
                _fire_tts_for_first_chunk(result)
            return result
        elif current_question_no == 0:
            result = reflex_engine.generate_greeting_reflex(
                transcript=early_transcript,
                room_id=room_id,
                process_id=process_id
            )
            ack_text = result.get("ack", "")
            if ack_text:
                _fire_tts_for_first_chunk(result)
            return result
        else:
            # ══════════════════════════════════════════════════════
            # Task 2: SPECULATIVE nano intent+quality classifier
            # ══════════════════════════════════════════════════════
            # Fired speculatively (during the candidate's answer). When the ack bank is
            # rendered, this fully REPLACES live ack generation + Cartesia prefetch for
            # answered/skip/repeat/rephrase turns — the loop plays a pre-rendered ack from
            # memory (zero GPT, zero Cartesia). EXIT and an unrendered bank fall through to
            # the live streaming ack pipeline below.
            try:
                from ack_audio import get_ack_bank
                _cached_ready = USE_CACHED_ACKS and get_ack_bank().available
            except Exception:
                _cached_ready = False
            if _cached_ready:
                iq = reflex_engine.generate_intent_quality(
                    transcript=early_transcript,
                    current_question=question_to_ask,
                    question_number=current_question_no,
                    room_id=room_id,
                    process_id=process_id,
                )
                _intent = iq.get("intent", "others")
                if _intent != "exit":
                    logger.info(
                        f"🔮⚡ {log_prefix} SPECULATIVE INTENT+QUALITY: intent={_intent} "
                        f"quality={iq.get('quality')} conf={iq.get('confidence')} → cached ack"
                    )
                    return {"intent": _intent, "quality": iq.get("quality"),
                            "confidence": iq.get("confidence"), "ack": "", "_cached_ack": True}
                # exit → fall through to the live path (exit has a dedicated downstream flow)

            # ══════════════════════════════════════════════════════
            # TRUE STREAMING: Intent + Ack Stream + TTS in parallel (live fallback)
            # ══════════════════════════════════════════════════════
            import re
            from concurrent.futures import ThreadPoolExecutor as _StreamPool

            stream_start = time.time()
            
            # Fire intent classification in a separate thread (fast, ~300ms)
            with _StreamPool(max_workers=2) as stream_pool:
                intent_future = stream_pool.submit(
                    reflex_engine.generate_intent_only,
                    transcript=early_transcript,
                    current_question=question_to_ask,
                    question_number=current_question_no,
                    room_id=room_id,
                    process_id=process_id
                )
                
                # Stream ack tokens and build first chunk for TTS
                ack_buffer = ""
                first_chunk = None
                first_chunk_tts_future = None
                remaining_text = ""
                first_token_received = False
                
                try:
                    skills_list = mandatory_skills.split(", ") if isinstance(mandatory_skills, str) else mandatory_skills
                    
                    # We need intent for the ack prompt, but we can start with "others" 
                    # (most common) and the ack will still be appropriate
                    # The real intent from intent_future will be used for routing
                    for token in reflex_engine.generate_ack_stream(
                        transcript=early_transcript,
                        current_question=question_to_ask,
                        intent="others",  # Speculative — most common intent
                        jd_summary=job_description,
                        mandatory_skills=skills_list,
                        question_number=current_question_no,
                        recent_acks=recent_acks,
                        room_id=room_id,
                        process_id=process_id
                    ):
                        if not first_token_received:
                            first_token_received = True
                            ttft = (time.time() - stream_start) * 1000
                            logger.info(
                                f"🔮💨 {log_prefix} STREAM TTFT: {ttft:.0f}ms (first GPT token)"
                            )
                        
                        ack_buffer += token
                        
                        # Check if we have enough for a first chunk TTS
                        if first_chunk is None and len(ack_buffer) >= 15:
                            # Look for natural break in current buffer
                            chunk, rest = _extract_first_chunk_from_stream(ack_buffer)
                            if chunk and len(chunk) >= 15:
                                first_chunk = chunk
                                # Fire TTS immediately for this chunk!
                                tts_fire_time = time.time()
                                first_chunk_tts_future = stream_pool.submit(
                                    _generate_tts_chunk, first_chunk
                                )
                                logger.info(
                                    f"🔮🔊 {log_prefix} STREAM TTS FIRED: '{first_chunk}' "
                                    f"({len(first_chunk)} chars) at {(tts_fire_time-stream_start)*1000:.0f}ms"
                                )
                
                except Exception as e:
                    logger.warning(f"⚠️ {log_prefix} Ack stream failed: {e}")
                    if not ack_buffer:
                        ack_buffer = "Hmm, okay — I appreciate you sharing that."
                
                # Finalize: if no first chunk was extracted during streaming
                # (ack was too short for a break), use the whole buffer
                if first_chunk is None and ack_buffer:
                    first_chunk = ack_buffer.strip()
                    first_chunk_tts_future = stream_pool.submit(
                        _generate_tts_chunk, first_chunk
                    )
                    remaining_text = ""
                    logger.info(
                        f"🔮🔊 {log_prefix} STREAM TTS FIRED (full ack, no split): "
                        f"'{first_chunk[:60]}' ({len(first_chunk)} chars)"
                    )
                elif first_chunk:
                    # remaining = everything after the first chunk
                    remaining_text = ack_buffer[len(first_chunk):].strip()
                
                # Get intent result (should be done by now — it's much faster)
                try:
                    intent_result = intent_future.result(timeout=3.0)
                    intent = intent_result.get("intent", "others")
                except Exception:
                    intent = "others"
                
                # Wait for first chunk TTS (it's been running in parallel!)
                prefetched_audio = None
                if first_chunk_tts_future:
                    try:
                        tts_wait_start = time.time()
                        prefetched_audio = first_chunk_tts_future.result(timeout=2.0)
                        tts_wait = (time.time() - tts_wait_start) * 1000
                        total_ms = (time.time() - stream_start) * 1000
                        logger.info(
                            f"⚡ {log_prefix} STREAM TTS READY: {tts_wait:.0f}ms wait | "
                            f"{len(prefetched_audio) if prefetched_audio else 0} bytes | "
                            f"total pipeline: {total_ms:.0f}ms"
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ {log_prefix} Stream TTS failed: {e}")
            
            # Build result dict
            full_ack = ack_buffer.strip()
            
            # Strip trailing question marks from ack
            if full_ack.endswith("?"):
                full_ack = full_ack[:-1] + "."
            
            result = {"intent": intent, "ack": full_ack}
            
            # Attach prefetched audio and remaining text
            if prefetched_audio:
                result["_prefetched_tts"] = prefetched_audio
                result["_prefetch_remaining_text"] = remaining_text
            
            total_ms = (time.time() - stream_start) * 1000
            logger.info(
                f"🔮✅ {log_prefix} STREAMING REFLEX COMPLETE: {total_ms:.0f}ms | "
                f"intent={intent} | ack='{full_ack[:60]}...' | "
                f"chunk='{first_chunk[:40] if first_chunk else 'none'}' | "
                f"tts={'READY' if prefetched_audio else 'NONE'} | "
                f"remaining={len(remaining_text)} chars"
            )
            
            return result
    
    def _extract_first_chunk_from_stream(buffer, max_chars=50):
        """Extract first chunk from a still-growing buffer.
        Only returns a chunk if we've hit a natural break point."""
        import re
        buffer = buffer.strip()
        if len(buffer) <= 15:
            return None, buffer
        
        search_end = min(len(buffer), max_chars)
        search_region = buffer[:search_end]
        
        for pat in [r'[!?.]', r'—', r',']:
            matches = list(re.finditer(pat, search_region))
            if matches:
                last_match = matches[-1]
                split_pos = last_match.end()
                if split_pos >= 15:
                    return buffer[:split_pos].strip(), buffer[split_pos:].strip()
        
        # If buffer is longer than max_chars, split at last word boundary
        if len(buffer) >= max_chars:
            last_space = search_region.rfind(' ')
            if last_space > 15:
                return buffer[:last_space].strip(), buffer[last_space:].strip()
        
        return None, buffer  # Not enough text yet, keep buffering
    
    def _generate_tts_chunk(text):
        """Generate TTS audio for a text chunk. Returns None if Cartesia is active
        (Cartesia handles TTS at speak-time with ~300ms TTFA — no prefetch needed)."""
        if USE_CARTESIA:
            return None
        try:
            response = openai_client.audio.speech.create(
                model="tts-1",
                voice="nova",
                input=text,
                response_format="mp3",
                speed=1,
            )
            return response.content
        except Exception as e:
            logger.warning(f"⚠️ {log_prefix} TTS chunk generation failed: {e}")
            return None
    
    def _fire_tts_for_first_chunk(result_dict):
        """For Q0/Q1: extract first chunk and attach split info (NO blocking TTS).
        
        The actual TTS call happens at speak time — we just prepare the chunk
        split so the speak code knows what to play first.
        """
        import re
        ack_text = result_dict.get("ack", "")
        if not ack_text:
            return
        
        chunk, remaining = _extract_first_chunk_from_stream(ack_text, max_chars=50)
        if not chunk:
            chunk = ack_text
            remaining = ""
        
        result_dict["_prefetch_remaining_text"] = remaining
        logger.info(
            f"🔮 {log_prefix} Q{current_question_no} CHUNK SPLIT: "
            f"'{chunk[:50]}' (remaining: {len(remaining)} chars) — TTS at speak time"
        )
    
    # Fire follow-up reflex in parallel (only for Q3+ and within chain limit).
    # Suppress it when the configured quotas can no longer spare a slot — an
    # experience follow-up would consume a slot the config wants for a quota
    # question (conceptual / scenario / resume).
    followup_reflex_future = None
    fire_followup = (
        current_question_no >= 3 and
        followup_chain_count < max_followups_for_question and
        not quota_forcing
    )
    
    def _speculative_followup_reflex():
        try:
            skills_list = mandatory_skills.split(", ") if isinstance(mandatory_skills, str) else mandatory_skills
            return reflex_engine.generate_followup_reflex(
                transcript=early_transcript,
                current_question=question_to_ask,
                mandatory_skills=skills_list,
                question_number=current_question_no,
                abstraction_level=2,  # Will be refined in main loop
                recent_acks=recent_acks,
                room_id=room_id,
                process_id=process_id
            )
        except Exception as e:
            logger.warning(f"⚠️ {log_prefix} Speculative follow-up reflex failed: {e}")
            return {"followup_worthy": False, "ack": "", "followup_topic": ""}
    
    speculative_future = speculative_executor.submit(_speculative_reflex)
    if fire_followup:
        followup_reflex_future = speculative_executor.submit(_speculative_followup_reflex)
        logger.info(f"🔮🔄 {log_prefix} DUAL SPECULATIVE FIRED: main + followup (while recording continues)")
    else:
        logger.info(f"🔮 {log_prefix} SPECULATIVE REFLEX FIRED (while recording continues)")
    
    # NOTE: TTS prefetch is now handled INSIDE _speculative_reflex() via streaming.
    # The result dict will contain _prefetched_tts and _prefetch_remaining_text
    # when the future completes — no separate callback needed.
    
    # ── Step 3+4: Loop — continuation may return early on re-silence ──
    MAX_SPECULATIVE_ATTEMPTS = 5  # Safety cap
    attempt = 1
    
    while attempt <= MAX_SPECULATIVE_ATTEMPTS:
        cont_result = _streaming_continuation(driver, room_id, process_id)
        final_transcript = (cont_result.get("answer", "") if cont_result else "").strip()
        candidate_spoke_again = cont_result.get("candidateSpokAgain", False) if cont_result else False
        
        # If continuation returned early (re-silence), candidate spoke more then paused again
        if cont_result and cont_result.get("earlyReturn"):
            new_transcript = final_transcript
            
            # Only fire a new speculative if transcript actually changed
            if new_transcript == early_transcript:
                logger.info(
                    f"🔄 {log_prefix} RE-SILENCE DETECTED but transcript unchanged — "
                    f"keeping existing speculative (attempt {attempt})"
                )
                continue  # Re-enter continuation without wasting a GPT call
            
            # Transcript changed — cancel old speculative and fire new
            speculative_future.cancel()
            early_transcript = new_transcript
            attempt += 1
            logger.info(
                f"🔄 {log_prefix} RE-SILENCE DETECTED (attempt {attempt}) — "
                f"transcript changed, firing new speculative reflex"
            )
            
            # For retries, use generate_reflex (batch) for simplicity.
            # Retries are rare and the batch approach is more reliable here
            # since we need to cancel/restart quickly.
            def _speculative_reflex_retry(t=early_transcript):
                if current_question_no == 1:
                    result = reflex_engine.generate_q1_reflex(
                        transcript=t,
                        current_question=question_to_ask,
                        jd_summary=job_description,
                        round_number=1,
                        room_id=room_id,
                        process_id=process_id
                    )
                elif current_question_no == 0:
                    result = reflex_engine.generate_greeting_reflex(
                        transcript=t,
                        room_id=room_id,
                        process_id=process_id
                    )
                else:
                    result = reflex_engine.generate_reflex(
                        transcript=t,
                        current_question=question_to_ask,
                        jd_summary=job_description,
                        mandatory_skills=mandatory_skills,
                        question_number=current_question_no,
                        recent_acks=recent_acks,
                        spoken_filler=None,
                        room_id=room_id,
                        process_id=process_id
                    )
                # Fire TTS for first chunk of the result
                _fire_tts_for_first_chunk(result)
                return result
            
            speculative_future = speculative_executor.submit(_speculative_reflex_retry)
            logger.info(f"🔮 {log_prefix} SPECULATIVE REFLEX #{attempt} FIRED")
            continue  # Loop back to wait for continuation again
        
        # Continuation finished (recording is done) — compare transcripts
        break
    
    # Build the final answer_dict
    answer_dict = {
        "answer": final_transcript if final_transcript else early_transcript,
        "answer_duration": early_result.get("answer_duration", 0) + cont_result.get("answer_duration", 0)
    }
    
    # ── Compare transcripts and decide ──
    reflex_result = None
    speculative_hit = False
    
    # Normalize for comparison (ignore case, strip whitespace/punctuation difference)
    def normalize(text):
        return re.sub(r'[^\w\s]', '', text.lower()).strip()
    
    norm_early = normalize(early_transcript)
    norm_final = normalize(final_transcript)
    
    # HIT CONDITIONS:
    # 1. Exact match (ideal)
    # 2. Early is a prefix of Final (e.g. "I have experience" vs "I have experience with Java")
    #    AND candidate didn't speak a NEW sentence/thought (heuristic: length diff < 50 chars)
    # 3. Final is a substring of Early (rare, but happens if Deepgram corrects/truncates)
    
    is_prefix_match = norm_final.startswith(norm_early)
    is_close_enough = is_prefix_match and (len(norm_final) - len(norm_early) < 50)
    
    if final_transcript and norm_final != norm_early and not is_close_enough:
        # Transcript changed significantly — MISS
        speculative_future.cancel()
        logger.info(
            f"⚠️ {log_prefix} SPECULATIVE MISS — significant change | "
            f"early='{early_transcript[:50]}...' | final='{final_transcript[:50]}...'"
        )
    elif not candidate_spoke_again or norm_final == norm_early or is_close_enough:
        # Transcript matches or is close enough — HIT
        try:
            reflex_result = speculative_future.result(timeout=10)
            speculative_hit = True
            hit_type = "EXACT" if norm_final == norm_early else "PREFIX"
            logger.info(f"✅ {log_prefix} SPECULATIVE HIT ({hit_type}) — using pre-computed reflex")
            
            # TTS audio is already embedded in the result dict from streaming
            # (attached inside _speculative_reflex during streaming pipeline)
            if reflex_result.get("_prefetched_tts"):
                logger.info(
                    f"⚡ {log_prefix} STREAMING TTS PRE-ATTACHED: "
                    f"{len(reflex_result['_prefetched_tts'])} bytes | "
                    f"remaining: {len(reflex_result.get('_prefetch_remaining_text', ''))} chars"
                )
        except Exception as e:
            logger.warning(f"⚠️ {log_prefix} Speculative future failed: {e}")
    else:
        # Fallback for other cases (e.g. candidate spoke again but transcript didn't match prefix)
        speculative_future.cancel()
        logger.info(f"⚠️ {log_prefix} SPECULATIVE MISS — candidate spoke again, no prefix match")
    
    # ── Fallback: run reflex synchronously if no speculative result ──
    spoken_filler = None
    if reflex_result is None:
        final_answer = answer_dict.get("answer", "").strip()
        if final_answer and not final_answer.startswith("BOT NOTE:"):
            if current_question_no == 1:
                reflex_result = reflex_engine.generate_q1_reflex(
                    transcript=final_answer,
                    current_question=question_to_ask,
                    jd_summary=job_description,
                    round_number=1,
                    room_id=room_id,
                    process_id=process_id
                )
            elif current_question_no == 0:
                reflex_result = reflex_engine.generate_greeting_reflex(
                    transcript=final_answer,
                    room_id=room_id,
                    process_id=process_id
                )
                reflex_result = reflex_engine.generate_reflex(
                    transcript=final_answer,
                    current_question=question_to_ask,
                    jd_summary=job_description,
                    mandatory_skills=mandatory_skills,
                    question_number=current_question_no,
                    recent_acks=recent_acks,
                    spoken_filler=None,
                    room_id=room_id,
                    process_id=process_id
                )
    
    # ── Override if face was not visible ──
    is_face_visible = True
    if face_visible_future:
        try:
            is_face_visible = face_visible_future.result(timeout=5)
        except Exception as e:
            logger.warning(f"⚠️ {log_prefix} Face visibility future failed: {e}")
            
    if not is_face_visible:
        logger.info(f"🚫 {log_prefix} Face not visible. Overriding ACK with warning.")
        reflex_result = {
            "intent": "repeat",
            "ack": "I cannot see you clearly. Please turn on your camera so we can continue the interview.",
            "followup_worthy": False,
            "followup_topic": ""
        }
        # Clear any follow-up since we are just giving a camera warning
        if followup_reflex_future:
            followup_reflex_future.cancel()

    
    # Resolve follow-up reflex result
    followup_reflex_result = None
    if followup_reflex_future:
        try:
            followup_reflex_result = followup_reflex_future.result(timeout=5)
            logger.info(
                f"🔄 {log_prefix} SPECULATIVE FOLLOWUP RESOLVED: "
                f"worthy={followup_reflex_result.get('followup_worthy', False)}"
            )
        except Exception as fu_err:
            logger.warning(f"⚠️ {log_prefix} Speculative follow-up future failed: {fu_err}")
            followup_reflex_result = {"followup_worthy": False, "ack": "", "followup_topic": ""}
    
    speculative_executor.shutdown(wait=False)
    
    # Calculate speech end time
    speech_end_time = time.time()
    
    logger.info(
        f"⏱️ {log_prefix} AUDIO RETURN → ACK READY: {time.time() - spec_start:.2f}s "
        f"(Time from browser audio end until we have an intent/ack)"
    )
    
    return answer_dict, reflex_result, spoken_filler, speculative_hit, speech_end_time, followup_reflex_result


def stop_room(room_id: str,interview_id:str, reason: str = "interview_ended"):
    """
    Stop a room. Idempotent - safe to call multiple times.
    Returns {"ok": True/False, ...}
    """
    url = f"{SENTINEL_BASE_URL}/rooms/stop/{room_id}_{interview_id}"

    try:
        response = requests.post(url, json={"reason": reason})
        response.raise_for_status()  # Raise an exception for HTTP errors
        logger.info(f"✅ Room {room_id} stopped successfully")
        return response.json()  # Return the JSON response from the API
    except requests.exceptions.HTTPError as e:
        # 400 BAD REQUEST might mean room already stopped - treat as success
        if e.response.status_code == 400:
            logger.debug(f"🔇 Room {room_id} already stopped or invalid (400 response)")
            return {"ok": True, "already_stopped": True}
        logger.error(f"❌ HTTP error stopping room {room_id}: {e}")
        return {"ok": False, "error": str(e)}
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Network error stopping room {room_id}: {e}")
        return {"ok": False, "error": str(e)}


def end_meeting_for_all(room_id: str, timeout: int = 5,process_id=0) -> bool:
    """
    Calls your backend: POST {base_url}/api/meeting/{room_id}/end
    Returns True on 2xx, False otherwise.
    """
    try:
        url = f"{base_url.rstrip('/')}/api/meeting/{room_id}/end"
        r = requests.post(url, timeout=timeout)
        if r.ok:
            logger.info("✅[process id: %s][room id: %s] Ended meeting for all: room=%s, resp=%s",process_id,room_id, room_id, r.text[:200])
            return True
        logger.error("❌[process id: %s][room id: %s] End meeting failed (%s): %s",process_id,room_id, r.status_code, r.text[:200])
        return False
    except Exception as e:
        logger.error("❌[process id: %s][room id: %s] End meeting request error for room %s: %s",process_id,room_id, room_id, e)
        return False


def save_image_to_png(id, output_dir='images'):
    """
    Retrieve best_face bytea from interviews via API and save as PNG
    
    Args:
        id: Interview ID to retrieve
        output_dir: Directory to save the image (default: 'images')
    
    Returns:
        str: Path to saved image file or None if failed
    """
    try:
        result = airadb._api_call("GET", f"/interview/best-face/{id}")
        face_b64 = result.get("bestFace")
        if not face_b64:
            logger.warning(f"⚠️ No image data from API for interview id: {id}")
            return None
        bytea_data = _normalize_bytea(base64.b64decode(face_b64))
        if not bytea_data:
            logger.warning(f"⚠️ Could not normalize image data for interview id: {id}")
            return None
        os.makedirs(output_dir, exist_ok=True)
        filepath = os.path.join(output_dir, f"interview_{id}_best_face.png")
        image = Image.open(io.BytesIO(bytea_data))
        if image.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', image.size, (255, 255, 255))
            if image.mode == 'P':
                image = image.convert('RGBA')
            background.paste(image, mask=image.split()[-1] if image.mode in ('RGBA', 'LA') else None)
            image = background
        elif image.mode != 'RGB':
            image = image.convert('RGB')
        image.save(filepath, 'PNG')
        logger.info(f"✅ Image saved successfully via API: {filepath}")
        return filepath
    except Exception as e:
        logger.error(f"❌ Error saving image via API: {e}")
        return None


def inspect_bytea(id):
    """
    Inspect the bytea data to understand its format via API
    """
    try:
        result = airadb._api_call("GET", f"/interview/best-face/{id}")
        face_b64 = result.get("bestFace")
        if not face_b64:
            logger.error("No data found via API")
            return None
        bytea_data = _normalize_bytea(base64.b64decode(face_b64))
        if not bytea_data:
            logger.error("Could not normalize bytea data")
            return None
        logger.info(f"📊 Bytea size: {len(bytea_data)} bytes (via API)")
        
        # Check for common image signatures
        signatures = {
            b'\xff\xd8\xff': 'JPEG',
            b'\x89PNG\r\n\x1a\n': 'PNG',
            b'GIF87a': 'GIF87a',
            b'GIF89a': 'GIF89a',
            b'BM': 'BMP',
            b'RIFF': 'WEBP (possibly)',
            b'data:image': 'Base64 encoded image'
        }
        
        for sig, format_name in signatures.items():
            if bytea_data.startswith(sig):
                logger.info(f"✅ Detected format: {format_name}")
                return format_name
        
        logger.warning("⚠️ Unknown format - not a standard image format")
        return None
    except Exception as e:
        logger.error(f"❌ Error inspecting bytea via API: {e}")
        return None


def _normalize_bytea(raw):
    """Normalize PostgreSQL bytea to raw bytes (handles memoryview, hex-encoded, plain bytes)."""
    if raw is None:
        return None
    if isinstance(raw, memoryview):
        return bytes(raw)
    if isinstance(raw, str):
        if raw.startswith('\\x'):
            return bytes.fromhex(raw[2:])
        return raw.encode('latin-1')
    return bytes(raw)


def add_to_conversation_timeline(timeline, conversation_type, aira_text, candidate_text="", qtn_idx=None, room_id="", process_id=0, **kwargs):
    """
    Add a conversation exchange to the comprehensive timeline.
    
    Args:
        timeline: The conversation_timeline list to append to
        conversation_type: Type of conversation - 'setup', 'technical_qa', 'warning', 'repeat', 'clarification', 'exit'
        aira_text: What AIRA said
        candidate_text: What candidate said (if any)
        qtn_idx: Question index (for technical_qa)
        room_id: Room ID for logging
        process_id: Process ID for logging
    """
    timeline_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "conversation_type": conversation_type,
        "aira_said": aira_text,
        "candidate_said": candidate_text,
        "question_index": qtn_idx
    }
    timeline.append(timeline_entry)
    # logger.info("-"*1000)
    # logger.info(f"[process id: {process_id}][room id: {room_id}] conversational timeline for debugging: {timeline_entry}")
    # logger.info(f"[process id: {process_id}][room id: {room_id}] 📝 Added to timeline: {conversation_type} - AIRA: {aira_text[:100]}...")
    # logger.info("-"*1000)
    return timeline
