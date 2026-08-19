# --- Imports and environment setup ---
import base64
import json
import random
from PIL import Image
import time
import os
import numpy as np
import sounddevice as sd
# TTS is now handled by OpenAI (previously ElevenLabs)
from zoneinfo import ZoneInfo
from pydub import AudioSegment
import io
from dotenv import load_dotenv
from datetime import datetime, timedelta
from openai import OpenAI
import logging
import threading
import integrations.aira_sql as airadb
import re
import requests
import pymysql
import ssl
from zoneinfo import ZoneInfo
from answer_analysis_integration import (
    trigger_answer_analysis_async,
    map_question_format_to_type
)
from answer_analysis.session_store import append_mute_event
from core.responses_client import ResponsesClient, CRITICAL_REMINDERS
from reflex_engine import ReflexEngine
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from core.evaluation_engine import evaluate_answer
from integrations.aira_sql import (
    Session as AiraSession,
    insert_question_row,
    update_question_evaluation,
    mark_evaluation_failed,
    upsert_project,
    load_skill_baselines,
    update_skill_baseline,
    cleanup_candidate_live_data,
    InterviewQuestion,
)
# from live_state import update_current_question
# Deepgram STT runs inside the LiveKit bot (aira_bot_client) — this module no longer
# imports the deepgram SDK directly. (The old browser-STT symbols were dead after the
# browser removal, and they don't exist in deepgram-sdk 6.x anyway.)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from config_loader import INTERVIEW_PASS_THRESHOLD_PERCENTAGE, HARDCODED_EMAILS, NUMBER_OF_QUESTIONS, MAX_INTERVIEW_DURATION_MINUTES
from core.interview_config import InterviewConfig, InterviewContext, RuntimeSkillState, ConceptState
import certifi

# Apply SSL fixes for macOS

try:
    # Fix SSL certificate issues
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
    # Create default HTTPS context with proper certificates
    ssl._create_default_https_context = ssl._create_unverified_context
    logging.getLogger(__name__).info("✅ SSL certificate fix applied")
except Exception as ssl_fix_error:
    logging.getLogger(__name__).warning("SSL fix failed: %s", ssl_fix_error)

load_dotenv(override=True)
logger = logging.getLogger(__name__)

# ── handlers split (re-exported for compatibility) ────────────────────────
from webrtc.handlers import (_capture_face_after_ack, _capture_face_during_answer,
                             read_best_face_bytes, get_room_participants, check_video_status,
                             check_face_visibility, video_check, handle_candidate_absent_block,
                             handle_candidate_present_but_muted_block, handle_no_answer_block,
                             _capture_with_speculative_reflex, stop_room, end_meeting_for_all,
                             save_image_to_png, add_to_conversation_timeline)

# ── audio split (re-exported for compatibility) ───────────────────────────
from webrtc.audio import (_TTS_SEMAPHORE, DG_API_KEY, DG_MODEL, livekit_speak_cartesia,
                          deepgram_transcribe_bytes, record_transcribe_webrtc_audio,
                          record_transcribe_webrtc_streaming, transcribe_webrtc_audio,
                          play_audio_in_webrtc, play_ack_with_bargein, play_streaming_audio_in_webrtc,
                          ai_speak_streaming, ai_speak_streamed_text, ai_speak, ai_speak_with_audio,
                          prefetch_tts, chain_prefetch_to_planner, generate_ai_response,
                          _streaming_continuation, _play_segment_system_audio,
                          play_cached_ack, USE_CACHED_ACKS, USE_CACHED_QUESTIONS)
from ack_audio import bucket_for  # Task 2: intent+quality → cached ack bucket
from webrtc.livekit_adapter import get_livekit_adapter  # Task 2: play cached question PCM

# ── analysis split (re-exported for compatibility) ─────────────────────────
from webrtc.analysis import (QUESTION_ANALYSIS_PROMPT, AGGREGATE_PROMPT, _TRAIL_OBJ, _TRAIL_ARR,
                             _VALUE_STR_RE, extract_json_from_response, analyze_single_question,
                             validate_and_fix_scoring, extract_json_from_response_final,
                             _validate_and_fix_schema, create_comprehensive_final_report,
                             calculate_score, generate_final_analysis_with_gpt,
                             detect_rejected_skills, summarize_audio_video_issues)

# ═══════════════════════════════════════════════════════════
# LiveKit feature flag (Phase 3 of browser-kill migration)
# When True, all audio I/O goes through LiveKit bot client
# instead of Selenium/Chrome. Set AIRA_USE_LIVEKIT=true to activate.
# ═══════════════════════════════════════════════════════════
from webrtc.livekit_adapter import get_livekit_adapter, set_livekit_adapter, USE_CARTESIA
















# Optional email client import with fallback
try:
    import integrations.MSEmailClient as email_client
    EMAIL_ENABLED = True
    logger.info("✅ Email functionality enabled")
except ImportError as e:
    logger.warning("⚠️ Email functionality disabled due to missing dependencies: %s", e)
    EMAIL_ENABLED = False
    email_client = None

# --- Constants and API keys ---
SILENCE_THRESHOLD = 650
SILENCE_DURATION = 4
CHUNK = 1024
RATE = 16000
CHANNELS = 1
openAIclient = OpenAI(timeout=30.0, max_retries=0)

CLIENT_ID = os.getenv("JOBDIVA_CLIENT_ID")
USERNAME = os.getenv("JOBDIVA_USERNAME")
PASSWORD = os.getenv("JOBDIVA_PASSWORD")
AUTH_URL = os.getenv("JOBDIVA_AUTH_URL")
API_BASE = os.getenv("JOBDIVA_API_BASE")
from env_config import DEPLOYMENT, SENTINEL_BASE_URL, CONNECT_BASE_URL
base_url = CONNECT_BASE_URL

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.error("Missing OpenAI API key in environment variables.")
    raise ValueError("Missing OpenAI API key in environment variables.")

openai_client = OpenAI(api_key=OPENAI_API_KEY, timeout=30.0, max_retries=0)

# ─────────────────────────────────────────────────────────────────────────────
# Global TTS concurrency limiter
# Without this, multiple concurrent interviews each fire 3 parallel TTS calls,
# producing 9–12 simultaneous OpenAI TTS requests. OpenAI silently stalls these
# connections (rather than returning 429) causing 10-minute hangs.
# This semaphore caps the total in-flight TTS calls at 3 across ALL rooms/threads.
# ─────────────────────────────────────────────────────────────────────────────
import threading

# Global variables for JobDiva authentication
ACCESS_TOKEN = None

# Global flag to track participant's presence
participant_present = True

# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3A: Global bounded thread pool for async per-question evaluation
# ═══════════════════════════════════════════════════════════════════════════════
evaluation_executor = ThreadPoolExecutor(
    max_workers=5,
    thread_name_prefix="eval-worker",
)


def process_evaluation(
    interview_id: int,
    row_id: int,
    skill: str,
    question_text: str,
    answer_text: str,
    round_number: int,
    question_number: int,
    skill_states: dict = None,
    candidate_id: int = None,
    job_id: int = None,
    process_id: str = None,
    room_id: str = None
) -> None:
    """Background thread entry point for per-question evaluation.
    Accepts ONLY primitives. Creates own DB session. Never raises."""
    logger.info(
        "🧵 [eval-worker] Starting evaluation: interview=%s Q%s row=%s",
        interview_id, question_number, row_id,
    )
    thread_session = AiraSession()
    try:
        # 1. Call evaluation LLM
        result = evaluate_answer(skill, question_text, answer_text, round_number)

        # 2. Update DB with evaluation result
        conf_score = result.get("confidence_score", 0.5)
        update_question_evaluation(
            thread_session,
            row_id,
            summary=result.get("summary", ""),
            confidence_score=conf_score,
            evaluation_tags=result.get("evaluation_tags", []),
            process_id=process_id,
            room_id=room_id
        )

        # Attribute this evaluation to EVERY skill the (possibly grouped) question
        # tested — `skill` may be a comma-joined list. The actual skill_state
        # update happens once, below (section 4), so no skill is scored twice.
        _eval_skills = [s.strip() for s in (skill or "").split(",") if s.strip()]

        # 3. Process project extraction
        projects = result.get("projects_mentioned", [])
        for proj in projects:
            if isinstance(proj, dict) and proj.get("name"):
                upsert_project(
                    thread_session,
                    interview_id,
                    proj,
                    question_number,
                    round_number,
                    candidate_id=candidate_id,
                    job_id=job_id,
                    process_id=process_id,
                    room_id=room_id
                )

        logger.info(
            "✅ [eval-worker] Completed: interview=%s Q%s confidence=%.2f tags=%s projects=%d",
            interview_id, question_number,
            result.get("confidence_score", 0.5),
            result.get("evaluation_tags", []),
            len(projects),
        )

        # 4. Phase 3C+ / Phase 4: Update runtime skill state + concept tracking for
        # EVERY skill tested by this question (grouped multi-skill questions score
        # each of their skills, so none are covered-but-unevaluated).
        if skill_states:
            conf = result.get("confidence_score", 0.5)
            concept_tags = result.get("concept_tags", [])
            for _sk in _eval_skills:
                state = skill_states.get(_sk)
                if not state or getattr(state, "status", "active") == "removed":
                    continue
                state.recent_conf.append(conf)
                logger.info(
                    "📈 [eval-worker] Appended confidence %.2f to skill '%s' (history: %s)",
                    conf, _sk, list(state.recent_conf)
                )
                # Concept Tracking (Phase 4): Extract tags safely, no status mutation
                for tag in concept_tags:
                    if not isinstance(tag, str) or not tag.strip():
                        continue
                    tag = tag.strip().lower()
                    if tag not in state.concepts:
                        state.concepts[tag] = ConceptState(tag)
                    state.concepts[tag].score_history.append(conf)
                    logger.info(f"🧠 [eval-worker] Appended conf {conf:.2f} to concept '{tag}' (skill '{_sk}') -> history len: {len(state.concepts[tag].score_history)}")

    except Exception as e:
        logger.error(
            "❌ [eval-worker] Failed: interview=%s Q%s row=%s error=%s",
            interview_id, question_number, row_id, e,
            exc_info=True,
        )
        try:
            mark_evaluation_failed(thread_session, row_id, process_id=process_id, room_id=room_id)
        except Exception as db_err:
            logger.critical(
                "💥 [eval-worker] DB failure marking row %s as failed: %s",
                row_id, db_err,
            )
    finally:
        thread_session.close()

# WebRTC audio integration control (can be disabled for faster fallback)
def is_webrtc_audio_enabled():
    """Check WebRTC audio setting dynamically at runtime"""
    return os.getenv("WEBRTC_AUDIO_ENABLED", "true").lower() == "true"

# Legacy constant for backward compatibility
WEBRTC_AUDIO_ENABLED = is_webrtc_audio_enabled()

# Use project root directory (same level as webrtc_interview.py)
BASE_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
QUESTIONS_DIR = os.path.join(BASE_DATA_DIR, "questions")
RESPONSE_FILE = os.path.join(BASE_DATA_DIR, "results", "interview_responses.json")

# Global repeat variables
repeat_qtn = False
repeat_count = 1
max_repeat_count = 3
is_start_of_interview = True

# --- Prompts, analysis, and advanced features ---


EC2_HOST = os.getenv("EC2_HOST", False)
EC2_USER = os.getenv("EC2_USER", False)
EC2_KEY = os.getenv("EC2_KEY", False)

# MySQL Details
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB = os.getenv("MYSQL_DB")

DB_CONFIG = {
    'host': 'localhost',  # will connect via tunnel
    'user': MYSQL_USER,
    'password': MYSQL_PASS,
    'database': MYSQL_DB,
    'cursorclass': pymysql.cursors.DictCursor
}


def get_db_connection():
    if not EC2_HOST:
        conn = pymysql.connect(
            host='localhost',
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASS,
            database=MYSQL_DB,
            cursorclass=pymysql.cursors.DictCursor
        )
        return conn, None
    from sshtunnel import SSHTunnelForwarder
    tunnel = SSHTunnelForwarder(
        (EC2_HOST, 22),
        ssh_username=EC2_USER,
        ssh_pkey=EC2_KEY,
        remote_bind_address=(MYSQL_HOST, MYSQL_PORT),
        local_bind_address=('127.0.0.1', 3307)
    )
    tunnel.start()
    logging.info(f"✅ SSH Tunnel established at 127.0.0.1:{tunnel.local_bind_port}")
    conn = pymysql.connect(
        host='localhost',
        port=tunnel.local_bind_port,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        database=MYSQL_DB,
        cursorclass=pymysql.cursors.DictCursor
    )
    return conn, tunnel




# ---------- REGEX HELPERS ----------









def get_headers():
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

def authenticate(room_id,process_id):
    global ACCESS_TOKEN

    # Check if required credentials are available
    if not all([CLIENT_ID, USERNAME, PASSWORD, AUTH_URL]):
        logger.warning("[process id: %s][room id: %s] JobDiva credentials not fully configured - skipping authentication",process_id,room_id)
        return False

    params = {
        "clientid": CLIENT_ID,
        "username": USERNAME,
        "password": PASSWORD
    }

    try:
        logger.info("[process id: %s][room id: %s] Attempting JobDiva authentication...",process_id,room_id)
        response = requests.get(AUTH_URL, params=params, timeout=30)
        logger.info("[process id: %s][room id: %s] JobDiva auth response status: %s",process_id,room_id, response.status_code)
        
        if response.status_code != 200:
            logger.warning("[process id: %s][room id: %s] JobDiva auth failed with status %s: %s",process_id,room_id, response.status_code, response.text[:200])
            return False
            
        # Check if response has content
        if not response.text.strip():
            logger.warning("[process id: %s][room id: %s] JobDiva returned empty response",process_id,room_id)
            return False
            
        try:
            data = response.json()
        except ValueError as json_error:
            logger.warning("[process id: %s][room id: %s] JobDiva returned invalid JSON: %s. Response: %s",process_id,room_id, json_error, response.text[:200])
            return False
        
        if 'access_token' in data:
            ACCESS_TOKEN = data['access_token']
            logger.info("[process id: %s][room id: %s] JobDiva authentication successful",process_id,room_id)
            return True
        else:
            logger.warning("No access token in JobDiva response: %s", data)
            return False
            
    except requests.RequestException as e:
        logger.warning("[process id: %s][room id: %s] JobDiva authentication network error: %s",process_id,room_id, e)
        return False
    except Exception as e:
        logger.warning("[process id: %s][room id: %s] JobDiva authentication unexpected error: %s",process_id,room_id, e)
        return False

def log_jobdiva_note(candidate_id, job_id, action, note_text,room_id,process_id):   
    # Attempt authentication
    if not authenticate(room_id,process_id):
        logger.warning("[process id: %s][room id: %s] Skipping JobDiva note due to authentication failure",process_id,room_id)
        return False
        
    if not API_BASE:
        logger.warning("[process id: %s][room id: %s] API_BASE not configured - skipping JobDiva note",process_id,room_id)
        return False
    
    url = f"{API_BASE}/jobdiva/createCandidateNote"
    params = {
        "candidateid": candidate_id,
        "note": note_text,
        "action": action,
        "link2AnOpenJob": job_id,
        "setAsAuto": "true"
    }
    logger.info("[process id: %s][room id: %s] Adding note to JobDiva for candidate %s with job ID %s",process_id,room_id, candidate_id, job_id)
    logger.info("[process id: %s][room id: %s] Note text: %s",process_id,room_id, note_text)
    try:
        response = requests.post(url, params=params, headers=get_headers(), timeout=30)
        response.raise_for_status()
        logger.info("[process id: %s][room id: %s]JobDiva note added successfully",process_id,room_id)
        return True
    except Exception as e:
        logger.warning("[process id: %s][room id: %s] Error adding JobDiva note (non-critical): %s",process_id,room_id, e)
        return False

def save_response(question, answer,room_id,process_id):
    response_data = {"question": question, "answer": answer}
    try:
        # Ensure the directory exists
        response_dir = os.path.dirname(RESPONSE_FILE)
        if not os.path.exists(response_dir):
            os.makedirs(response_dir, exist_ok=True)
            logger.info("[process id: %s][room id: %s] Created directory: %s",process_id,room_id, response_dir)
        
        if os.path.exists(RESPONSE_FILE):
            with open(RESPONSE_FILE, 'r', encoding='utf-8') as f:
                responses = json.load(f)
        else:
            responses = []
        
        responses.append(response_data)
        
        with open(RESPONSE_FILE, 'w', encoding='utf-8') as f:
            json.dump(responses, f, indent=2, ensure_ascii=False)
            
    except Exception as e:
        logger.error("[process id: %s][room id: %s] Error saving response: %s",process_id,room_id, e)


if not DG_API_KEY:
    raise RuntimeError("DEEPGRAM_API_KEY is required for transcription")


# Tunables (with safe defaults)
DG_STREAMING = os.getenv("DG_STREAMING", "true").lower() == "true"  # Enable WebSocket streaming















# === Interview Management Functions ===
def user_want_to_skip(resp_prompt):
    """Check if user wants to skip the question"""
    if not resp_prompt:
        return False
    
    skip_keywords = ["skip", "next", "pass", "move on", "don't know", "no idea"]
    resp_lower = resp_prompt.lower().strip()
    
    # Exact matches
    if resp_lower in ["skip", "skip this", "skip question", "next", "pass", "no", "don't know", "no idea"]:
        return True
        
    # Partial matches
    for keyword in skip_keywords:
        if keyword in resp_lower and len(resp_lower) < 20:  # Short responses containing skip keywords
            return True
            
    return False

def user_want_to_exit(prompt):
    """Check if user wants to exit the interview"""
    if not prompt:
        return False
        
    exit_keywords = ["exit", "quit", "end", "stop", "terminate", "leave", "bye", "goodbye"]
    prompt_lower = prompt.lower().strip()
    
    # Exact matches
    if prompt_lower in ["exit", "quit", "end", "stop", "terminate", "leave", "bye", "goodbye", "end interview"]:
        return True
        
    # Partial matches for short responses
    for keyword in exit_keywords:
        if keyword in prompt_lower and len(prompt_lower) < 15:
            return True
            
    return False



def confirm_user_want_to_exit(response, openai_client, system_prompt="You are a classifier. Decide if the candidate confirms exit."):
    """
    Use LLM to confirm if the candidate really wants to exit.
    Returns True if the candidate confirms exit, False otherwise.
    """
    # Handle empty/no response as confirmation (candidate likely disconnected or stayed silent)
    if not response or not response.strip():
        return True

    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"The candidate previously requested to END the interview. "
                    f"The interviewer then asked: 'Are you sure you want to end the interview?' "
                    f"The candidate replied: '{response}'.\n\n"
                    "Reply ONLY with 'true' if the candidate confirms they want to exit "
                    "(e.g. 'yes', 'yeah', 'sure', 'yes sir', 'yes please', 'I want to exit', 'quit', 'yes I am sure', 'yep', 'okay', affirmative responses).\n"
                    "Reply ONLY with 'false' if the candidate explicitly says they want to CONTINUE "
                    "(e.g. 'no', 'never mind', 'let's continue', 'I changed my mind', 'no I want to stay').\n"
                    "When in doubt, reply 'true' because the candidate already expressed intent to exit."
                ),
            },
        ],
        temperature=0.0,
        max_tokens=3,
    )

    decision = resp.choices[0].message.content.strip().lower()
    return decision == "true"



def greet_and_exit(user_name, driver=None,room_id="",process_id=0, **kwargs):
    """Greet the user and exit gracefully"""
    exit_messages = [
        f"Thank you {user_name} for your time today. We'll be in touch regarding next steps. Have a great day!",
        f"Thanks for participating in the interview, {user_name}. We appreciate your time and will follow up soon.",
        f"Thank you {user_name}. We've completed what we needed today. You'll hear from us about the next steps.",
        f"Great talking with you today, {user_name}. We'll review everything and get back to you shortly.",
        f"Thank you for your time today, {user_name}. We'll be in contact soon with updates on your application."
    ]
    
    import random
    message = random.choice(exit_messages)
    logger.info("[process id: %s][room id: %s] Exit message: %s",process_id,room_id, message)
    ai_speak(message, driver,room_id,process_id)

def mute_message(retries):
    """Generate message for muted user"""
    messages = [
        "I can't hear you clearly. Please unmute your microphone and try again.",
        "It seems like you might be muted. Could you please unmute and speak?",
        "I'm not getting any audio. Please check your microphone and unmute if necessary.",
        "I can't hear your response. Please ensure your microphone is unmuted and try again.",
        "No audio detected. Please unmute your microphone and give your answer."
    ]
    
    if retries >= 3:
        return "I've tried several times but can't hear you. Let's move to the next question."
    
    import random
    return random.choice(messages)

def ack_user_response(prev_question: str, response: str, is_last: bool = False) -> str:
    system_prompt = ( 
        "You are an AI interviewer. Your goal is to acknowledge the candidate’s responses politely and move the interview forward.\n\n"
        "• IMPORTANT RULE: Never ask any question, your role is to just acknowledge and end with a transition statement like: 'lets move to the next question.'"
        "• acknowlegement must be 1 or 2 sentences."
        "IMPORTANT RULES:\n" 
        "do not ask a question in return." 
        "• **Technical Questions**: If the candidate asks for a detailed explanation of a technical concept (e.g., 'What is recursion?', 'Explain polymorphism'), politely decline by saying: 'I cannot answer this question at this moment, let's continue with the next question.'\n" 
        "• **Non-technical Questions/Statements**: If the candidate talks about their experience, personal opinion, skills, or anything not related to technical concepts, provide a brief acknowledgment and transition to the next question. For example, 'I see you have experience in leadership, let's move on to the next question.'\n"
        "• **Acknowledgment**: After acknowledging the candidate's response, always transition politely to the next question with statements like: 'Let's continue with the next question.'\n\n" 
        "• always end the acknowledgement by saying statements like: 'Let's continue with the next question.' "

        "IMPORTANT NOTES:\n" 
        "• Avoid repeating the same phrases.\n" 
        "• Do not ask any follow-up questions or provide feedback on correctness.\n" 
        "• dont ever ask the user to continue the answer.\n" 
        "• Do not ever give acknowledgements like 'continue with the answer' or 'it seems like you are trying to answer continue with it' like these.\n" 
        "• If the candidate asks to skip a question or exit, handle it politely and transition to the next question.\n" 
        "• If the candidate shares personal experiences, such as 'I led a team of 5', acknowledge it positively like: 'That sounds like a valuable experience! Let's proceed.'\n\n" 
        "In short: Acknowledge non-technical responses and skip or decline technical questions." 
    )


    user_prompt = f"""
        Previous interview question: {prev_question}
        Candidate's response: {response}
        {"This was the last question. Wrap up the interview politely instead of transitioning." if is_last else ""}
                
        Provide a brief, professional acknowledgement following the rules above.
    """


    try:
        resp = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,  # Lower temperature for more consistent responses
            max_tokens=60     # Shorter to enforce brevity
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        # Fallback if API call fails
        if is_last:
            return "Thank you for your response. That concludes our interview."
        else:
            return "Thank you for your response."

def rephrase_question(original_question: str) -> str:
    """
    Rephrase a question using GPT to make it clearer/simpler.
    This is called when intent='rephrase' to generate a rephrased version.
    """
    system_prompt = (
        "You are an expert at rephrasing technical interview questions.\n"
        "Your goal is to take a question and rephrase it in simpler, clearer words.\n\n"
        "CRITICAL RULES:\n"
        "1. Keep the SAME topic/skill being tested\n"
        "2. Keep the SAME technical concept\n"
        "3. Just use different, simpler words\n"
        "4. Make it more conversational if possible\n"
        "5. DO NOT change what skill is being tested\n\n"
        "Example:\n"
        "Original: 'How would you utilize Python for data preprocessing in a machine learning pipeline?'\n"
        "Rephrased: 'Walk me through how you would use Python to prepare and clean data for a machine learning model.'\n\n"
        "Return ONLY the reprhased question, nothing else."
    )

    user_prompt = f"Rephrase this question:\n\n{original_question}"

    try:
        resp = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=150
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        # Fallback: return original question if rephrasing fails
        logger.error(f"Rephrase failed: {e}. Using original question.")
        return original_question

def polish_text(text: str) -> str:

    system_prompt = (
        "You are a professional editor and writing assistant.\n"
        "I’ll give you a block of text that is a mix of multiple ideas or writing styles.\n"
        "Your job is to rewrite it into a well-structured, fluent, and natural version that sounds coherent, polished, and engaging.\n\n"
        "Guidelines:\n"
        "• Keep the meaning and tone intact.\n"
        "• Make sure the text remains around the same length (don’t shorten or expand it significantly).\n"
        "• Fix grammar, flow, and readability.\n"
        "• Maintain the original emotional intent (whether it’s motivational, formal, technical, etc.)."
    )

    user_prompt = f"Here’s the text to polish:\n\n{text}"

    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=600,
        )

        return response.choices[0].message.content.strip()

    except Exception as e:
        return f"[Error while polishing text: {e}]"



def ack_last_question(answer, candidate_name, driver,room_id,process_id,user_name):
    try:
        system_prompt = (
            f"Thank the user {user_name} for taking time to join the interview and taking up the interview, "
            f"convey that our team will get back to them, Speak conversationally,  professionally, friendly, concise, and like a human interviewer. Avoid sounding like an email."
            f"give the response in 2 to 4 sentences."
        )

        response = openAIclient.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content":  system_prompt}
            ]
        )
        answer = response.choices[0].message.content
        ai_speak(answer,driver,room_id=room_id,process_id=process_id)
        return answer
    except Exception as e:
        return f"Error: {e}"







    






def update_responses(responses, asked_question, qtn_idx, user_input, flag, room_id, process_id,
                     interview_id=None, round_number=1, skill="",
                     effective_level=0, constraint_name="", skill_states=None, vector_dict=None,
                     candidate_id=None, job_id=None):
    """Update the responses list with question and answer.
    Phase 3A: Also inserts a sync DB row and submits async evaluation."""
    if "Before we get started, let me quickly explain how this interview will work: I'll be asking you questions via voice. You should respond verbally. Please make sure both your audio and video are turned on" in asked_question:
        return responses
    response_entry = {
        "id": qtn_idx,
        "question": asked_question,
        "answer": user_input if user_input else "No response",
        "flag": flag,
        "vector": vector_dict
    }
    responses.append(response_entry)
    save_response(asked_question, user_input, room_id, process_id)

    # ═══════════════════════════════════════════════════════════════════════
    # Phase 3A: Sync DB insert + async evaluation submission
    # ═══════════════════════════════════════════════════════════════════════
    if interview_id and user_input and user_input.strip() and not flag:
        try:
            # Phase 3C: Override default DB labels with actual vector dict
            if vector_dict:
                eff_lvl = vector_dict.get("A", effective_level)
                c_val = vector_dict.get("C", 0)
                amb_val = vector_dict.get("Amb", 0)
                s_val = vector_dict.get("S", 0)
                cnst_name = f"C{c_val}-Amb{amb_val}-S{s_val}"
            else:
                eff_lvl = effective_level
                cnst_name = constraint_name or "None"

            sync_session = AiraSession()
            row_id = insert_question_row(
                sync_session,
                interview_id=int(interview_id),
                round_number=int(round_number),
                question_number=int(qtn_idx),
                skill=str(skill),
                effective_level=int(eff_lvl or 0),
                constraint_name=str(cnst_name),
                question_text=str(asked_question),
                answer_text=str(user_input),
                candidate_id=int(candidate_id) if candidate_id and str(candidate_id).isdigit() else None,
                job_id=int(job_id) if job_id and str(job_id).isdigit() else None,
                process_id=process_id,
                room_id=room_id
            )
            sync_session.close()

            # Submit async evaluation — only primitives
            evaluation_executor.submit(
                process_evaluation,
                int(interview_id),
                int(row_id),
                str(skill),
                str(asked_question),
                str(user_input),
                int(round_number),
                int(qtn_idx),
                skill_states,
                int(candidate_id) if candidate_id and str(candidate_id).isdigit() else None,
                int(job_id) if job_id and str(job_id).isdigit() else None,
                process_id,
                room_id
            )
            logger.info(
                "🚀 [process id: %s][room id: %s] Submitted async eval for Q%s (row=%s)",
                process_id, room_id, qtn_idx, row_id,
            )
        except Exception as e:
            logger.error(
                "❌ [process id: %s][room id: %s] Phase 3A sync insert failed for Q%s: %s",
                process_id, room_id, qtn_idx, e,
                exc_info=True,
            )
            # Never block interview flow

    return responses



def user_want_to_repeat_question_or_exit_interview(question: str, answer: str, *, model: str = "gpt-3.5-turbo",room_id="",process_id=0) -> str:
    """
    LLM-only classifier. Returns one of: REPEAT | REPHRASE | EXIT | SKIP | AUDIO | VIDEO | OTHERS.
    """
    try:
        text = (answer or "").strip()
        if not text:
            return "OTHERS"
        system_prompt = (
            "You are an intent classifier for a live interview. "
            "Return ONLY one label: REPEAT, REPHRASE, EXIT, SKIP, AUDIO, VIDEO, or OTHERS.\n\n"
            "DEFINITIONS (be precise and strict):\n"
            "• REPEAT = Candidate EXPLICITLY asks the INTERVIEWER to repeare-state the question using the SAME words.\n"
            "  Examples: 'can you repeat the question?', 'please ask the question again', "
            "'could you say that question one more time?', 'I didn't catch the question', 'what was the question?'\n\n"
            "• REPHRASE = Candidate EXPLICITLY asks the INTERVIEWER to rephrase/restate the question in DIFFERENT words.\n"
            "  Examples: 'can you rephrase the question?', 'could you restate that differently?', "
            "'can you ask that in another way?', 'could you explain the question differently?', 'rephrase please'\n\n"
            "Do not classify statements like 'can you provide answer for this question' as REPEAT or REPHRASE - classify them as OTHERS.\n\n"
            "• AUDIO = Candidate is asking if their microphone/audio is clear or working.\n"
            "  Examples: 'am I audible?', 'can you hear me?', 'is my voice clear?', 'do you hear me clearly?', "
            "'can you hear my audio?', 'is my sound okay?'\n\n"
            "• VIDEO = Candidate is asking if their camera/video is clear or working.\n"
            "  Examples: 'is my video clear?', 'am I visible?', 'can you see me?    ', 'is my camera working?', "
            "'is my video on?', 'can you see my screen?'\n\n"
            "• EXIT = Candidate wants to end the interview.\n"
            "  Examples: 'I want to leave', 'end the interview', 'I'm done', 'goodbye', 'I quit'\n\n"
            "• SKIP = Candidate wants to skip the current question.\n"
            "  Examples: 'skip this', 'pass', 'next question', 'move on'\n\n"
            "• OTHERS = Everything else including:\n"
            "  - Actual answers to the interview question\n"
            "  - Candidate repeating words/phrases (like 'hello hello hello')\n"
            "  - Requests for topic explanations (e.g., 'I don't know what that topic is', 'what is X?')\n"
            "  - Statements about not knowing a topic (e.g., 'I don't know this topic', 'I'm not familiar with that')\n"
            "  - General conversation\n"
            "  - Candidate restating/paraphrasing the question themselves\n"
            "  - Greetings and chit-chat\n\n"
            "CRITICAL RULES:\n"
            "- If candidate asks about microphone or audio clarity → classify as AUDIO.\n"
            "- If candidate asks about video or camera visibility → classify as VIDEO.\n"
            "- If candidate asks you to repeat the QUESTION using same words → classify as REPEAT.\n"
            "- If candidate asks you to rephrase/restate the QUESTION in different words → classify as REPHRASE.\n"
            "- If candidate says they don't know a topic or asks for explanation → classify as OTHERS.\n"
            "- When in doubt, prefer AUDIO or VIDEO over OTHERS if the candidate is checking clarity.\n\n"
            "OUTPUT FORMAT: JSON only, e.g. {\"label\":\"OTHERS\"}"
        )
        user_prompt = (
            f"QUESTION: {question}\n"
            f"CANDIDATE RESPONSE: {answer}\n\n"
            "What is the candidate's intent? Return JSON label only."
        )
        resp = openai_client.chat.completions.create(
            model=model,
            temperature=0.0,
            max_tokens=10,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        label = json.loads(out).get("label", "").upper().strip()
        if label not in {"REPEAT", "REPHRASE", "EXIT", "SKIP", "VIDEO", "AUDIO", "OTHERS"}:
            label = "OTHERS"
        logger.info("[process id: %s][room id: %s] Intent classified as: %s for response: %s",process_id,room_id, label, answer[:50])
        return label
    except Exception as e:
        logger.error("[process id: %s][room id: %s] Error classifying intent: %s",process_id,room_id, e)
        return "OTHERS"
    
# --- helpers.py (or inside your orchestration module) ---














# --- helpers: adaptive capture wrappers ---






import json
import re

    
    




import threading
import queue
from dataclasses import dataclass,field
from typing import Optional, Dict, Any
import time
import json
import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from enum import Enum

class ThreadStopCommand(Enum):
    """Commands for the thread manager"""
    STOP_VIDEO = "stop_video"
    STOP_SCORE = "stop_score"
    STOP_ALL = "stop_all"
    SHUTDOWN = "shutdown"


@dataclass
class VideoStatus:
    """Minimal video status - only stores camera on/off state.
    Face visibility is checked on-demand at reflex time, not polled."""
    is_video_off: bool = False  # Camera turned off
    is_candidate_visible: bool = True  # Cached from last on-demand check
    lock: threading.Lock = field(default_factory=threading.Lock)
    
    def update_status(self, is_video_off: bool, is_candidate_visible: bool = None):
        """Update status flags atomically. is_candidate_visible is optional (on-demand only)."""
        with self.lock:
            self.is_video_off = is_video_off
            if is_candidate_visible is not None:
                self.is_candidate_visible = is_candidate_visible
    
    def get_status(self):
        """Get current status"""
        with self.lock:
            return {
                'is_video_off': self.is_video_off,
                'is_candidate_visible': self.is_candidate_visible
            }
        

@dataclass
class ScoreData:
    """Shared score data between threads"""
    total_score: float = 0.0
    avg_score: float = 0.0
    question_count: int = 0
    lock: threading.Lock = None
    
    def __post_init__(self):
        if self.lock is None:
            self.lock = threading.Lock()
    
    def update_score(self, new_score: float):
        with self.lock:
            self.total_score += new_score
            self.question_count += 1
            self.avg_score = self.total_score / self.question_count if self.question_count > 0 else 0.0
    
    def get_scores(self):
        with self.lock:
            return {
                'total_score': self.total_score,
                'avg_score': self.avg_score,
                'question_count': self.question_count
            }


class InterviewThreadManager:
    """Manages the three concurrent threads for the interview process"""
    
    def __init__(self, interview, driver, room_id, process_id, interview_id):
        self.interview = interview
        self.driver = driver
        self.room_id = room_id
        self.process_id = process_id
        self.interview_id = interview_id
        
        # Shared data structures
        self.video_status = VideoStatus()
        self.score_data = ScoreData()
        
        # Queues for communication
        self.score_queue = queue.Queue()  # (qtn_idx, question, answer, job_description, mandatory_skills, resume) tuples
        self.thread_control_queue = queue.Queue()  # Commands to stop threads
        self.video_check_event = threading.Event()
        
        # Thread control - separate stop events for each thread
        self.video_stop_event = threading.Event()
        self.score_stop_event = threading.Event()
        self.manager_stop_event = threading.Event()
        
        # Thread references
        self.video_thread = None
        self.score_thread = None
        self.manager_thread = None
        
        # Track which question is being processed
        self.current_question_idx = 0
        self.current_question_lock = threading.Lock()
    
    def start_threads(self):
        """Start all threads including the thread manager"""
        # Thread 1: Thread Manager (controls other threads)
        self.manager_thread = threading.Thread(
            target=self.thread_manager_thread,
            name="ThreadManagerThread",
            daemon=True
        )
        
        # Thread 2: Video monitoring
        self.video_thread = threading.Thread(
            target=self.video_monitoring_thread,
            name="VideoMonitoringThread",
            daemon=True
        )
        
        # Thread 3: Score calculation
        self.score_thread = threading.Thread(
            target=self.score_calculation_thread,
            name="ScoreCalculationThread",
            daemon=True
        )
        
        # Start threads (video monitoring removed — now on-demand)
        self.manager_thread.start()
        # self.video_thread.start()  # REMOVED: video+face checks are now on-demand, not polled
        self.score_thread.start()
        
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] Threads started (video monitoring: on-demand)")
    
    def thread_manager_thread(self):
        """Thread 1: Manages stopping of other threads in the background"""
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] Thread manager started")
        
        try:
            while not self.manager_stop_event.is_set():
                try:
                    # Wait for stop commands
                    command = self.thread_control_queue.get(timeout=1.0)
                    
                    if command == ThreadStopCommand.STOP_VIDEO:
                        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                                   f"Manager received command to stop video thread")
                        self._stop_video_thread_internal()
                        
                    elif command == ThreadStopCommand.STOP_SCORE:
                        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                                   f"Manager received command to stop score thread")
                        self._stop_score_thread_internal()
                        
                    elif command == ThreadStopCommand.STOP_ALL:
                        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                                   f"Manager received command to stop all threads")
                        self._stop_video_thread_internal()
                        self._stop_score_thread_internal()
                        
                    elif command == ThreadStopCommand.SHUTDOWN:
                        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                                   f"Manager received shutdown command")
                        self._stop_video_thread_internal()
                        self._stop_score_thread_internal()
                        break
                    
                    self.thread_control_queue.task_done()
                    
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"[process id: {self.process_id}][room id: {self.room_id}] "
                               f"Error in thread manager: {e}")
                    
        except Exception as e:
            logger.error(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"Fatal error in thread manager: {e}")
        finally:
            logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"Thread manager stopped")
    
    def _stop_video_thread_internal(self):
        """Internal method to stop video thread (called by manager thread)"""
        if self.video_thread and self.video_thread.is_alive():
            logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"Stopping VideoMonitoringThread...")
            
            self.video_stop_event.set()
            self.video_thread.join(timeout=10.0)
            
            if self.video_thread.is_alive():
                logger.warning(f"[process id: {self.process_id}][room id: {self.room_id}] "
                             f"VideoMonitoringThread did not stop gracefully")
            else:
                logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                          f"VideoMonitoringThread stopped successfully")
        else:
            logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"VideoMonitoringThread already stopped or not started")
    
    def _stop_score_thread_internal(self):
        """Internal method to stop score thread (called by manager thread)"""
        if self.score_thread and self.score_thread.is_alive():
            logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"Stopping ScoreCalculationThread...")
            
            self.score_stop_event.set()
            self.score_queue.put(None)  # Poison pill
            self.score_thread.join(timeout=10.0)
            
            if self.score_thread.is_alive():
                logger.warning(f"[process id: {self.process_id}][room id: {self.room_id}] "
                             f"ScoreCalculationThread did not stop gracefully")
            else:
                logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                          f"ScoreCalculationThread stopped successfully")
        else:
            logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"ScoreCalculationThread already stopped or not started")
    
    # Public methods to request thread stops (non-blocking)
    def stop_video_monitoring_thread(self):
        """Request to stop video monitoring thread in background (non-blocking)"""
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                   f"Requesting video thread stop (background)...")
        self.thread_control_queue.put(ThreadStopCommand.STOP_VIDEO)
    
    def stop_score_thread(self):
        """Request to stop score calculation thread in background (non-blocking)"""
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                   f"Requesting score thread stop (background)...")
        self.thread_control_queue.put(ThreadStopCommand.STOP_SCORE)
    
    def stop_threads(self):
        """Request to stop all worker threads in background (non-blocking)"""
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                   f"Requesting all threads stop...")
        self.thread_control_queue.put(ThreadStopCommand.STOP_ALL)
    
    def is_video_thread_alive(self):
        """Check if video monitoring thread is still running"""
        return self.video_thread and self.video_thread.is_alive()
    
    def is_score_thread_alive(self):
        """Check if score calculation thread is still running"""
        return self.score_thread and self.score_thread.is_alive()
    
    def shutdown(self):
        """Shutdown everything including the manager thread (blocking)"""
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                   f"Initiating shutdown...")
        
        # Send shutdown command
        self.thread_control_queue.put(ThreadStopCommand.SHUTDOWN)
        
        # Wait for manager to finish
        if self.manager_thread and self.manager_thread.is_alive():
            self.manager_thread.join(timeout=15.0)
            
        # Final cleanup - ensure all threads are stopped
        self.manager_stop_event.set()
        
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                   f"Shutdown complete")
    
    def set_current_question(self, qtn_idx):
        """Set the current question index being processed"""
        with self.current_question_lock:
            self.current_question_idx = qtn_idx
    
    def get_current_question(self):
        """Get the current question index"""
        with self.current_question_lock:
            return self.current_question_idx
    

    def video_monitoring_thread(self):
        """
        Thread that continuously monitors video status - NO LOGIC, just updates.
        Checks API/DB every 10 seconds and updates shared VideoStatus object.
        NOTE: Face visibility is also checked precisely at reflex time
        (inside _capture_with_speculative_reflex), so this background thread
        only serves as a safety net for prolonged camera-off scenarios.
        """
        logger.info(
            f"[process id: {self.process_id}][room id: {self.room_id}] "
            f"Video monitoring thread started"
        )
        
        check_interval = 10.0  # Check every 10 seconds (precise check fires at reflex time)
        
        try:
            while not self.video_stop_event.is_set():
                start_time = time.time()
                
                try:
                    # Simple DB check and status update
                    video_check(
                        self.process_id,
                        self.room_id,
                        self.interview_id,
                        self.video_status  # Pass the shared VideoStatus object
                    )
                    
                except Exception as e:
                    logger.error(
                        f"[process id: {self.process_id}][room id: {self.room_id}] "
                        f"Error in video check: {e}"
                    )
                
                # Wait for next check
                elapsed = time.time() - start_time
                remaining = max(0, check_interval - elapsed)
                self.video_stop_event.wait(timeout=remaining)
                
        except Exception as e:
            logger.error(
                f"[process id: {self.process_id}][room id: {self.room_id}] "
                f"Fatal error in video monitoring thread: {e}"
            )
        finally:
            logger.info(
                f"[process id: {self.process_id}][room id: {self.room_id}] "
                f"Video monitoring thread finished"
            )
    
    def score_calculation_thread(self):
        """Thread 3: Calculates scores asynchronously"""
        logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                   f"Score calculation thread started")
        
        try:
            while not self.score_stop_event.is_set():
                try:
                    # Wait for score calculation requests
                    score_request = self.score_queue.get(timeout=1.0)
                    
                    if score_request is None:  # Poison pill
                        break
                    
                    qtn_idx, question, answer, job_description, mandatory_skills, resume = score_request
                    
                    # Calculate score
                    start = time.time()
                    score_result = calculate_score(
                        question, 
                        answer, 
                        job_description, 
                        mandatory_skills, 
                        resume
                    )
                    score = score_result['score']
                    elapsed = time.time() - start
                    
                    logger.info(f"⏱️⏱️⏱️⏱️⏱️⏱️⏱️⏱️[process id: {self.process_id}][room id: {self.room_id}] "
                               f"Score calculated: {score} for question {qtn_idx} in {elapsed:.6f} seconds")
                    
                    # Update shared score data
                    self.score_data.update_score(score)
                    
                    # Mark task as done
                    self.score_queue.task_done()
                    
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"[process id: {self.process_id}][room id: {self.room_id}] "
                               f"Error calculating score: {e}")
                    
        except Exception as e:
            logger.error(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"Error in score calculation thread: {e}")
        finally:
            logger.info(f"[process id: {self.process_id}][room id: {self.room_id}] "
                       f"Score calculation thread finished")



def rephrase(question_to_ask, room_id, process_id, skill_states=None):
    """
    Rephrase the given question using OpenAI to reword it in a different way.
    
    Args:
        question_to_ask: The original question to be rephrased
        room_id: Room identifier
        process_id: Process identifier
        
    Returns:
        str: The rephrased question
    """
    
    system_prompt = (
        "You are an expert interviewer. Your task is to rephrase interview questions "
        "in a different way while maintaining the exact same meaning and intent.\n\n"
        "RULES:\n"
        "- Keep the core question and requirements identical\n"
        "- Use different words and sentence structure\n"
        "- Make it simpler or clearer if possible\n"
        "- Maintain professional interview tone\n"
        "- Do not add or remove any technical requirements\n"
        "- Return ONLY the rephrased question, nothing else\n\n"
        "OUTPUT FORMAT: Plain text question only (no JSON, no extra text)"
    )
    
    user_prompt = (
        f"Rephrase this interview question in different words:\n\n"
        f"{question_to_ask}"
    )
    
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            temperature=0.7,  # Slightly higher for creative rephrasing
            max_tokens=200,   # Enough tokens for a full question
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
        )
        
        rephrased_question = resp.choices[0].message.content.strip()
        return rephrased_question
        
    except Exception as e:
        logger.info(f"[process id: {process_id}][room id: {room_id}] Error in rephrasing question: {e}")
        # Fallback: return original question if rephrasing fails
        return question_to_ask

def remove_question_from_ack(ack, next_question):
    """Simple and effective question removal"""
    if not ack or not next_question:
        return ack
    next_question=next_question.lower()
    ack=ack.lower()
    if next_question in ack:
        cleaned = ack.replace(next_question, "").strip()
        
        # Remove trailing commas or extra punctuation
        cleaned = cleaned.rstrip(',').strip()
        
        # Add period if needed
        if cleaned and not cleaned.endswith('.'):
            cleaned += '.'
        
        return cleaned
    
    return ack


def _compute_resume_ratio(role: str, current_round: int) -> float:
    """Resume ratio starts at 0.25 (R1) and scales upward for mid/senior.
    Junior stays fixed at 0.25. Mid/Senior scale to 0.40 max."""
    BASE = 0.25
    if role.lower() == "junior":
        return BASE
    # Mid/Senior: ramp by 0.075 per round, capped at 0.40
    scaled = BASE + (current_round - 1) * 0.075
    return min(round(scaled, 3), 0.40)


def _extract_role_from_jd(job_description: str, process_id: int = 0, room_id: str = "") -> str:
    """Extract role experience level from JD via GPT when interview_config is null.
    Returns one of: 'junior', 'mid', 'senior'. Defaults to 'mid' on failure."""
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0,
            max_tokens=10,
            messages=[
                {"role": "system", "content": (
                    "You are a role classifier. Given a job description, output ONLY one word: "
                    "'junior', 'mid', or 'senior'. Base your decision on the title, years of "
                    "experience required, and seniority signals in the description. "
                    "If unclear, default to 'mid'."
                )},
                {"role": "user", "content": f"Job Description:\n{job_description[:3000]}"},
            ],
        )
        role = resp.choices[0].message.content.strip().lower()
        if role not in ("junior", "mid", "senior"):
            logger.warning(
                "[process id: %s][room id: %s] GPT returned invalid role '%s', defaulting to 'mid'",
                process_id, room_id, role,
            )
            return "mid"
        logger.info(
            "[process id: %s][room id: %s] 🎯 GPT-extracted role from JD: '%s'",
            process_id, room_id, role,
        )
        return role
    except Exception as e:
        logger.warning(
            "[process id: %s][room id: %s] ⚠️ GPT role extraction failed (%s), defaulting to 'mid'",
            process_id, room_id, e,
        )
        return "mid"


def ask_questions_in_webrtc(
    base_url_param,interview, driver=None, room_id=None, process_id=20, check_participant_fn=None, video_ready=True, sentinel_status={}
):
    """
    Main interview logic for AIRA WebRTC with multi-threading.
    Thread 1 (Main): Asks questions, captures answers, classifies intent
    Thread 2: Continuously monitors video status
    """
    # --- Setup ---
    global base_url
    base_url=base_url_param
    process_id = process_id
    room_id = room_id
    interview_id = interview["id"]
    questions_data = interview["interview_questions"]
    candidate_jd_id = interview["candidate_jd_id"]
    job_jd_id = interview["job_jd_id"]
    candidate_name = interview["candidate_name"]
    exit_count = 0
    max_exit_count = 2
    job_description = interview["job_description"]
    resume = interview["resume_text"]
    is_last = True
    face_invisible_since = None
    first_warning_given = False
    last_warning_time = None
    video_off_since = None
    video_off_warning_given = False
    company_name=interview.get('company_name', 'Unknown Company')

    # Task 2: cached JD questions (sequential) from the /claim `question_bank` field.
    # Absent field / kill-switch off / unavailable Redis → every path below falls back to
    # the live planner + TTS, so this is safe to leave dormant.
    qbank_plan = None
    qbank_cache = None
    question_audio_key = None      # audio key for the current question_to_ask (consumed at speak)
    if USE_CACHED_QUESTIONS:
        try:
            _qb = interview.get("question_bank")
            if _qb:
                from question_bank_cache import QuestionBankPlan, get_audio_cache
                _plan = QuestionBankPlan(_qb)
                if _plan.available:
                    qbank_plan = _plan
                    qbank_cache = get_audio_cache()
                    logger.info(
                        f"[process id: {process_id}][room id: {room_id}] "
                        f"📚 question_bank loaded: {qbank_plan.remaining} cached JD question(s)"
                    )
        except Exception as _qbe:
            logger.warning(
                f"[process id: {process_id}][room id: {room_id}] question_bank init failed: {_qbe}"
            )
            qbank_plan = None

    def convert_timezone(time_value, source_zone: str, target_zone: str, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime:
        if not source_zone:
            source_zone = "America/New_York"
        if not target_zone:
            target_zone = source_zone

        if isinstance(time_value, datetime):
            naive_dt = time_value
        else:
            naive_dt = datetime.strptime(time_value, fmt)
 
        source_dt = naive_dt.replace(tzinfo=ZoneInfo(source_zone))
        target_dt = source_dt.astimezone(ZoneInfo(target_zone))
        return target_dt
 
    converted = convert_timezone(interview["interview_time"], interview["interview_timezone"], interview["zone"])
    interview["interview_time"] = converted
    if interview["zone"]:
        interview["interview_timezone"] = interview["zone"]
    # Voice is now configured in generate_ai_response() using OpenAI TTS (voice="alloy")
    converted.strftime("%Y-%m-%d %H:%M:%S %p")
    post_first_question_video_notification_given = False
    
    logger.info(
        "[process id: %s][room id: %s] Starting interview for candidate ID: %s, Interview ID: %s, Job ID: %s, Candidate JD: %s",
        process_id, room_id, candidate_name, interview_id, job_jd_id, candidate_jd_id
    )
    
    # Initialize thread manager
    thread_manager = None
    
    try:
        if check_participant_fn and not check_participant_fn(room_id,base_url):
            logger.error("[process id: %s][room id: %s] No candidate joined. Terminating interview.", process_id, room_id)
            ai_speak("It looks like the candidate didn't join. We'll end this session now.", driver, room_id, process_id)
            return
        
        questions_data = []
        continue_asking = True
        responses = []
        conversation_timeline = []  # Track ALL conversations chronologically
        audio_video_issues = []
        qtn_idx = 1  # Start from 1 as per new prompt (Q1 is greeting)
        max_repeat_count = 3
        interview_start_time = datetime.now()
        # interview_end_time is computed later once config is initialized
        initial_video_notification_given = False
        
        current_vector_dict = None
        next_vector_dict = None
        question_tts_future = None  # Prefetched TTS audio for next question (set after planner resolves)
        
        data = sentinel_status.get("data", {})
        mandatory_skills = interview.get("mandatory_skills")
        if mandatory_skills:
            mandatory_skills = [s.strip() for s in mandatory_skills.split(",") if s.strip()]
        else:
            mandatory_skills = []
            
        optional_skills = interview.get("optional_skills")
        if optional_skills:
            optional_skills = [s.strip() for s in optional_skills.split(",") if s.strip()]
        else:
            optional_skills = []
        import platform
        if platform.system() == "Darwin":
            question_to_ask=f"Hi {candidate_name}, welcome.."
        else:
            question_to_ask = None  # Will be set after config is loaded
        # question_to_ask=f"Hi {candidate_name}, welcome.."

        # ── Step 2: Construct InterviewConfig from DB ──
        
        raw_config = interview.get("interview_config")
        if raw_config:
            if isinstance(raw_config, str):
                parsed_config = json.loads(raw_config)
            else:
                parsed_config = raw_config  # Already a dict from DB driver
            logger.info(
                "[process id: %s][room id: %s] 📦 Loaded interview_config from DB: %s",
                process_id, room_id, parsed_config,
            )
        else:
            # Fallback: extract role from JD via GPT, use safe defaults
            extracted_role = _extract_role_from_jd(job_description, process_id, room_id)
            parsed_config = {
                "total_rounds": 1,
                "questions_per_round": 15,
                "base_level": "medium",
                "role_experience_required": extracted_role,
                "pass_threshold_pct": INTERVIEW_PASS_THRESHOLD_PERCENTAGE,
                "max_duration_minutes": MAX_INTERVIEW_DURATION_MINUTES,
            }
            logger.warning(
                "[process id: %s][room id: %s] ⚠️ interview_config is NULL — using defaults with GPT-extracted role='%s'",
                process_id, room_id, extracted_role,
            )

        # ── CAMPUS: resolve config + merge target company/role PROFILE ──
        # A profile (skills + difficulty preset) fills GAPS only — any explicit value in
        # the DB interview_config wins. The resolved CampusConfig (mode/coaching/etc.) is
        # stashed on parsed_config for downstream use. All non-blocking.
        try:
            from campus.config import CampusConfig
            from campus.profiles import load_profile, profile_to_overrides
            _campus_overrides = parsed_config.get("campus") if isinstance(parsed_config, dict) else None
            campus_cfg = CampusConfig.resolve(_campus_overrides)
            if campus_cfg.profile:
                _prof = load_profile(campus_cfg.profile)
                if _prof:
                    for _k, _v in profile_to_overrides(_prof).items():
                        parsed_config.setdefault(_k, _v)
                    logger.info(f"[process id: {process_id}][room id: {room_id}] 🎓 Applied campus profile "
                                f"'{campus_cfg.profile}' ({_prof.get('label', '')})")
            parsed_config["_campus_config"] = campus_cfg.to_dict()
        except Exception as _pe:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Campus profile/config merge skipped (non-blocking): {_pe}")

        current_round = interview.get("current_round_number", 1) or 1
        role = parsed_config.get("role_experience_required", "mid")
        if company_name=="DemoCompany":
            parsed_config["questions_per_round"]=5
            parsed_config["max_duration_minutes"]=20
        # Extract explicit JD/resume question counts (optional — overrides ratio)
        jd_q_count = parsed_config.get("jd_question_count")
        resume_q_count = parsed_config.get("resume_question_count")
        if jd_q_count is not None:
            jd_q_count = int(jd_q_count)
        if resume_q_count is not None:
            resume_q_count = int(resume_q_count)

        # Extract concept/scenario split (optional — subdivides JD questions)
        conceptual_q_count = parsed_config.get("conceptual_question_count")
        scenario_q_count = parsed_config.get("scenario_question_count")
        if conceptual_q_count is not None:
            conceptual_q_count = int(conceptual_q_count)
        if scenario_q_count is not None:
            scenario_q_count = int(scenario_q_count)

        # ── Question SOURCE (jd/skill vs resume/project) & FORMAT (conceptual
        #    vs scenario) mix ────────────────────────────────────────────────────
        # questions_per_round is the number of CONTENT questions. Q1 greeting + Q2
        # background and adaptive follow-ups are SEPARATE and are NOT counted here.
        # Ratios come from the DB interview_config when present, else from .env.
        # Explicit counts in interview_config always win over ratios.
        _qpr = int(parsed_config.get("questions_per_round", NUMBER_OF_QUESTIONS))
        _content = max(1, _qpr)   # content-question budget (no greeting/reserve subtraction)

        _resume_ratio = parsed_config.get("resume_question_ratio")
        if _resume_ratio is None:
            _resume_ratio = float(os.getenv("RESUME_QUESTION_PERCENTAGE", "0.4"))
        _resume_ratio = max(0.0, min(1.0, float(_resume_ratio)))

        _conceptual_ratio = parsed_config.get("conceptual_question_ratio")
        if _conceptual_ratio is None:
            _conceptual_ratio = float(os.getenv("CONCEPTUAL_QUESTION_PERCENTAGE", "0.5"))
        _conceptual_ratio = max(0.0, min(1.0, float(_conceptual_ratio)))

        # Adaptive follow-up headroom — a SCHEDULING HINT only. It does NOT shrink the
        # content-question quota; follow-ups are asked on top of the content questions.
        _reserve_ratio = max(0.0, min(0.5, float(os.getenv("FOLLOWUP_RESERVE_PERCENTAGE", "0.2"))))
        _followup_reserve = max(0, round(_content * _reserve_ratio))

        # ── SOURCE split (jd/skill vs resume/project) over the content budget ──
        if resume_q_count is None:
            resume_q_count = round(_resume_ratio * _content)
        resume_q_count = max(0, min(resume_q_count, _content))

        if jd_q_count is not None:
            # jd explicit → the content total is jd + resume (matches InterviewConfig).
            jd_q_count = max(0, jd_q_count)
            _content = jd_q_count + resume_q_count
        else:
            jd_q_count = max(0, _content - resume_q_count)

        # ── FORMAT split (conceptual vs scenario) — STYLE over ALL content ─────
        # conceptual/scenario are a STYLE distribution across ALL content questions
        # (both JD-skill and resume-project), independent of jd_question_count.
        # Resume questions are conceptual-styled and count toward conceptual. When
        # EXPLICITLY configured they are honored as totals (capped at the content
        # count). The old code clamped conceptual to a post-resume JD budget — which
        # resume could shrink to 0 — silently turning "conceptual=4" into scenario.
        # That was the "bot asked scenario instead of conceptual" bug (e.g. job 6511).
        _concept_explicit = parsed_config.get("conceptual_question_count") is not None
        _scenario_explicit = parsed_config.get("scenario_question_count") is not None
        if _concept_explicit or _scenario_explicit:
            conceptual_q_count = max(0, min(int(conceptual_q_count or 0), _content))
            scenario_q_count = max(0, min(int(scenario_q_count or 0), _content))
        else:
            # Legacy ratio-based split over ALL content questions.
            conceptual_q_count = max(0, min(round(_conceptual_ratio * _content), _content))
            scenario_q_count = max(0, _content - conceptual_q_count)

        logger.info(
            f"[process id: {process_id}][room id: {room_id}] 📋 Question mix: "
            f"content={_content} (jd={jd_q_count}, resume={resume_q_count}; "
            f"followup_reserve={_followup_reserve}) → "
            f"conceptual={conceptual_q_count} ({_conceptual_ratio:.0%}), scenario={scenario_q_count}"
        )

        config = InterviewConfig(
            total_rounds=parsed_config.get("total_rounds", 1),
            questions_per_round=_content,
            resume_question_ratio=(resume_q_count / _content) if _content else _resume_ratio,
            base_difficulty=parsed_config.get("base_level", "medium"),
            role_experience_required=role,
            pass_threshold_pct=parsed_config.get("pass_threshold_pct", INTERVIEW_PASS_THRESHOLD_PERCENTAGE),
            max_duration_minutes=int(parsed_config.get("max_duration_minutes", MAX_INTERVIEW_DURATION_MINUTES)),
            jd_question_count=jd_q_count,
            resume_question_count=resume_q_count,
            conceptual_question_count=conceptual_q_count,
            scenario_question_count=scenario_q_count,
            followup_reserve_count=_followup_reserve,
        )
        config.log_config(prefix=f"[process id: {process_id}][room id: {room_id}] ")

        # Now that config is initialized, set the absolute end time based on configurable duration
        interview_end_time = interview_start_time + timedelta(minutes=config.max_duration_minutes)
        logger.info(f"⏳[process id: {process_id}][room id: {room_id}] Interview max duration set to {config.max_duration_minutes} minutes. Must end by {interview_end_time.strftime('%H:%M:%S')}")

        # Now set the full greeting with interview duration and question count
        if question_to_ask is None:
            question_to_ask = f"""Hi {candidate_name}, welcome! I'm glad you could join us today.
                Before we begin, let me quickly walk you through how this interview will work.
                I'll be asking you questions via voice, and you should respond verbally. Please make sure both your audio and video are turned on throughout the interview.
                This interview will last approximately {config.max_duration_minutes} minutes and will consist of {config.questions_per_round} questions.
                Each of your responses can be up to about five minutes long before we move on to the next question.
                Please note that the processing of your answer may take up to 8 seconds, and kindly mute yourself once you complete your response.
                I hope that makes sense. Just let me know whenever you're ready, and we'll get started!"""

        # ──────────────────────────────────────────────────────────────────
        # 🚀 PARALLEL LAUNCH: Fire TTS + Skill Groups + LLM warmup NOW
        #    while the main thread does baselines/context/skill states
        # ──────────────────────────────────────────────────────────────────
        from concurrent.futures import ThreadPoolExecutor as _LaunchPool
        launch_executor = _LaunchPool(max_workers=3, thread_name_prefix='launch')
        
        # ── PARALLEL TASK 1: Pre-generate Q1 greeting TTS ──────────────
        greeting_tts_future = None
        def _pregenerate_greeting_tts():
            # Skip if Cartesia is active — ai_speak_with_audio intercepts with
            # livekit_speak_cartesia and throws away these bytes anyway (TTFA ~300ms).
            if USE_CARTESIA:
                logger.info(
                    "⚡ [process id: %s][room id: %s] "
                    "Cartesia active — skipping Q1 greeting OpenAI TTS prefetch",
                    process_id, room_id,
                )
                return None
            try:
                tts_start = time.time()
                audio_data = generate_ai_response(question_to_ask, room_id, process_id)
                audio_stream = io.BytesIO()
                for chunk in audio_data:
                    if chunk:
                        audio_stream.write(chunk)
                audio_stream.seek(0)
                elapsed = time.time() - tts_start
                logger.info(
                    f"🎙️ [process id: {process_id}][room id: {room_id}] "
                    f"PRE-GENERATED Q1 greeting TTS in {elapsed*1000:.0f}ms (running in background)"
                )
                return audio_stream
            except Exception as e:
                logger.error(f"❌ [process id: {process_id}][room id: {room_id}] Pre-gen TTS failed: {e}")
                return None
        
        greeting_tts_future = launch_executor.submit(_pregenerate_greeting_tts)
        logger.info(f"🚀 [process id: {process_id}][room id: {room_id}] Fired PARALLEL: Q1 greeting TTS pre-generation")

        candidate_id = interview.get("c.id") or interview.get("candidate_id")
        job_id = interview.get("job.id") or interview.get("j.id") or interview.get("job_id")
        
        # ── Phase 3B: Load candidate skill baselines (main thread — fast) ──
        candidate_skill_baselines = {}
        try:
            baseline_session = AiraSession()
            candidate_skill_baselines = load_skill_baselines(baseline_session, candidate_id)
            baseline_session.close()
            if candidate_skill_baselines:
                logger.info(
                    "📊 [process id: %s][room id: %s] Phase 3B: Loaded %d skill baselines for candidate %s",
                    process_id, room_id, len(candidate_skill_baselines), candidate_jd_id,
                )
        except Exception as e:
            logger.warning(
                "⚠️ [process id: %s][room id: %s] Phase 3B baseline load failed (non-blocking): %s",
                process_id, room_id, e,
            )
        
        # ── Step 3: Construct InterviewContext ──
        context = InterviewContext(config=config)
        
        # Phase 3C: Initialize RuntimeSkillState for each mandatory skill
        from core.difficulty_policy import compute_difficulty_vector
        for skill in mandatory_skills:
            best_baseline = candidate_skill_baselines.get(skill)
            if not best_baseline:
                skill_lower = skill.lower()
                for b_key, b_data in candidate_skill_baselines.items():
                    if skill_lower in b_key.lower() or b_key.lower() in skill_lower:
                        best_baseline = b_data
                        break
            
            initial_vector = compute_difficulty_vector(
                base_difficulty=config.base_difficulty,
                role_experience_required=config.role_experience_required,
                round_number=context.current_round,
                skill_baseline=best_baseline,
                total_rounds=config.total_rounds,
            )
            context.skill_states[skill] = RuntimeSkillState(initial_vector)
            logger.info(f"Initialized skill state for '{skill}': {initial_vector}")
        
        client = ResponsesClient(
            api_key=OPENAI_API_KEY, 
            interview_config=config,
            candidate_skill_baselines=candidate_skill_baselines,
        )
        
        # ── PARALLEL TASK 2: Skill tracker init (has GPT call ~2.5s) ───
        skill_tracker_future = launch_executor.submit(
            client.init_skill_tracker,
            mandatory_skills=mandatory_skills,
            optional_skills=optional_skills
        )
        logger.info(f"🚀 [process id: {process_id}][room id: {room_id}] Fired PARALLEL: Skill tracker init (GPT skill groups)")
        
        reflex_engine = ReflexEngine(api_key=OPENAI_API_KEY, interview_config=config)
        planner_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='planner')
        tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tts-prefetch')
        followup_chain_count = 0  # Track consecutive follow-ups for current question
        # Weighted random: how many follow-ups for THIS question [0,1,2,3] with [15%,35%,40%,10%]
        max_followups_for_question = random.choices([0, 1, 2, 3], weights=[15, 35, 40, 10], k=1)[0]
        logger.info(f"🎲 [process id: {process_id}][room id: {room_id}] FOLLOWUP BUDGET for Q1: max={max_followups_for_question}")
        use_followup = False      # Whether current turn uses follow-up planner
        # Minimal messages for warmup/fallback — all real logic is in Planner + ReflexEngine
        messages = [
            {"role": "system", "content": f"You are an AI interviewer. Skills: {mandatory_skills + optional_skills}. Respond with JSON."}
        ]
        
        # ── PARALLEL TASK 3: LLM warmup (fire-and-forget) ─────────────
        warmup_result = {"response_id": None}
        
        def warmup_llm():
            try:
                warmup_messages = messages.copy()
                warmup_messages.append({"role": "user", "content": "Start the interview with the greeting question."})
                ai_response, metrics, response_id = client.create_response(
                    question_number=0,
                    messages=warmup_messages,
                    use_stateful=False,
                    context=context
                )
                warmup_result["response_id"] = response_id
                logger.info(f"[process id: {process_id}][room id: {room_id}] \u2705 LLM warmup completed (response_id: {response_id})")
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] \u274c LLM warmup failed: {e}")
        
        launch_executor.submit(warmup_llm)
        logger.info(f"🚀 [process id: {process_id}][room id: {room_id}] Fired PARALLEL: LLM warmup")
        
        # ── PARALLEL TASK 4: GPT resume project parsing (~2-3s) ───────
        # Parse resume to extract structured project names, tech, and details
        # so resume questions can reference specific projects by name.
        resume_parse_future = launch_executor.submit(client.parse_resume_projects, resume)
        logger.info(f"🚀 [process id: {process_id}][room id: {room_id}] Fired PARALLEL: Resume project parsing (GPT)")
        
        logger.info(f'\n[process id: {process_id}][room id: {room_id}] \U0001f916 AI Interviewer preparing... (4 tasks running in parallel)\n')
        
        # ── Wait for skill tracker (needed before main loop) ──────────
        try:
            skill_tracker_future.result(timeout=15)
            logger.info(f"✅ [process id: {process_id}][room id: {room_id}] Skill tracker ready")
        except Exception as e:
            logger.error(f"❌ [process id: {process_id}][room id: {room_id}] Skill tracker init failed: {e}")
            # Fallback: try synchronously — but never let a failure here crash the
            # whole multiprocessing pool (one bad interview must not kill the batch).
            try:
                client.init_skill_tracker(mandatory_skills=mandatory_skills, optional_skills=optional_skills)
            except Exception as e2:
                logger.error(f"❌ [process id: {process_id}][room id: {room_id}] Skill tracker fallback also failed: {e2} — continuing with degraded tracker")
        
        # ── Cross-round authenticity: seed prior answers ──────────────
        if current_round > 1:
            try:
                from integrations.aira_sql import load_previous_round_answers
                from answer_analysis.session_store import seed_prior_answers
                seed_session = AiraSession()
                prior_answers = load_previous_round_answers(
                    seed_session, candidate_jd_id, job_jd_id, current_round
                )
                seed_session.close()
                if prior_answers:
                    session_id = f"interview_{interview_id}"
                    seed_prior_answers(session_id, prior_answers)
                    logger.info(
                        f"🔗 [process id: {process_id}][room id: {room_id}] "
                        f"Seeded {len(prior_answers)} prior-round answers into authenticity session "
                        f"(Round {current_round}, candidate={candidate_jd_id})"
                    )
                else:
                    logger.info(
                        f"ℹ️ [process id: {process_id}][room id: {room_id}] "
                        f"No prior-round answers found for Round {current_round} seeding"
                    )
            except Exception as e:
                logger.warning(
                    f"⚠️ [process id: {process_id}][room id: {room_id}] "
                    f"Cross-round authenticity seeding failed (non-blocking): {e}"
                )
        
        ack = ""
        intent = "others"
        current_question_no = 1
        previous_intent = None  # Track previous intent for skill reuse on rephrase
        curr_question = question_to_ask
        previous_response_id = None
        ack_spoken_time = None       # Tracks when ack finishes speaking (for idle gap logging)
        transcript_ready_time = None  # Tracks when transcript is ready (for audio→ack logging)
        recent_acks = []             # Track last 3 acks to avoid repetition
        recent_ack_ids = []          # Task 2: track last cached-ack clip ids (no-immediate-repeat)


        
        # Track Q1 text
        if client.skill_tracker:
            client.skill_tracker.register_question(1, [], question_text=question_to_ask)
            
        logger.info(f"[process id: {process_id}][room id: {room_id}] Question: {question_to_ask} Question no:{current_question_no}")
        
        # Initialize thread manager and start background threads
        thread_manager = InterviewThreadManager(interview, driver, room_id, process_id, interview_id)
        thread_manager.start_threads()
        
        
        answer = ""
        
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"

        # ── HR ROUND TYPE DETECTION ─────────────────────────────────────
        # If round_type is "hr", branch to the HR behavioral interview engine
        # and skip the entire technical question flow.
        # PRIORITY: interview_config JSON > column > default
        # The column often carries a stale default ("technical") even when
        # the config JSON has been updated to "hr", so config wins.
        col_round_type = (interview.get("round_type") or "").strip().lower()
        cfg_round_type = (parsed_config.get("round_type") or "").strip().lower()
        round_type = cfg_round_type or col_round_type or "technical"
        logger.info(f"🔎 {log_prefix} Detected round_type = '{round_type}' (column={col_round_type or 'None'}, config={cfg_round_type or 'None'})")
        if round_type == "hr":
            logger.info(f"🎯 {log_prefix} ROUND TYPE = HR — branching to HR behavioral interview engine")
            try:
                from hr_webrtc_interview import run_hr_interview
                hr_result = run_hr_interview(
                    interview=interview,
                    parsed_config=parsed_config,
                    driver=driver,
                    room_id=room_id,
                    process_id=process_id,
                    interview_id=interview_id,
                    candidate_name=candidate_name,
                    job_description=job_description,
                    conversation_timeline=conversation_timeline,
                    ai_speak_fn=ai_speak,
                    capture_fn=_capture_normal,
                    add_timeline_fn=add_to_conversation_timeline,
                    thread_manager=thread_manager,
                )
                logger.info(f"✅ {log_prefix} HR interview complete. Evaluation: {hr_result.get('evaluation', {}).get('overall_score', 'N/A')}/5")
                
                # Stop threads and clean up
                if thread_manager:
                    thread_manager.stop_video_monitoring_thread()
                
                # Store HR results for report generation
                interview["hr_result"] = hr_result
                interview["round_type"] = "hr"
                
                # ── HR Post-Interview Pipeline ──────────────────────────
                # Mirror the technical flow: DB update, email, report save
                
                evaluation = hr_result.get("evaluation") or {}
                completion = hr_result.get("completion_status") or {}
                hr_score = evaluation.get("overall_score", 0) if evaluation else 0
                hr_summary = evaluation.get("summary", "") if evaluation else ""
                
                # 1. Set actual_duration on interview dict
                duration_min = completion.get("duration_minutes", 0)
                interview["actual_duration"] = (
                    f"{int(duration_min)} minutes" if duration_min >= 1
                    else f"{int(duration_min * 60)} seconds"
                )
                
                # 2. Determine pass/fail using DB pass_threshold_pct (0-100 → 0-5 scale)
                pass_threshold_5 = (config.pass_threshold_pct / 100.0) * 5.0
                hr_verdict = "Pass" if hr_score >= pass_threshold_5 else "Fail"
                logger.info(
                    f"📋 {log_prefix} HR Verdict: score={hr_score}/5, "
                    f"threshold={pass_threshold_5}/5 ({config.pass_threshold_pct}%), "
                    f"verdict={hr_verdict}"
                )
                
                # 3. Update DB with HR score/verdict/status
                try:
                    airadb.update_interview(interview_id, {
                        "interview_score": hr_score,
                        "interview_analysis": hr_summary,
                        "interview_verdict": hr_verdict,
                        "interview_status": "Complete"
                    }, process_id=process_id, room_id=room_id)
                    logger.info(f"✅ {log_prefix} DB updated: score={hr_score}, verdict={hr_verdict}")
                except Exception as e:
                    logger.error(f"❌ {log_prefix} Failed to update DB with HR results: {e}")
                
                # 4. Save HR report to interview_reports/ directory
                report_path = hr_result.get("report_path")
                if report_path:
                    logger.info(f"📄 {log_prefix} HR Report saved: {report_path}")
                    try:
                        import shutil
                        os.makedirs("interview_reports", exist_ok=True)
                        dest = os.path.join("interview_reports", f"{room_id}_hr_report.html")
                        shutil.copy2(report_path, dest)
                        logger.info(f"📄 {log_prefix} HR Report copied to: {dest}")
                    except Exception as e:
                        logger.warning(f"⚠️ {log_prefix} Failed to copy HR report: {e}")
                
                # 5. Persist HR report data to DB for ETL and report regeneration
                try:
                    # Convert dimension scores from LLM 1-10 → 0-5 for storage
                    dimension_scores_5 = []
                    for dim in (evaluation.get("scores", []) if evaluation else []):
                        dimension_scores_5.append({
                            "dimension_key": dim.get("dimension_key", ""),
                            "dimension_name": dim.get("dimension_name", ""),
                            "score": round(dim.get("score", 5) / 2.0, 1),
                            "justification": dim.get("justification", ""),
                        })
                    
                    hr_report_data = {
                        "overall_score": hr_score,
                        "summary": hr_summary,
                        "verdict_passed": hr_verdict == "Pass",
                        "verdict_status": hr_verdict,
                        "dimension_scores": dimension_scores_5,
                        "strengths": evaluation.get("strengths", []) if evaluation else [],
                        "concerns": evaluation.get("concerns", []) if evaluation else [],
                        "transcript": hr_result.get("transcript", []),
                        "topics_covered": completion.get("topics_covered", 0),
                        "topics_total": completion.get("topics_total", 0),
                        "total_responses": completion.get("responses", 0),
                        "explorations": completion.get("explorations", 0),
                        "duration_minutes": completion.get("duration_minutes", 0),
                        "completion_status": completion.get("status", "unknown"),
                        "phase": completion.get("phase", 1),
                        "raw_evaluation": evaluation,
                    }
                    airadb.save_hr_report_data(
                        interview_id, hr_report_data,
                        process_id=process_id, room_id=room_id
                    )
                    logger.info(f"✅ {log_prefix} HR report data saved to DB")
                except Exception as e:
                    logger.error(f"❌ {log_prefix} Failed to save HR report data to DB: {e}")
                
                # 6. Send HR report via email
                try:
                    if EMAIL_ENABLED and email_client:
                        logger.info(f"{log_prefix} Sending HR interview report email...")
                        # Read the HR report HTML
                        hr_html = ""
                        if report_path and os.path.exists(report_path):
                            with open(report_path, "r", encoding="utf-8") as f:
                                hr_html = f.read()
                        
                        if hr_html:
                            import base64
                            safe_name = email_client.sanitize_filename(candidate_name)
                            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                            
                            # Build attachments list
                            attachments = [{
                                "@odata.type": "#microsoft.graph.fileAttachment",
                                "name": f"{safe_name}_HR_Report_{timestamp_str}.html",
                                "contentType": "text/html",
                                "contentBytes": base64.b64encode(hr_html.encode("utf-8")).decode("utf-8"),
                            }]
                            
                            # Try PDF conversion
                            try:
                                from integrations.MSEmailClient import convert_interview_html_to_pdf
                                import tempfile
                                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_pdf:
                                    pdf_path = convert_interview_html_to_pdf(
                                        hr_html, tmp_pdf.name,
                                        interview.get("candidate_jd_id", interview_id),
                                        process_id, room_id
                                    )
                                    with open(pdf_path, "rb") as pf:
                                        pdf_bytes = base64.b64encode(pf.read()).decode("utf-8")
                                    attachments = [{
                                        "@odata.type": "#microsoft.graph.fileAttachment",
                                        "name": f"{safe_name}_HR_Report_{timestamp_str}.pdf",
                                        "contentType": "application/pdf",
                                        "contentBytes": pdf_bytes,
                                    }]
                                    os.unlink(tmp_pdf.name)
                                    logger.info(f"✅ {log_prefix} HR Report PDF generated")
                            except Exception as pdf_err:
                                logger.warning(f"⚠️ {log_prefix} HR PDF conversion failed, using HTML: {pdf_err}")
                            
                            subject = f"[AIRA] HR Interview Report – {candidate_name} for {interview.get('job_title', '')}"
                            recipients = email_client._resolve_email_recipients(interview)
                            email_client.send_email(subject, hr_html, recipients, "support@airahr.ai", email_ics_msg=attachments)
                            logger.info(f"✅ {log_prefix} HR report email sent to {recipients}")
                        
                        # Also send feedback email
                        try:
                            email_client.send_interview_feedback_email(interview, DEPLOYMENT)
                            logger.info(f"✅ {log_prefix} HR feedback email sent")
                        except Exception as fe:
                            logger.warning(f"⚠️ {log_prefix} HR feedback email failed: {fe}")
                    else:
                        logger.warning(f"{log_prefix} Email disabled — HR report not emailed")
                except Exception as e:
                    logger.error(f"❌ {log_prefix} Failed to send HR report email: {e}")
                    import traceback
                    logger.error(f"{log_prefix} Email error: {traceback.format_exc()}")
                
                # 7. Archive interview to history table
                try:
                    import integrations.aira_sql as airadb_archive
                    success, history_id = airadb_archive.archive_and_delete_interview(
                        room_id=room_id,
                        process_id=process_id,
                        company_name=interview.get("company_name"),
                        interview_id=interview_id,
                    )
                    if success:
                        logger.info(f"✅ {log_prefix} HR interview archived to history (ID: {history_id})")
                    else:
                        logger.error(f"❌ {log_prefix} Failed to archive HR interview")
                except Exception as e:
                    logger.error(f"❌ {log_prefix} Archive call failed for HR interview: {e}")
                
                return hr_result
                
            except Exception as e:
                logger.error(f"❌ {log_prefix} HR interview engine failed: {e}", exc_info=True)
                # Fallback: continue with technical interview
                logger.warning(f"⚠️ {log_prefix} Falling back to technical interview flow")
        
        sentinel_enabled = interview.get("sentinel_enabled", 1)
        if platform.system() == "Darwin":
            sentinel_flag = True
        elif sentinel_enabled == 0:
            sentinel_flag = True  # Sentinel not needed for this interview — proceed
            logger.info(f"⏭️ [process id: {process_id}][room id: {room_id}] Sentinel disabled for this interview, proceeding without.")
        else:
            sentinel_flag = sentinel_status.get("ok") and data.get("status") == "started"
        push_to_report=False
        
        def _check_driver_alive(drvr, rm_id, pid, ctx=""):
            """Forensic helper: verifies Chrome driver is still responding."""
            try:
                # A lightweight call to check if the session is alive
                _ = drvr.current_url
                return True
            except Exception as d_err:
                logger.critical(f"💀 CHROME DRIVER DEAD [PID: {pid}][room: {rm_id}] at {ctx} — error: {d_err}")
                return False

        if True:
            logger.info(f"🔔 AUDIT [PID: {process_id}][room: {room_id}] INTERVIEW_START interview_id={interview_id}")
            # ── Ensure face_visible = 1 at the start of interview ──
            try:
                if not airadb._IS_LOCAL:
                    airadb._api_call("PUT", "/interview/update", json={
                        "interviewId": interview_id,
                        "fields": {"candidatePresent": 1}
                    }, process_id=process_id, room_id=room_id)
                else:
                    from sqlalchemy import text as sa_text
                    airadb.session.execute(
                        sa_text("UPDATE interviews SET candidate_present = 1 WHERE id = :iid"),
                        {"iid": interview_id}
                    )
                    airadb.session.commit()
                logger.info(f"✅ [process id: {process_id}][room id: {room_id}] Set candidate_present=1 at interview start")
            except Exception as e:
                logger.warning(f"⚠️ [process id: {process_id}][room id: {room_id}] Failed to set candidate_present=1: {e}")
            while current_question_no <= config.questions_per_round + 1 and continue_asking and question_to_ask != "" :
                logger.info(f"🔔 AUDIT [PID: {process_id}][room: {room_id}] QUESTION_START q={current_question_no} text={question_to_ask[:30]}...")
                # Tell the bot which filler bucket to use (opening/mid/closing) if it
                # has to wait for a Cartesia slot while speaking this question.
                _fadapter = get_livekit_adapter()
                if _fadapter:
                    _fadapter.set_filler_phase(current_question_no, config.questions_per_round)
                # Tell the bot which filler bucket to use (opening/mid/closing) if it
                # has to wait for a Cartesia slot while speaking this question.
                _fadapter = get_livekit_adapter()
                if _fadapter:
                    _fadapter.set_filler_phase(current_question_no, config.questions_per_round)
                if current_question_no >= 3:
                    client.update_tracker_after_response(intent)
                if datetime.now() > interview_end_time:
                    ai_speak(
                        "Alright, appreciate your responses. We've reached the allotted time; we'll conclude the session here. "
                        "We will review your responses and reach out regarding the next steps. Thank you.",
                        driver, room_id, process_id
                    )
                    break
                
                # Update current question index for video thread
                thread_manager.set_current_question(current_question_no)
                repeat_qtn = True
                repeat_count = 1
                candidate_exit = False
                
                while repeat_qtn and continue_asking:
                    if current_question_no > 1:
                        is_video_off = check_video_status(room_id, process_id)
                        
                        if is_video_off:
                            # Start tracking video off duration
                            if video_off_since is None:
                                video_off_since = datetime.now()
                                logger.warning(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    "Video turned off"
                                )
                            
                            # Give warning if not given yet
                            if not video_off_warning_given:
                                logger.info(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    "Video is OFF - giving warning"
                                )
                                
                                video_warning_1 = (
                                    "I notice your video is turned off. Please turn on your video. "
                                    "It is extremely important to have your video turned on throughout "
                                    "the interview, thank you."
                                )
                                ai_speak(video_warning_1, driver, room_id, process_id)
                                add_to_conversation_timeline(
                                    conversation_timeline, 
                                    'warning', 
                                    video_warning_1, 
                                    "", 
                                    current_question_no, 
                                    room_id, 
                                    process_id
                                )
                                
                                video_off_warning_given = True
                                time.sleep(5)
                                
                                # Check again after warning
                                if check_video_status(room_id, process_id):
                                    video_warning_2 = (
                                        f"{candidate_name}, to proceed, we need you to turn on your video, "
                                        "be clearly visible, and avoid filters, blurred backgrounds, or poor "
                                        "lighting so we can connect better."
                                    )
                                    ai_speak(video_warning_2, driver, room_id, process_id)
                                    add_to_conversation_timeline(
                                        conversation_timeline, 
                                        'warning', 
                                        video_warning_2, 
                                        "", 
                                        current_question_no, 
                                        room_id, 
                                        process_id
                                    )
                                    time.sleep(5)
                                    
                                    # Final check
                                    if not check_video_status(room_id, process_id):
                                        logger.info(
                                            f"[process id: {process_id}][room id: {room_id}] "
                                            "Video turned on after warnings"
                                        )
                                        add_to_conversation_timeline(
                                            conversation_timeline, 
                                            'warning', 
                                            "Candidate video is now ON after warning", 
                                            "", 
                                            current_question_no, 
                                            room_id, 
                                            process_id
                                        )
                                    else:
                                        logger.info(
                                            f"[process id: {process_id}][room id: {room_id}] "
                                            "Video still off after warnings - continuing interview"
                                        )
                                        add_to_conversation_timeline(
                                            conversation_timeline, 
                                            'warning', 
                                            "Video still OFF after final warning - continuing interview", 
                                            "", 
                                            current_question_no, 
                                            room_id, 
                                            process_id
                                        )
                                else:
                                    logger.info(
                                        f"[process id: {process_id}][room id: {room_id}] "
                                        "Video turned on after first warning"
                                    )
                        
                        else:  # Video is ON
                            # If video was off before and now is on
                            if video_off_since is not None:
                                video_off_duration = (datetime.now() - video_off_since).total_seconds()
                                logger.info(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"Video turned back on after {video_off_duration:.1f} seconds"
                                )
                                
                                # Record issue if off > 30 seconds
                                if video_off_duration > 30:
                                    msg = (
                                        f"Video monitoring alert: The candidate's video was turned off "
                                        f"for {video_off_duration:.1f} seconds while responding to "
                                        f"question number {current_question_no}."
                                    )
                                    if msg not in audio_video_issues:
                                        audio_video_issues.append(msg)
                                
                                # Reset tracking variables
                                video_off_since = None
                                video_off_warning_given = False
                    
                    # ================================================================
                    # VIDEO CHECK 2: FACE VISIBILITY (after Q1)
                    # On-demand check — only fires once per question loop iteration
                    # ================================================================
                    if current_question_no > 1:
                        is_candidate_visible = check_face_visibility(process_id, room_id, interview_id)
                        # Cache into video_status for other consumers
                        thread_manager.video_status.update_status(
                            thread_manager.video_status.is_video_off, is_candidate_visible
                        )
                        
                        # Face NOT visible - handle warnings and potential termination
                        if not is_candidate_visible:
                            
                            # Start tracking invisible duration
                            if face_invisible_since is None:
                                face_invisible_since = datetime.now()
                                logger.warning(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"Face became invisible at {face_invisible_since}"
                                )
                            
                            # Calculate how long face has been invisible
                            face_invisible_duration = (datetime.now() - face_invisible_since).total_seconds()
                            
                            logger.warning(
                                f"[process id: {process_id}][room id: {room_id}] "
                                f"Face not visible for {face_invisible_duration:.1f} seconds"
                            )
                            
                            # TERMINATION CHECK: 5 minutes grace period expired
                            if face_invisible_duration >= 300:  # 5 minutes
                                logger.critical(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    "Interview terminated: face not visible for 5 minutes"
                                )
                                
                                termination_messages = [
                                    "I regret to inform you that this interview session has been terminated. You were not visible in the camera for 5 consecutive minutes, which violates our interview monitoring policy. If you believe this termination occurred due to a technical error or misunderstanding, please reach out to our support team with your interview ID for review. Thank you for your time.",
                                    
                                    "Unfortunately, I must terminate this interview session. Our system has detected that your face was not visible in the camera for the full 5-minute grace period. This is a mandatory requirement for maintaining interview integrity. If you feel this was due to a technical issue on our end, please contact our support team immediately with your session details for investigation.",
                                    
                                    "This interview has now been officially terminated. Despite multiple warnings, you were not facing the camera for 5 continuous minutes, which exceeds our policy limits. We take interview monitoring seriously to ensure fairness for all candidates. If you believe there was a technical malfunction or error, please contact our support department with your interview reference number for assistance.",
                                    
                                    "I am terminating this interview session as of now. You were not visible on camera for the entire 5-minute warning period, which is against our interview conduct policy. This requirement ensures authenticity and fairness in our assessment process. Should you have concerns about technical difficulties or believe this was an error, please get in touch with our support team providing your interview ID and we will review the situation.",
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
                                answer = "BOT NOTE: Interview session terminated due to inactivity — candidate did not enable video for 5 consecutive minutes."
                                responses = update_responses(responses, question_to_ask, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                                # Mark as terminated and exit
                                continue_asking = False
                                break
                            
                            # FIRST WARNING: Face just became invisible
                            if not first_warning_given:
                                logger.warning(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    "Issuing first warning for face not visible"
                                )
                                
                                first_warning_messages = [
                                    "Your face is not visible. Please adjust your position so I can see you clearly.",
                                    
                                    "You've moved away from the camera. Please ensure your face is centered and visible.",
                                    
                                    "I can't see you on camera. Please reposition so your face is clearly visible.",
                                    
                                    "You're not facing the camera. Please adjust your position to show your face.",
                                    
                                    "You're not visible in the camera view. Please ensure your face is clearly shown.",
                                ]
                                
                                first_warning_msg = random.choice(first_warning_messages)
                                ai_speak(first_warning_msg, driver, room_id, process_id)
                                add_to_conversation_timeline(
                                    conversation_timeline, 
                                    'warning', 
                                    first_warning_msg, 
                                    "", 
                                    current_question_no, 
                                    room_id, 
                                    process_id
                                )
                                
                                first_warning_given = True
                                last_warning_time = datetime.now()
                                time.sleep(3)
                                continue  # Re-check before proceeding
                            
                            # PERIODIC WARNINGS: Every 30 seconds while face invisible
                            elif last_warning_time is None or (datetime.now() - last_warning_time).total_seconds() >= 30:
                                time_remaining = 300 - face_invisible_duration
                                
                                if time_remaining > 0:
                                    minutes_remaining = int(time_remaining // 60)
                                    seconds_remaining = int(time_remaining % 60)
                                    
                                    logger.warning(
                                        f"[process id: {process_id}][room id: {room_id}] "
                                        f"Periodic warning: {minutes_remaining}m {seconds_remaining}s remaining"
                                    )
                                    
                                    periodic_warning_messages = [
                                        "Your face must be visible. Please face the camera now.",
                                        
                                        "I still cannot see your face. Please position yourself in front of the camera.",
                                        
                                        "Your face is still not visible. Please adjust immediately.",
                                        
                                        "You are still not facing the camera. Please adjust your position now."
                                    ]
                                    
                                    random_warning = random.choice(periodic_warning_messages)
                                    formatted_warning = random_warning.format(
                                        minutes=minutes_remaining,
                                        min_plural='s' if minutes_remaining != 1 else '',
                                        seconds=seconds_remaining,
                                        sec_plural='s' if seconds_remaining != 1 else ''
                                    )
                                    
                                    ai_speak(formatted_warning, driver, room_id, process_id)
                                    add_to_conversation_timeline(
                                        conversation_timeline, 
                                        'warning', 
                                        formatted_warning, 
                                        "", 
                                        current_question_no, 
                                        room_id, 
                                        process_id
                                    )
                                    
                                    last_warning_time = datetime.now()
                                    time.sleep(3)
                                    continue  # Re-check before proceeding
                            
                            # Record issue if face invisible > 60 seconds
                            if face_invisible_duration > 60:
                                msg = (
                                    f"Video monitoring alert: The candidate's face was not visible "
                                    f"in the camera for {face_invisible_duration:.1f} seconds during "
                                    f"question number {current_question_no}. This violation has been "
                                    "recorded in the interview log."
                                )
                                if msg not in audio_video_issues:
                                    audio_video_issues.append(msg)
                            
                            # Still not visible - wait and re-check
                            time.sleep(2)
                            continue  # Re-check status
                        
                        # Face IS visible - check if it just became visible after being invisible
                        else:  # is_candidate_visible == True
                            if first_warning_given:  # Face was invisible before, now visible
                                was_invisible_for = (datetime.now() - face_invisible_since).total_seconds()
                                
                                logger.info(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"Face became visible again after {was_invisible_for:.1f} seconds"
                                )
                                
                                resume_messages = [
                                    "Thank you, I can see you now. Let's continue the interview.",
                                    
                                    "Perfect, you're back in frame. Let's resume our conversation.",
                                    
                                    "Great, I can see you properly now. Let's get back to the interview.",
                                    
                                    "Excellent, your video is working. Let's continue with the next questions.",
                                    
                                    "Thank you, you're clearly visible now. Let's pick up where we left off.",
                                ]
                                
                                resume_msg = random.choice(resume_messages)
                                ai_speak(resume_msg, driver, room_id, process_id)
                                add_to_conversation_timeline(
                                    conversation_timeline, 
                                    'resume', 
                                    resume_msg, 
                                    "", 
                                    current_question_no, 
                                    room_id, 
                                    process_id
                                )
                                
                                # Reset tracking variables
                                face_invisible_since = None
                                first_warning_given = False
                                last_warning_time = None

                    # NOTE: ack is now spoken inside the split-brain block (immediately after reflex)
                    # The old ack speaking here has been removed to prevent duplicate speech
                    
                    # Ask the question
                    start = time.time()
                    
                    # Speak the question — use pre-fetched TTS audio when available
                    idle_after_ack = time.time() - ack_spoken_time if ack_spoken_time else 0
                    logger.info(
                        f"🕐 [process id: {process_id}][room id: {room_id}] "
                        f"ACK→QUESTION IDLE: {idle_after_ack:.2f}s (bot idle between ack end and question start)"
                    )
                    
                    # Determine which pre-generated audio source to use (if any)
                    # Priority: Q1 greeting TTS > prefetched question TTS > live TTS
                    pre_gen_audio = None
                    if current_question_no == 1 and greeting_tts_future is not None:
                        # Q1: Use the greeting TTS that was fired during init
                        try:
                            pre_gen_audio = greeting_tts_future.result(timeout=10)
                            if pre_gen_audio:
                                logger.info(
                                    f"⚡ [process id: {process_id}][room id: {room_id}] "
                                    f"Using PRE-GENERATED TTS for Q1 greeting (0ms TTS latency)"
                                )
                        except Exception as e:
                            logger.warning(f"⚠️ [process id: {process_id}][room id: {room_id}] Q1 pre-gen TTS failed: {e}, will try prefetch")
                        greeting_tts_future = None  # Clear after use
                    
                    if pre_gen_audio is None and question_tts_future is not None:
                        # Q2+: Use the TTS that was prefetched while video checks ran
                        try:
                            pre_gen_audio = question_tts_future.result(timeout=15)
                            if pre_gen_audio:
                                logger.info(
                                    f"⚡ [process id: {process_id}][room id: {room_id}] "
                                    f"Using PREFETCHED TTS for Q{current_question_no} (0ms TTS latency)"
                                )
                        except Exception as e:
                            logger.warning(f"⚠️ [process id: {process_id}][room id: {room_id}] Prefetch TTS failed: {e}, falling back to live TTS")
                        question_tts_future = None  # Clear after use
                    
                    # Task 2: if this question came from the cached bank, play its Redis
                    # audio from memory (zero Cartesia). Any miss falls back to live TTS.
                    _cached_q_pcm = None
                    if USE_CACHED_QUESTIONS and question_audio_key and qbank_cache is not None:
                        _cached_q_pcm = qbank_cache.get_pcm(question_audio_key)
                    if _cached_q_pcm:
                        _adp = get_livekit_adapter()
                        if not (_adp and _adp.play_cached_clip(_cached_q_pcm, qbank_plan.sample_rate, question_audio_key)):
                            ai_speak(question_to_ask, driver, room_id, process_id)
                        else:
                            logger.info(
                                f"📚 [process id: {process_id}][room id: {room_id}] "
                                f"CACHED QUESTION played from Redis [{question_audio_key}] — no Cartesia"
                            )
                    elif pre_gen_audio:
                        ai_speak_with_audio(question_to_ask, pre_gen_audio, driver, room_id, process_id)
                    else:
                        ai_speak(question_to_ask, driver, room_id, process_id)
                    question_audio_key = None  # consume — repeats/rephrases fall back to live

                    # update_current_question(room_id, current_question_no, question_to_ask)
                    add_to_conversation_timeline(conversation_timeline, 'technical_qa', question_to_ask, "", current_question_no, room_id, process_id)
                    
                    # Track question text immediately when spoken
                    if client.skill_tracker:
                        client.skill_tracker._asked_questions[current_question_no] = question_to_ask.strip().lower()
                    
                    end = time.time()
                    elapsed = end - start
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Execution time to speak question {current_question_no}: {elapsed:.6f} seconds")

                    logger.info("🎤[process id: %s][room id: %s] Recording candidate response...", process_id, room_id)
                    start = time.time()

                    # First 2 questions: capture the candidate's face WHILE they
                    # answer (preferred best-face source). The FaceDetector also
                    # keeps a first-20s-of-presence baseline as a fallback.
                    if current_question_no <= 2:
                        _capture_face_during_answer()

                    # For ALL questions, use speculative reflex (fire GPT during VAD silence)
                    # Q1 is handled directly inside _capture_with_speculative_reflex via generate_q1_reflex
                    answer_dict, speculative_reflex_result, _, speculative_hit, speech_end_time, speculative_followup_result = \
                        _capture_with_speculative_reflex(
                            driver, room_id, process_id,
                            reflex_engine, question_to_ask, job_description,
                            mandatory_skills, current_question_no, recent_acks,
                            followup_chain_count=followup_chain_count,
                            max_followups_for_question=max_followups_for_question,
                            interview_id=interview_id,
                            quota_forcing=client.quota_needs_slot(current_question_no + 1)
                        )
                    
                    if answer_dict:
                        answer=answer_dict["answer"]
                        answer_duration=answer_dict["answer_duration"]
                    else:
                        answer=""
                        answer_duration=0
                    end = time.time()
                    elapsed = end - start
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Execution time (for whole audio record and transcribe process): {elapsed:.6f} seconds")

                    # Initialize pending_analysis to None (ensures it's always defined)
                    pending_analysis = None
                    
                    if answer and answer.strip():
                        add_to_conversation_timeline(conversation_timeline, 'technical_qa', "", answer, current_question_no, room_id, process_id)
                        
                        # ═══════════════════════════════════════════════════════════════════════
                        # Prepare answer analysis data (trigger deferred until intent is known)
                        # ═══════════════════════════════════════════════════════════════════════
                        if 2 <= current_question_no <= config.questions_per_round + 1 and not answer.startswith("BOT NOTE:"):
                            try:
                                # Get current question format from tracker
                                question_format = client.format_tracker._question_formats.get(current_question_no) if client.format_tracker else None
                                question_type = map_question_format_to_type(question_format) if question_format else "implementation"
                                
                                # Build session context from existing data
                                session_context = {
                                    "questions": [r.get('question', '') for r in responses],
                                    "answers": [r.get('answer', '') for r in responses],
                                    "timings": [r.get('duration', 0) for r in responses],
                                    "conversation_timeline": conversation_timeline[-10:] if len(conversation_timeline) > 10 else conversation_timeline
                                }
                                
                                # Store for deferred trigger (after we know intent)
                                pending_analysis = {
                                    "session_id": f"interview_{interview_id}",
                                    "question_id": f"Q{current_question_no}",
                                    "question_text": question_to_ask,
                                    "question_type": question_type,
                                    "answer_text": answer,
                                    "answer_duration_sec": answer_duration,
                                    "resume_text": resume,
                                    "session_context": session_context
                                }
                            except Exception as e:
                                logger.error(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"Analysis prep failed (non-critical): {e}"
                                )
                    # # COMMENTED OUT: Queue score calculation for technical questions (Q3-Q15)
                    # if answer and answer.strip() and not answer.startswith("BOT NOTE:") and 2 <= current_question_no <= 15:
                    #     thread_manager.score_queue.put((current_question_no, question_to_ask, answer, job_description, mandatory_skills, resume))
                    
                    last_answer = answer
                    logger.info(
                        f"🎤 [process id: {process_id}][room id: {room_id}] "
                        f"USER TRANSCRIPT Q{current_question_no}: {answer}"
                    )
                    
                    # Handle no answer
                    if not answer or not answer.strip():
                        # Build analysis params for triggering analysis after audio resume
                        question_format = client.format_tracker._question_formats.get(current_question_no) if client.format_tracker else None
                        question_type = map_question_format_to_type(question_format) if question_format else "technical"
                        analysis_params = {
                            "interview_id": interview_id,
                            "question_text": question_to_ask,
                            "question_type": question_type,
                            "resume": resume,
                            "session_context": {
                                "questions": [r.get('question', '') for r in responses],
                                "answers": [r.get('answer', '') for r in responses],
                                "timings": [r.get('duration', 0) for r in responses],
                                "conversation_timeline": conversation_timeline[-10:] if len(conversation_timeline) > 10 else conversation_timeline
                            }
                        } if 2 <= current_question_no <= config.questions_per_round + 2 else None
                        
                        reflex_context = {
                            "engine": reflex_engine,
                            "question_to_ask": question_to_ask,
                            "jd_summary": job_description,
                            "mandatory_skills": mandatory_skills,
                            "recent_acks": recent_acks
                        }
                        
                        answer, repeat_qtn, candidate_exit, silence_time, bot_note_reason, spec_res_retry, spec_hit_retry, speech_end_retry = handle_no_answer_block(
                            candidate_name, driver, room_id, process_id, current_question_no, conversation_timeline,
                            analysis_params=analysis_params,
                            reflex_context=reflex_context,
                            interview_id=interview_id
                        )
                        
                        # If retry captured a speculative result, use it!
                        if spec_res_retry:
                            speculative_reflex_result = spec_res_retry
                            speculative_hit = spec_hit_retry
                            speech_end_time = speech_end_retry
                        if bot_note_reason != "Others":
                            if bot_note_reason == "MUTE":
                                if silence_time > 0:
                                    msg = f'The candidate remained on mute for approximately {silence_time} seconds during question {current_question_no}.'
                                    audio_video_issues.append(msg)
                                    # Store mute event in session for authorship analysis
                                    try:
                                        append_mute_event(
                                            session_id=f"interview_{interview_id}",
                                            question_number=current_question_no,
                                            silence_duration_sec=silence_time,
                                            event_type="MUTE"
                                        )
                                        logger.info(f"[process id: {process_id}][room id: {room_id}] Stored mute event for Q{current_question_no}")
                                    except Exception as e:
                                        logger.warning(f"[process id: {process_id}][room id: {room_id}] Failed to store mute event: {e}")
                            elif bot_note_reason == "EXIT":
                                if silence_time > 0:
                                    msg = f'The candidate exited or got disconnected for about {silence_time} seconds during question {current_question_no}.'
                                    audio_video_issues.append(msg)
                                    # Store exit event in session for authorship analysis
                                    try:
                                        append_mute_event(
                                            session_id=f"interview_{interview_id}",
                                            question_number=current_question_no,
                                            silence_duration_sec=silence_time,
                                            event_type="EXIT"
                                        )
                                        logger.info(f"[process id: {process_id}][room id: {room_id}] Stored exit event for Q{current_question_no}")
                                    except Exception as e:
                                        logger.warning(f"[process id: {process_id}][room id: {room_id}] Failed to store exit event: {e}")
                        if answer.startswith("BOT NOTE: candidate exited at") or answer.startswith("BOT NOTE: no answer recorded as candidate is muted."):
                            responses = update_responses(responses, question_to_ask, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                            candidate_exit = True
                            break
                    
                    # ═══════════════════════════════════════════════════════════════════════
                    # ✅ SPLIT-BRAIN ARCHITECTURE: Reflex (fast) + Planner (parallel)
                    # Stage 1: Reflex → intent + ack (~1s)
                    # Stage 2: Planner → next_question (runs in background while ack speaks)
                    # ═══════════════════════════════════════════════════════════════════════
                    start = time.time()
                    curr_question = question_to_ask
                    
                    try:
                        # ── Initialize streaming flag (Q1 always uses legacy path) ──
                        use_streaming_ack = False
                        
                        # ───────────────────────────────────────────────────────────────────
                        # Q1: Dedicated greeting reflex (separate LLM prompt)
                        # Understands conversational answers, repeat requests, and questions
                        # ───────────────────────────────────────────────────────────────────
                        if current_question_no == 1:
                            transcript_ready_time = time.time()
                            
                            # Grab warmup response_id so planner uses stateful mode
                            if warmup_result["response_id"]:
                                previous_response_id = warmup_result["response_id"]
                                logger.info(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"\u2705 Using warmup response_id: {previous_response_id}"
                                )
                            else:
                                logger.warning(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"\u26a0\ufe0f Warmup didn't complete in time, will use stateless fallback"
                                )
                            
                            # Add Q1 greeting as assistant message for conversation continuity
                            messages.append({"role": "assistant", "content": question_to_ask})
                            
                            # Use speculative reflex result if available (preserves _prefetched_tts)
                            if speculative_reflex_result:
                                reflex_result = speculative_reflex_result
                                intent = reflex_result.get("intent", "others")
                                ack = reflex_result.get("ack", "")
                                logger.info(
                                    f"{'✅' if speculative_hit else '⚠️'} [process id: {process_id}][room id: {room_id}] "
                                    f"Q1 SPECULATIVE {'HIT' if speculative_hit else 'MISS (used fallback)'} | intent={intent}"
                                )
                            else:
                                # No speculative result — fire synchronous Q1 reflex
                                reflex_start = time.time()
                                reflex_result = reflex_engine.generate_q1_reflex(
                                    transcript=answer,
                                    current_question=question_to_ask,
                                    jd_summary=job_description,
                                    round_number=context.current_round,
                                    room_id=room_id,
                                    process_id=process_id
                                )
                                intent = reflex_result.get("intent", "others")
                                ack = reflex_result.get("ack", "")
                                reflex_latency = time.time() - reflex_start
                                logger.info(
                                    f"\U0001f44b [process id: {process_id}][room id: {room_id}] "
                                    f"Q1 REFLEX (sync): intent={intent} | {reflex_latency*1000:.0f}ms"
                                )
                            
                            # ⭐ CRITICAL: Q1 greeting must NEVER be repeated.
                            # Force all non-exit intents to "others" so we always flow
                            # into the "how's your day" path and then advance to Q2.
                            if intent not in ("others", "exit"):
                                logger.info(
                                    f"🔧 [process id: {process_id}][room id: {room_id}] "
                                    f"Q1 SAFETY: Normalizing intent='{intent}' → 'others' "
                                    f"(greeting must never be repeated)"
                                )
                                intent = "others"
                            
                            logger.info(
                                f"\U0001f44b [process id: {process_id}][room id: {room_id}] "
                                f"Q1 REFLEX: intent={intent}"
                            )
                        else:
                            # ───────────────────────────────────────────────────────────────
                            # Q2+: SPLIT REFLEX ARCHITECTURE (Intent Engine + ACK Engine)
                            # Intent: fast classification (~500ms) — fires first
                            # ACK: streaming text → sentence TTS → WebRTC playback
                            # ───────────────────────────────────────────────────────────────
                            transcript_ready_time = time.time()
                            reflex_start = time.time()
                            
                            # ── INTENT: Use speculative if available, else fire intent engine ──
                            use_streaming_ack = False  # Flag: whether to use streaming ack pipeline
                            
                            if speculative_reflex_result:
                                # Speculative result available — use it (includes both intent + ack)
                                reflex_result = speculative_reflex_result
                                intent = reflex_result.get("intent", "others")
                                ack = reflex_result.get("ack", "")
                                logger.info(
                                    f"{'✅' if speculative_hit else '⚠️'} [process id: {process_id}][room id: {room_id}] "
                                    f"SPECULATIVE {'HIT' if speculative_hit else 'MISS (used fallback)'} | intent={intent}"
                                )
                            elif USE_CARTESIA:
                                # LIVE ACK (Cartesia): stream GPT tokens straight into
                                # Cartesia via the downstream streaming pipeline instead of
                                # building the whole ack text first and speaking it full.
                                # Previously every ack ran the non-spec branch below, which
                                # collected the full text (~1s+ before any audio) then spoke
                                # it whole. Routing through use_streaming_ack lets the
                                # candidate hear the ack ~300ms after the first GPT token.
                                try:
                                    # Task 2: nano intent+quality (sync fallback when the
                                    # speculative classifier didn't fire). quality drives
                                    # cached-ack bucket selection at the ack chokepoint below.
                                    _io = reflex_engine.generate_intent_quality(
                                        transcript=answer,
                                        current_question=question_to_ask,
                                        question_number=current_question_no,
                                        room_id=room_id,
                                        process_id=process_id,
                                    )
                                    intent = _io.get("intent", "others")
                                except Exception:
                                    intent = "others"
                                    _io = {"intent": "others"}
                                ack = ""  # produced + spoken by the streaming pipeline below
                                reflex_result = {"intent": intent, "ack": "",
                                                 "quality": _io.get("quality"),
                                                 "confidence": _io.get("confidence")}
                                use_streaming_ack = True
                                logger.info(
                                    f"🚀 [process id: {process_id}][room id: {room_id}] "
                                    f"LIVE ACK → streaming pipeline (Cartesia) | intent={intent}"
                                )
                            else:
                                # No speculative result — fire PARALLEL intent + ack stream + TTS
                                # Same architecture as the speculative path
                                import re as _re_ns
                                from concurrent.futures import ThreadPoolExecutor as _NSPool
                                
                                ns_start = time.time()
                                
                                with _NSPool(max_workers=2) as ns_pool:
                                    # Fire intent classification (fast, ~300ms)
                                    intent_future = ns_pool.submit(
                                        reflex_engine.generate_intent_only,
                                        transcript=answer,
                                        current_question=question_to_ask,
                                        question_number=current_question_no,
                                        room_id=room_id,
                                        process_id=process_id
                                    )
                                    
                                    # Stream ack tokens and detect first chunk for TTS
                                    skills_list = mandatory_skills.split(", ") if isinstance(mandatory_skills, str) else mandatory_skills
                                    ack_buffer = ""
                                    first_chunk = None
                                    first_chunk_tts_future = None
                                    first_token_received = False
                                    
                                    try:
                                        for token in reflex_engine.generate_ack_stream(
                                            transcript=answer,
                                            current_question=question_to_ask,
                                            intent="others",  # Speculative — most common
                                            jd_summary=job_description,
                                            mandatory_skills=skills_list,
                                            question_number=current_question_no,
                                            recent_acks=recent_acks,
                                            room_id=room_id,
                                            process_id=process_id
                                        ):
                                            if not first_token_received:
                                                first_token_received = True
                                                ttft = (time.time() - ns_start) * 1000
                                                logger.info(
                                                    f"💨 [process id: {process_id}][room id: {room_id}] "
                                                    f"NON-SPEC STREAM TTFT: {ttft:.0f}ms"
                                                )
                                            
                                            ack_buffer += token
                                            
                                            # Detect first natural break for TTS
                                            if first_chunk is None and len(ack_buffer) >= 15:
                                                search_end = min(len(ack_buffer), 50)
                                                search_region = ack_buffer[:search_end]
                                                for pat in [r'[!?.]', r'—', r',']:
                                                    matches = list(_re_ns.finditer(pat, search_region))
                                                    if matches:
                                                        split_pos = matches[-1].end()
                                                        if split_pos >= 15:
                                                            first_chunk = ack_buffer[:split_pos].strip()
                                                            # Fire TTS immediately — skip if Cartesia active
                                                            if USE_CARTESIA:
                                                                logger.info(
                                                                    f"⚡ [process id: {process_id}][room id: {room_id}] "
                                                                    f"NON-SPEC: Cartesia active — skipping OpenAI TTS prefetch for first chunk"
                                                                )
                                                                first_chunk_tts_future = None
                                                            else:
                                                                first_chunk_tts_future = ns_pool.submit(
                                                                    lambda t=first_chunk: openai_client.audio.speech.create(
                                                                        model="tts-1", voice="nova",
                                                                        input=t, response_format="mp3", speed=1
                                                                    ).content
                                                                )
                                                                tts_fire_ms = (time.time() - ns_start) * 1000
                                                                logger.info(
                                                                    f"🔊 [process id: {process_id}][room id: {room_id}] "
                                                                    f"NON-SPEC TTS FIRED: '{first_chunk}' "
                                                                    f"({len(first_chunk)} chars) at {tts_fire_ms:.0f}ms"
                                                                )
                                                            break
                                    except Exception as e:
                                        logger.warning(
                                            f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                            f"Non-spec ack stream failed: {e}"
                                        )
                                        if not ack_buffer:
                                            ack_buffer = "Hmm, okay — I appreciate you sharing that."
                                    
                                    # If no chunk was extracted during streaming, use full buffer
                                    if first_chunk is None and ack_buffer:
                                        first_chunk = ack_buffer.strip()
                                        if not USE_CARTESIA:
                                            first_chunk_tts_future = ns_pool.submit(
                                                lambda t=first_chunk: openai_client.audio.speech.create(
                                                    model="tts-1", voice="nova",
                                                    input=t, response_format="mp3", speed=1
                                                ).content
                                            )
                                        # If Cartesia: leave first_chunk_tts_future=None,
                                        # ACK will be spoken via Cartesia at speak-time (~300ms TTFA)
                                    
                                    remaining_text = ""
                                    if first_chunk and len(ack_buffer) > len(first_chunk):
                                        remaining_text = ack_buffer[len(first_chunk):].strip()
                                    
                                    # Get intent (should be done by now)
                                    try:
                                        intent_result = intent_future.result(timeout=3.0)
                                        intent = intent_result.get("intent", "others")
                                    except Exception:
                                        intent = "others"
                                    
                                    # Get first chunk TTS
                                    prefetched_audio = None
                                    if first_chunk_tts_future:
                                        try:
                                            prefetched_audio = first_chunk_tts_future.result(timeout=3.0)
                                        except Exception as e:
                                            logger.warning(
                                                f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                                f"Non-spec TTS failed: {e}"
                                            )
                                
                                ack = ack_buffer.strip()
                                if ack.endswith("?"):
                                    ack = ack[:-1] + "."
                                
                                # Build reflex_result with prefetched TTS
                                reflex_result = {"intent": intent, "ack": ack}
                                if prefetched_audio:
                                    reflex_result["_prefetched_tts"] = prefetched_audio
                                    reflex_result["_prefetch_remaining_text"] = remaining_text
                                
                                total_ms = (time.time() - ns_start) * 1000
                                logger.info(
                                    f"⚡ [process id: {process_id}][room id: {room_id}] "
                                    f"NON-SPEC PARALLEL DONE: {total_ms:.0f}ms | intent={intent} | "
                                    f"ack='{ack[:60]}...' | "
                                    f"tts={'READY' if prefetched_audio else 'NONE'} | "
                                    f"remaining={len(remaining_text)} chars"
                                )
                                
                                # Disable old streaming ack path — we already have the ack + TTS
                                use_streaming_ack = False
                            
                            # ─── FOLLOW-UP REFLEX (pre-computed in parallel during recording) ───
                            followup_reflex_result = speculative_followup_result or {"followup_worthy": False, "ack": "", "followup_topic": ""}
                            fire_followup = (
                                current_question_no >= 3 and 
                                followup_chain_count < max_followups_for_question and
                                intent.upper() not in ('REPEAT', 'SKIP', 'EXIT', 'REPHRASE', 'AUDIO', 'VIDEO')
                            )
                            
                            if fire_followup and followup_reflex_result.get("followup_worthy"):
                                logger.info(
                                    f"🔄 [process id: {process_id}][room id: {room_id}] "
                                    f"FOLLOWUP REFLEX (speculative): worthy=True | "
                                    f"topic='{followup_reflex_result.get('followup_topic', '')[:60]}'"
                                )
                            
                            # ─── PRIORITY LOGIC: Decide main vs follow-up ───
                            use_followup = False
                            if intent.upper() in ('REPEAT', 'SKIP', 'EXIT', 'REPHRASE', 'AUDIO', 'VIDEO'):
                                use_followup = False
                            elif followup_reflex_result.get("followup_worthy", False) and fire_followup:
                                # Check coverage: can we afford a follow-up?
                                remaining_skills = len(client.skill_tracker.uncovered) if client.skill_tracker else 0
                                remaining_qs = config.questions_per_round - current_question_no
                                
                                if remaining_skills <= remaining_qs:
                                    # 70/30 weighted coin flip: 70% chance to actually follow up
                                    coin_flip = random.choices([True, False], weights=[80, 20], k=1)[0]
                                    if coin_flip:
                                        use_followup = True
                                        ack = followup_reflex_result["ack"]
                                        use_streaming_ack = False  # Follow-up ack is pre-computed, not streamed
                                        logger.info(
                                            f"🔄 [process id: {process_id}][room id: {room_id}] "
                                            f"FOLLOWUP ACTIVATED Q{current_question_no}: chain={followup_chain_count + 1}/{max_followups_for_question} | "
                                            f"remaining_skills={remaining_skills} remaining_qs={remaining_qs} | coin=YES"
                                        )
                                    else:
                                        logger.info(
                                            f"🎲 [process id: {process_id}][room id: {room_id}] "
                                            f"FOLLOWUP SKIPPED by coin flip (30% chance) Q{current_question_no}"
                                        )
                                else:
                                    logger.info(
                                        f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                        f"FOLLOWUP SUPPRESSED: remaining_skills={remaining_skills} > remaining_qs={remaining_qs}"
                                    )
                            
                            # Calculate TRUE perceived latency from speech end
                            perceived_latency = (time.time() - speech_end_time) if speech_end_time else (time.time() - reflex_start)
                            
                            logger.info(
                                f"⚡ [process id: {process_id}][room id: {room_id}] "
                                f"PERCEIVED LATENCY: {perceived_latency*1000:.0f}ms | intent={intent} "
                                f"| speculative={'HIT' if speculative_hit else 'HIT' if speculative_reflex_result else 'MISS'}"
                                f"| followup={'YES' if use_followup else 'NO'}"
                                f"| streaming_ack={'YES' if use_streaming_ack else 'NO'}"
                            )
                        
                        # Clean ack
                        # ack = remove_question_from_ack(ack, question_to_ask)
                        
                        # Track ack for no-repeat (keep last 3)
                        # NOTE: For streaming ack, this is tracked after streaming completes
                        if ack and ack.strip():
                            recent_acks.append(ack)
                            if len(recent_acks) > 3:
                                recent_acks.pop(0)
                        
                        # ─── Task 2: CACHED ACK (play a pre-rendered ack from memory) ───
                        # For answered / skip / repeat / rephrase turns (Q2+), play a warm
                        # pre-rendered ack instead of generating + synthesizing one live —
                        # zero GPT (no ack generation), zero Cartesia (no live synth). EXIT
                        # keeps its own confirm flow; a speculative prefetched ack (already
                        # paid for) or a cache miss falls through to the live ack path below.
                        cached_ack_played = False
                        if (USE_CACHED_ACKS and current_question_no != 1
                                and intent.upper() in ('OTHERS', 'SKIP', 'REPEAT', 'REPHRASE')
                                and not reflex_result.get("_prefetched_tts")):
                            _is_last_q = (current_question_no == config.questions_per_round)
                            _bucket = bucket_for(intent, reflex_result.get("quality"),
                                                 reflex_result.get("confidence"), is_last=_is_last_q)
                            _clip = play_cached_ack(_bucket, recent_ack_ids, room_id, process_id)
                            if _clip:
                                cached_ack_played = True
                                recent_ack_ids.append(_clip)
                                if len(recent_ack_ids) > 5:
                                    recent_ack_ids.pop(0)
                                ack_spoken_time = time.time()
                                _capture_face_after_ack()   # candidate attentive → grab best face
                                add_to_conversation_timeline(
                                    conversation_timeline, 'technical_qa', "[cached ack]", "",
                                    current_question_no, room_id, process_id)
                                ack = ""              # skip the live pre-computed ack branch
                                use_streaming_ack = False  # skip the live streaming ack branch

                        # ─── ACK TTS: Streaming pipeline OR prefetch (based on split architecture) ───
                        ack_tts_future = None
                        repeat_question_tts_future = None
                        streaming_ack_text = None  # Will hold the full ack text after streaming
                        
                        if use_streaming_ack and intent.upper() not in ['AUDIO', 'VIDEO', 'EXIT']:
                            # ═══════════════════════════════════════════════════════════════
                            # 🚀 STREAMING ACK PIPELINE: GPT stream → sentence TTS → WebRTC
                            # No prefetch needed — audio plays AS sentences arrive
                            # ═══════════════════════════════════════════════════════════════
                            logger.info(
                                f"🚀 [process id: {process_id}][room id: {room_id}] "
                                f"STREAMING ACK PIPELINE: Starting GPT→TTS→WebRTC for intent={intent}"
                            )
                            
                            # For REPEAT/REPHRASE: prefetch question TTS in parallel with streaming ack
                            if intent.upper() in ('REPEAT', 'REPHRASE') and question_to_ask and question_to_ask.strip():
                                repeat_question_tts_future = prefetch_tts(
                                    question_to_ask, room_id, process_id, tts_executor
                                )
                                logger.info(
                                    f"🔊 [process id: {process_id}][room id: {room_id}] "
                                    f"REPEAT/REPHRASE QUESTION TTS PREFETCH FIRED (parallel with streaming ack)"
                                )
                            
                            # Create the streaming ack generator
                            ack_stream_gen = reflex_engine.generate_ack_stream(
                                transcript=answer,
                                current_question=question_to_ask,
                                intent=intent,
                                jd_summary=job_description,
                                mandatory_skills=mandatory_skills,
                                question_number=current_question_no,
                                recent_acks=recent_acks,
                                room_id=room_id,
                                process_id=process_id
                            )
                            
                            # Stream ACK: GPT text → sentence TTS → WebRTC playback
                            streaming_ack_text = ai_speak_streaming(
                                ack_stream_gen, driver, room_id, process_id
                            )
                            
                            # Update ack variable with what was actually spoken
                            if streaming_ack_text:
                                ack = streaming_ack_text
                                # Track streamed ack for no-repeat
                                recent_acks.append(ack)
                                if len(recent_acks) > 3:
                                    recent_acks.pop(0)
                            
                            ack_spoken_time = time.time()
                            _capture_face_after_ack()   # candidate attentive → grab best face
                            if ack and ack.strip():
                                add_to_conversation_timeline(conversation_timeline, 'technical_qa', ack, "", current_question_no, room_id, process_id)
                            ack = ""  # Clear ack to prevent duplicate speaking at interview end
                            
                        elif ack and ack.strip() and intent.upper() not in ['AUDIO', 'VIDEO', 'EXIT']:
                            # ═══════════════════════════════════════════════════════════════
                            # SPECULATIVE/FOLLOWUP PATH: Pre-computed ack text available
                            # No ack TTS prefetch needed — ai_speak_streamed_text handles
                            # sentence-chunked TTS at speak time (~1s to first audio)
                            # ═══════════════════════════════════════════════════════════════
                            logger.info(
                                f"🔊 [process id: {process_id}][room id: {room_id}] "
                                f"PRE-COMPUTED ACK ready (will use sentence-streamed TTS at speak time)"
                            )
                            
                            # For REPEAT/REPHRASE: prefetch question TTS in parallel
                            if intent.upper() in ('REPEAT', 'REPHRASE') and question_to_ask and question_to_ask.strip():
                                repeat_question_tts_future = prefetch_tts(
                                    question_to_ask, room_id, process_id, tts_executor
                                )
                                logger.info(
                                    f"🔊 [process id: {process_id}][room id: {room_id}] "
                                    f"REPEAT/REPHRASE QUESTION TTS PREFETCH FIRED"
                                )

                        
                        # ───────────────────────────────────────────────────────────────────
                        # STAGE 2: PLANNER (slow lane, runs in background)
                        # Generate next_question while ack is being spoken
                        # ───────────────────────────────────────────────────────────────────
                        
                        # Phase 4 Update: Record full question logic to eliminate duplicates
                        if current_question_no >= 1 and client.skill_tracker:
                            # 1. Provide the candidate's answer to the PREVIOUS question
                            client.skill_tracker.record_answer(current_question_no, answer)
                            
                            # 2. Register this newly planned question
                            # We get the planned skills from the tracker
                            curr_skills = client._next_skills if hasattr(client, '_next_skills') and client._next_skills else []
                            client.skill_tracker.register_question(
                                current_question_no + 1, 
                                curr_skills, 
                                question_text="" # Text gets registered when answer arrives for Q+1 or below
                            )
                        planner_future = None
                        followup_planner_future = None
                        chained_prefetch = None
                        needs_planner = intent.upper() in ('OTHERS', 'SKIP')
                        
                        if use_followup:
                            # ─── FOLLOW-UP PLANNER (separate from main planner) ───
                            followup_chain_count += 1
                            followup_planner_future = planner_executor.submit(
                                client.create_followup_planner_response,
                                question_number=current_question_no,
                                candidate_answer=answer,
                                previous_question=question_to_ask,
                                followup_topic=followup_reflex_result.get("followup_topic", ""),
                                skills=client._next_skills if hasattr(client, '_next_skills') and client._next_skills else [],
                                room_id=room_id,
                                process_id=process_id,
                                context=context,
                            )
                            logger.info(
                                f"🔄 [process id: {process_id}][room id: {room_id}] "
                                f"FOLLOWUP PLANNER launched (chain={followup_chain_count}/2)"
                            )
                            # 🔗 Chain TTS prefetch — fires as soon as followup planner resolves
                            chained_prefetch = chain_prefetch_to_planner(
                                followup_planner_future, room_id, process_id, tts_executor
                            )
                        elif needs_planner and previous_response_id:
                            # ─── ORIGINAL PLANNER (new topic, new skills) ───
                            followup_chain_count = 0  # Reset chain on new topic
                            max_followups_for_question = random.choices([0, 1, 2, 3], weights=[15, 35, 40, 10], k=1)[0]
                            logger.info(f"🎲 [process id: {process_id}][room id: {room_id}] FOLLOWUP BUDGET for next Q: max={max_followups_for_question}")
                            planner_future = planner_executor.submit(
                                client.create_planner_response,
                                question_number=current_question_no,
                                user_message=answer,
                                intent=intent,
                                previous_response_id=previous_response_id,
                                current_question_text=question_to_ask,
                                resume_text=resume,
                                previous_intent=previous_intent,
                                room_id=room_id,
                                process_id=process_id,
                                context=context
                            )
                            logger.info(
                                f"🧠 [process id: {process_id}][room id: {room_id}] "
                                f"PLANNER launched in background thread"
                            )
                            # 🔗 Chain TTS prefetch — fires as soon as planner resolves
                            chained_prefetch = chain_prefetch_to_planner(
                                planner_future, room_id, process_id, tts_executor
                            )
                        elif needs_planner and not previous_response_id:
                            # Q1: Launch stateless fallback in background (messages-based)
                            messages.append({"role": "user", "content": answer})
                            
                            def _stateless_fallback(msgs, q_num, cli, r_id, p_id):
                                try:
                                    ai_resp, metrics, resp_id = cli.create_response(
                                        question_number=q_num,
                                        messages=msgs,
                                        use_stateful=False,
                                        context=context
                                    )
                                    # Strip markdown code fences if present
                                    if ai_resp.strip().startswith("```"):
                                        ai_resp = re.sub(r'^```(?:json)?\s*', '', ai_resp.strip())
                                        ai_resp = re.sub(r'```\s*$', '', ai_resp).strip()
                                    ai_resp = json.loads(ai_resp)
                                    return {
                                        "next_question": ai_resp["next_question"],
                                        "question_no": ai_resp["question_no"],
                                        "ai_response": ai_resp,
                                        "response_id": resp_id,
                                        "metrics": metrics
                                    }
                                except Exception as e:
                                    logger.error(f"[process id: {p_id}][room id: {r_id}] Stateless fallback failed: {e}")
                                    return None
                            
                            planner_future = planner_executor.submit(
                                _stateless_fallback,
                                messages.copy(), current_question_no, client, room_id, process_id
                            )
                            logger.info(
                                f"🧠 [process id: {process_id}][room id: {room_id}] "
                                f"STATELESS FALLBACK launched in background (Q1, no previous_response_id)"
                            )
                            # 🔗 Chain TTS prefetch — fires as soon as stateless fallback resolves
                            chained_prefetch = chain_prefetch_to_planner(
                                planner_future, room_id, process_id, tts_executor
                            )
                        
                        # ── ASYNC SKILL REJECTION DETECTION (runs entirely in background) ──
                        if answer and answer.strip() and client.skill_tracker and intent.upper() not in ['EXIT', 'AUDIO', 'VIDEO']:
                            def _detect_and_reject(t=answer, s=client.skill_tracker, r=room_id, p=process_id):
                                try:
                                    rejected = detect_rejected_skills(
                                        transcript=t,
                                        mandatory_skills=s.mandatory_skills,
                                        room_id=r,
                                        process_id=p
                                    )
                                    if rejected:
                                        for skill in rejected:
                                            s.mark_skipped([skill])
                                        logger.info(
                                            f"🚫 [process id: {p}][room id: {r}] "
                                            f"BLACKLISTED rejected skills: {rejected}"
                                        )
                                except Exception as e:
                                    logger.warning(
                                        f"⚠️ [process id: {p}][room id: {r}] "
                                        f"Skill rejection future failed: {e}"
                                    )
                            
                            planner_executor.submit(_detect_and_reject)
                        
                        # ───────────────────────────────────────────────────────────────────
                        # SPEAK ACK (speculative/followup path — sentence-chunked streaming TTS)
                        # If use_streaming_ack was True, ack was already spoken and cleared above.
                        # For pre-computed acks, we use ai_speak_streamed_text() which splits
                        # into sentences and fires TTS per sentence (~1s to first audio).
                        # ───────────────────────────────────────────────────────────────────
                        if ack and ack.strip() and intent.upper() not in ['AUDIO', 'VIDEO', 'EXIT']:
                            audio_to_ack = time.time() - transcript_ready_time
                            logger.info(
                                f"🕐 [process id: {process_id}][room id: {room_id}] "
                                f"AUDIO→ACK: {audio_to_ack:.2f}s (transcript ready → ack sent to speak) [SENTENCE-STREAMED]"
                            )
                            
                            # Cancel the old prefetch TTS future — no longer needed
                            # since ai_speak_streamed_text handles TTS per sentence
                            if ack_tts_future:
                                ack_tts_future.cancel()
                                ack_tts_future = None
                            
                            # ── BARGE-IN ACK LOOP ──
                            # Play ack with barge-in enabled. If candidate interrupts:
                            #   1. Capture their speech
                            #   2. Accumulate with previous answer
                            #   3. Re-fire reflex with full answer → get FRESH ack
                            #   4. Speak new ack (with barge-in again)
                            # Max 3 cycles to prevent infinite loops.
                            MAX_BARGEIN_CYCLES = 3
                            bargein_cycle = 0
                            accumulated_answer = answer_dict.get("answer", "").strip() if answer_dict else ""
                            current_ack = ack
                            
                            # Check for prefetched TTS audio from streaming pipeline
                            prefetched_audio = None
                            if reflex_result and isinstance(reflex_result, dict):
                                # TTS audio is directly attached (generated during streaming)
                                prefetched_audio = reflex_result.pop("_prefetched_tts", None)
                                
                                if prefetched_audio:
                                    logger.info(
                                        f"⚡ [process id: {process_id}][room id: {room_id}] "
                                        f"USING STREAMING TTS ({len(prefetched_audio)} bytes) — instant play!"
                                    )
                            # Extract remaining text to speak after the prefetched first chunk
                            prefetch_remaining_text = ""
                            if prefetched_audio and reflex_result and isinstance(reflex_result, dict):
                                prefetch_remaining_text = reflex_result.pop("_prefetch_remaining_text", "")
                            
                            while bargein_cycle < MAX_BARGEIN_CYCLES:
                                # First cycle: use prefetched first-chunk TTS if available
                                if prefetched_audio and bargein_cycle == 0:
                                    # Play first chunk immediately (0ms TTS wait)
                                    bargein_result = play_ack_with_bargein(
                                        room_id, process_id, driver, prefetched_audio
                                    )
                                    if bargein_result and bargein_result.get("interrupted"):
                                        speak_result = {"interrupted": True, "bargein_transcript": bargein_result.get("transcript", "")}
                                    else:
                                        # First chunk played fine — now play remaining text
                                        if prefetch_remaining_text and prefetch_remaining_text.strip():
                                            logger.info(
                                                f"🔊 [process id: {process_id}][room id: {room_id}] "
                                                f"Playing remaining text ({len(prefetch_remaining_text)} chars) after prefetched chunk"
                                            )
                                            remaining_result = ai_speak_streamed_text(
                                                prefetch_remaining_text, driver, room_id, process_id,
                                                allow_bargein=True
                                            )
                                            if isinstance(remaining_result, dict) and remaining_result.get("interrupted"):
                                                speak_result = remaining_result
                                            else:
                                                speak_result = current_ack  # Full ack played
                                        else:
                                            speak_result = current_ack  # Single sentence ack, done
                                else:
                                    speak_result = ai_speak_streamed_text(
                                        current_ack, driver, room_id, process_id,
                                        allow_bargein=True
                                    )
                                
                                # Check if candidate interrupted
                                if isinstance(speak_result, dict) and speak_result.get("interrupted"):
                                    bargein_cycle += 1
                                    bargein_transcript = speak_result.get("bargein_transcript", "").strip()
                                    
                                    logger.info(
                                        f"🛑 [process id: {process_id}][room id: {room_id}] "
                                        f"BARGEIN CYCLE #{bargein_cycle}: candidate said '{bargein_transcript[:80]}...'"
                                    )
                                    
                                    if bargein_transcript:
                                        # Accumulate answer
                                        accumulated_answer = (accumulated_answer + " " + bargein_transcript).strip()
                                        # Update answer_dict so downstream has the full answer
                                        answer_dict["answer"] = accumulated_answer
                                        
                                        logger.info(
                                            f"📝 [process id: {process_id}][room id: {room_id}] "
                                            f"ACCUMULATED ANSWER ({len(accumulated_answer)} chars): "
                                            f"'{accumulated_answer[:100]}...'"
                                        )
                                        
                                        # Re-fire reflex with full accumulated answer → FRESH ack
                                        try:
                                            if current_question_no == 1:
                                                new_reflex = reflex_engine.generate_q1_reflex(
                                                    transcript=accumulated_answer,
                                                    current_question=question_to_ask,
                                                    jd_summary=job_description,
                                                    round_number=1,
                                                    room_id=room_id,
                                                    process_id=process_id
                                                )
                                            elif current_question_no == 0:
                                                new_reflex = reflex_engine.generate_greeting_reflex(
                                                    transcript=accumulated_answer,
                                                    room_id=room_id,
                                                    process_id=process_id
                                                )
                                            else:
                                                new_reflex = reflex_engine.generate_reflex(
                                                    transcript=accumulated_answer,
                                                    current_question=question_to_ask,
                                                    jd_summary=job_description,
                                                    mandatory_skills=mandatory_skills,
                                                    question_number=current_question_no,
                                                    recent_acks=recent_acks,
                                                    spoken_filler=None,
                                                    room_id=room_id,
                                                    process_id=process_id
                                                )
                                            
                                            current_ack = new_reflex.get("ack", "").strip() if new_reflex else ""
                                            new_intent = new_reflex.get("intent", intent) if new_reflex else intent
                                            
                                            logger.info(
                                                f"🔄 [process id: {process_id}][room id: {room_id}] "
                                                f"BARGEIN NEW ACK: intent={new_intent} | ack='{current_ack[:60]}...'"
                                            )
                                            
                                            # Update intent if it changed
                                            intent = new_intent
                                            
                                            if not current_ack:
                                                logger.info(
                                                    f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                                    f"Empty ack after bargein — breaking loop"
                                                )
                                                break
                                            
                                            # Loop back → speak new ack with barge-in
                                            continue
                                            
                                        except Exception as reflex_err:
                                            logger.error(
                                                f"❌ [process id: {process_id}][room id: {room_id}] "
                                                f"Bargein reflex failed: {reflex_err}"
                                            )
                                            break
                                    else:
                                        # Interrupted but no transcript captured — just break
                                        logger.info(
                                            f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                            f"Bargein detected but empty transcript — breaking loop"
                                        )
                                        break
                                else:
                                    # No interruption — ack finished normally
                                    break
                            
                            ack_spoken_time = time.time()
                            _capture_face_after_ack()   # candidate attentive → grab best face
                            add_to_conversation_timeline(conversation_timeline, 'technical_qa', current_ack or ack, "", current_question_no, room_id, process_id)
                            ack = ""  # Clear ack to prevent duplicate speaking at interview end
                            
                            # ✨ NEW: Wait for Candidate's response to "How has your day been?" ✨
                            # Since Q1 "others" ack ALWAYS ends with asking how their day is, we must let them answer.
                            if current_question_no == 1 and intent == "others":
                                logger.info(f"🎤 [process id: {process_id}][room id: {room_id}] Waiting for candidate to answer 'how has your day been?'")
                                
                                # Use current_question_no=0 to force standard reflex, not Q1 instructions reflex
                                day_response_dict, day_reflex_result, _, _, _, _ = _capture_with_speculative_reflex(
                                    driver, room_id, process_id,
                                    reflex_engine, ack, job_description,
                                    mandatory_skills, 0, recent_acks,
                                    interview_id=interview_id
                                )
                                day_response = day_response_dict.get("answer", "") if day_response_dict else ""
                                
                                warmup_q_preselected = None
                                warmup_q_tts = None
                                
                                if day_response:
                                    logger.info(f"🎤 [process id: {process_id}][room id: {room_id}] Candidate day response: '{day_response}'")
                                    add_to_conversation_timeline(conversation_timeline, 'technical_qa', "", day_response, current_question_no, room_id, process_id)
                                    
                                    # If speculative reflex didn't fire (no early return),
                                    # fire greeting reflex synchronously so we always have an ack
                                    if not day_reflex_result:
                                        logger.info(
                                            f"🔄 [process id: {process_id}][room id: {room_id}] "
                                            f"No speculative reflex for day response — firing greeting reflex sync"
                                        )
                                        day_reflex_result = reflex_engine.generate_greeting_reflex(
                                            transcript=day_response,
                                            room_id=room_id,
                                            process_id=process_id
                                        )
                                    
                                    # Speak the generated conversational acknowledgment
                                    day_ack = day_reflex_result.get("ack", "") if day_reflex_result else ""
                                    if day_ack and day_ack.strip():
                                        # Pre-select warmup question (for Round 1)
                                        warmup_q_preselected = None
                                        warmup_q_tts = None
                                        if current_round == 1:
                                            warmup_questions = [
                                                "Before we get into the technical questions, I'd love to hear a bit about your background. "
                                                "What got you into this field and what's been keeping you excited about it lately?",
                                                "So before we dive into the technical side of things, just give me a quick overview — "
                                                "what have you been working on recently and what kind of problems are you most drawn to?",
                                                "I'd love to get a sense of your journey so far. "
                                                "Walk me through what you've been up to professionally — what's the work you're most proud of?",
                                                "Before we jump into the technical stuff, tell me a little about yourself — "
                                                "how did you end up in this field and what kind of work gets you most excited?",
                                            ]
                                            warmup_q_preselected = random.choice(warmup_questions)
                                        
                                        # Speak day ack using sentence-chunked streaming TTS
                                        logger.info(f"🗣️ [process id: {process_id}][room id: {room_id}] Speaking day ack (streamed): '{day_ack}'")
                                        ai_speak_streamed_text(day_ack, driver, room_id, process_id)
                                        add_to_conversation_timeline(conversation_timeline, 'technical_qa', day_ack, "", current_question_no, room_id, process_id)
                                    else:
                                        warmup_q_preselected = None
                                        warmup_q_tts = None
                                    
                                # ✨ WARMUP: "Tell me about yourself" (conversational, NOT evaluated) ✨
                                if current_round == 1:
                                    if warmup_q_preselected:
                                        warmup_q = warmup_q_preselected
                                    else:
                                        warmup_questions = [
                                            "Before we get into the technical questions, I'd love to hear a bit about your background. "
                                            "What got you into this field and what's been keeping you excited about it lately?",
                                            "So before we dive into the technical side of things, just give me a quick overview — "
                                            "what have you been working on recently and what kind of problems are you most drawn to?",
                                            "I'd love to get a sense of your journey so far. "
                                            "Walk me through what you've been up to professionally — what's the work you're most proud of?",
                                            "Before we jump into the technical stuff, tell me a little about yourself — "
                                            "how did you end up in this field and what kind of work gets you most excited?",
                                        ]
                                        warmup_q = random.choice(warmup_questions)
                                    
                                    logger.info(
                                        f"🎙️ [process id: {process_id}][room id: {room_id}] "
                                        f"WARMUP: Asking 'tell me about yourself'"
                                    )
                                    # Speak warmup question using sentence-chunked streaming TTS
                                    ai_speak_streamed_text(warmup_q, driver, room_id, process_id)
                                    add_to_conversation_timeline(
                                        conversation_timeline, 'warmup', warmup_q, "",
                                        current_question_no, room_id, process_id
                                    )
                                    
                                    # Capture their response
                                    warmup_resp_dict, warmup_reflex_r, _, _, _, _ = _capture_with_speculative_reflex(
                                        driver, room_id, process_id,
                                        reflex_engine, warmup_q, job_description,
                                        mandatory_skills, 0, recent_acks,
                                        interview_id=interview_id
                                    )
                                    warmup_answer = warmup_resp_dict.get("answer", "") if warmup_resp_dict else ""
                                    if warmup_answer:
                                        logger.info(
                                            f"🎙️ [process id: {process_id}][room id: {room_id}] "
                                            f"WARMUP response: '{warmup_answer[:100]}...'"
                                        )
                                        add_to_conversation_timeline(
                                            conversation_timeline, 'warmup', "", warmup_answer,
                                            current_question_no, room_id, process_id
                                        )
                                        
                                        # If speculative reflex didn't fire, generate ack synchronously
                                        if not warmup_reflex_r:
                                            logger.info(
                                                f"🔄 [process id: {process_id}][room id: {room_id}] "
                                                f"No speculative reflex for warmup — firing greeting reflex sync"
                                            )
                                            warmup_reflex_r = reflex_engine.generate_greeting_reflex(
                                                transcript=warmup_answer,
                                                room_id=room_id,
                                                process_id=process_id
                                            )
                                    
                                    # Transitional ack — warm, moving to technical
                                    warmup_transition_ack = warmup_reflex_r.get("ack", "") if warmup_reflex_r else ""
                                    if not warmup_transition_ack or not warmup_transition_ack.strip():
                                        if warmup_answer and warmup_answer.strip():
                                            warmup_transition_ack = (
                                                "That's really interesting, thanks for sharing! "
                                                "Alright, let's get into some technical questions."
                                            )
                                        else:
                                            # Candidate didn't answer warmup — use a neutral transition
                                            warmup_transition_ack = (
                                                "No worries! Let's get started with the technical questions."
                                            )
                                    # Speak transition ack using sentence-chunked streaming TTS
                                    ai_speak_streamed_text(warmup_transition_ack, driver, room_id, process_id)
                                    add_to_conversation_timeline(
                                        conversation_timeline, 'warmup', warmup_transition_ack, "",
                                        current_question_no, room_id, process_id
                                    )
                                    
                        else:
                            ack_spoken_time = time.time()

                        # ── RESOLVE SKILL REJECTION (Done entirely in background now) ──
                        
                        # ───────────────────────────────────────────────────────────────────
                        # RESOLVE NEXT QUESTION (based on intent)
                        # ───────────────────────────────────────────────────────────────────
                        if intent.upper() == 'REPEAT':
                            # ⭐ SAFETY: Q1 greeting must NEVER be repeated
                            if current_question_no == 1:
                                logger.info(
                                    f"🛡️ [process id: {process_id}][room id: {room_id}] "
                                    f"Q1 REPEAT BLOCKED: Greeting must never be repeated. "
                                    f"Forcing intent='others' to advance to Q2."
                                )
                                intent = "others"
                                needs_planner = True
                                # Fall through to planner resolution below
                            else:
                                # Keep the original question
                                followup_chain_count = 0  # Reset chain on repeat
                                max_followups_for_question = random.choices([0, 1, 2, 3], weights=[15, 35, 40, 10], k=1)[0]
                                logger.info(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"🔁 Intent=REPEAT: Keeping original question"
                                )
                                next_question_no = current_question_no
                                # question_to_ask stays the same
                            
                        elif intent.upper() == 'REPHRASE':
                            # Dedicated GPT call to rephrase the current question
                            logger.info(
                                f"[process id: {process_id}][room id: {room_id}] "
                                f"🔄 Intent=REPHRASE: Making dedicated call to rephrase"
                            )
                            try:
                                rephrased = rephrase_question(question_to_ask)
                                logger.info(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"✅ Rephrased: '{question_to_ask[:60]}...' → '{rephrased[:60]}...'"
                                )
                                question_to_ask = rephrased
                            except Exception as e:
                                logger.error(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"❌ Rephrase failed: {e}. Keeping original question."
                                )
                            next_question_no = current_question_no
                            
                        elif intent.upper() == 'EXIT':
                            # Full exit flow with confirmation
                            logger.info(
                                f"🚪 [process id: {process_id}][room id: {room_id}] "
                                f"Intent=EXIT: Candidate requested to end interview"
                            )
                            exit_confirm_msg = "Are you sure you want to end the interview?"
                            ai_speak(exit_confirm_msg, driver, room_id, process_id)
                            add_to_conversation_timeline(conversation_timeline, 'exit', exit_confirm_msg, "", current_question_no, room_id, process_id)
                            
                            confirmation_dict = _capture_muted_window(driver, room_id, process_id)
                            if confirmation_dict:
                                confirmation = confirmation_dict["answer"]
                            else:
                                confirmation = ""
                            add_to_conversation_timeline(conversation_timeline, 'exit', "", confirmation if confirmation else "No response", current_question_no, room_id, process_id)
                            
                            if confirm_user_want_to_exit(confirmation, openai_client):
                                answer = "BOT NOTE: the user exited the meeting in the middle of the interview"
                                responses = update_responses(responses, curr_question, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                                logger.info(f"[process id: {process_id}][room id: {room_id}] Saved response for Q{current_question_no} (EXIT)")
                                greet_and_exit(candidate_name, driver, room_id, process_id)
                                # Candidate confirmed exit → mark it so Phase 4 (Q&A
                                # kickoff "…do you have any questions?") is SKIPPED.
                                # Otherwise the bot says goodbye AND then the kickoff —
                                # two thank-you messages after the candidate chose to end.
                                candidate_exit = True
                                continue_asking = False
                                is_last = False
                                break
                            else:
                                continue_msg = "Great, glad you're still with us! Okay, so let's jump back in \u2014 no worries at all about that, it happens."
                                ai_speak(continue_msg, driver, room_id, process_id)
                                add_to_conversation_timeline(conversation_timeline, 'exit', continue_msg, "", current_question_no, room_id, process_id)
                                if current_question_no != 1:
                                    repeat_qtn = True
                            
                        elif use_followup and followup_planner_future:
                            # ─── RESOLVE FOLLOW-UP QUESTION ───
                            try:
                                followup_result = followup_planner_future.result(timeout=15)
                                if followup_result and followup_result.get("next_question"):
                                    question_to_ask = followup_result["next_question"]
                                    next_question_no = followup_result.get("question_no", current_question_no + 1)
                                    metrics = followup_result.get("metrics", {})
                                    planner_latency = metrics.get("api_latency_ms", 0) if isinstance(metrics, dict) else 0
                                    logger.info(
                                        f"🔄 [process id: {process_id}][room id: {room_id}] "
                                        f"FOLLOWUP PLANNER ready! chain={followup_chain_count}/{max_followups_for_question} | "
                                        f"Latency: {planner_latency}ms | Q: {question_to_ask[:50]}..."
                                    )
                                else:
                                    # Follow-up planner returned empty — fall through to normal planner
                                    logger.warning(
                                        f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                        f"Followup planner returned empty. Falling through to main planner."
                                    )
                                    followup_chain_count = 0
                                    use_followup = False
                                    # Re-launch main planner synchronously
                                    planner_result = client.create_planner_response(
                                        question_number=current_question_no,
                                        user_message=answer,
                                        intent=intent,
                                        previous_response_id=previous_response_id,
                                        current_question_text=question_to_ask,
                                        resume_text=resume,
                                        previous_intent=previous_intent,
                                        room_id=room_id,
                                        process_id=process_id,
                                        context=context
                                    )
                                    if planner_result and planner_result.get("next_question"):
                                        question_to_ask = planner_result["next_question"]
                                        next_question_no = planner_result.get("question_no", current_question_no + 1)
                                        response_id = planner_result.get("response_id")
                                        if response_id:
                                            previous_response_id = response_id
                            except Exception as fu_plan_err:
                                logger.error(
                                    f"❌ [process id: {process_id}][room id: {room_id}] "
                                    f"FOLLOWUP PLANNER failed: {fu_plan_err}. Falling back to main planner."
                                )
                                followup_chain_count = 0
                                use_followup = False
                                next_question_no = current_question_no + 1
                                
                        elif needs_planner and planner_future:
                            # Wait for planner/fallback result (should be ready by now since ack took 2-4s)
                            try:
                                planner_result = planner_future.result(timeout=15)
                                
                                if planner_result is None:
                                    raise Exception("Background task returned None")
                                
                                if planner_result is None:
                                    logger.warning("⚠️ PLANNER returned None (Error)")
                                    new_question_from_ai = ""  # Will trigger fallback
                                    planner_latency = 0
                                    next_question_no = current_question_no + 1
                                    response_id = None
                                    previous_response_id = None
                                else:
                                    # If stateless fallback, update messages for conversation continuity
                                    if "ai_response" in planner_result:
                                        messages.append({"role": "assistant", "content": json.dumps(planner_result["ai_response"])})
                                        logger.info(
                                            f"🤖 [process id: {process_id}][room id: {room_id}] "
                                            f"STATELESS FALLBACK result: Q{planner_result.get('question_no', current_question_no + 1)} | "
                                            f"question='{planner_result.get('next_question', '')[:80]}...'"
                                        )
                                    
                                    # CHECK FOR END SIGNAL
                                    metrics = planner_result.get("metrics", {})
                                    planner_latency = metrics.get("api_latency_ms", 0) if isinstance(metrics, dict) else 0
                                    
                                    next_vector_dict = metrics.get("vector") if isinstance(metrics, dict) else None
                                    
                                    new_question_from_ai = planner_result.get("next_question", "")
                                    next_question_no = planner_result.get("question_no", current_question_no + 1)
                                    response_id = planner_result.get("response_id")
                                    # Only update previous_response_id if planner returned
                                    # a valid Responses API ID (Chat Completions planner returns None)
                                    if response_id:
                                        previous_response_id = response_id
                                
                                # CRITICAL: If planner returns empty question but NO error, it means SKILLS EXHAUSTED
                                if not new_question_from_ai and planner_result is not None:
                                    logger.warning("⛔ ALL SKILLS EXHAUSTED (Planner returned empty). Ending interview.")
                                    ai_speak(
                                        "It seems we've covered the key areas I wanted to discuss. Thank you for your time.",
                                        driver, room_id, process_id
                                    )
                                    continue_asking = False
                                    break

                                # Normal update
                                if new_question_from_ai:
                                    question_to_ask = new_question_from_ai
                                    current_vector_dict = next_vector_dict
                                
                                logger.info(
                                    f"✅ [process id: {process_id}][room id: {room_id}] "
                                    f"PLANNER ready! Latency: {planner_latency}ms. "
                                    f"Next Q: {question_to_ask[:50]}..."
                                )
                                
                            except FuturesTimeoutError:
                                logger.error(
                                    f"❌ [process id: {process_id}][room id: {room_id}] "
                                    f"PLANNER timed out (15s). Using fallback question."
                                )
                                # Fallback: generic question from skill tracker
                                if client.skill_tracker:
                                    fallback_skills = client.skill_tracker.get_next_skills(count=1)
                                    if not fallback_skills: 
                                        logger.warning("No skills left for fallback either. Ending.")
                                        ai_speak("We have covered everything. Thank you.", driver, room_id, process_id)
                                        continue_asking = False
                                        break
                                    skill_str = ", ".join(fallback_skills)
                                else:
                                    skill_str = "your technical skills"
                                question_to_ask = f"Can you tell me about your experience working with {skill_str}?"
                                next_question_no = current_question_no + 1
                            except Exception as e:
                                logger.error(
                                    f"❌ [process id: {process_id}][room id: {room_id}] "
                                    f"PLANNER failed: {e}. Using fallback question."
                                )
                                if client.skill_tracker:
                                    fallback_skills = client.skill_tracker.get_next_skills(count=1)
                                    if not fallback_skills:
                                        logger.warning("No skills left for fallback either. Ending.")
                                        ai_speak("We have covered everything. Thank you.", driver, room_id, process_id)
                                        continue_asking = False
                                        break
                                    skill_str = ", ".join(fallback_skills)
                                else:
                                    skill_str = "your technical skills"
                                question_to_ask = f"Can you tell me about your experience working with {skill_str}?"
                                next_question_no = current_question_no + 1
                        else:
                            # Edge case: no planner needed AND no planner running
                            logger.info(
                                f"[process id: {process_id}][room id: {room_id}] "
                                f"No question generation needed (intent={intent})"
                            )
                            # Only advance question number for intents that move forward
                            # ✅ Set current_question_no to exit the while loop
                            current_question_no = config.questions_per_round + 1
                            break
                        
                        # ───────────────────────────────────────────────────────────────────
                        # 🚀 PREFETCH TTS: Fire OpenAI TTS NOW for the next question
                        #    so audio is ready by the time we need to speak it.
                        #    This runs in parallel with video checks & other processing.
                        # ───────────────────────────────────────────────────────────────────
                        question_tts_future = None
                        if repeat_question_tts_future:
                            # ⚡ REPEAT/REPHRASE: TTS was pre-generated in parallel with ack
                            question_tts_future = repeat_question_tts_future
                            logger.info(
                                f"⚡ [process id: {process_id}][room id: {room_id}] "
                                f"Using REPEAT/REPHRASE TTS prefetch (fired during ACK playback)"
                            )
                        elif chained_prefetch and chained_prefetch.get("tts_future"):
                            # ⚡ Reuse TTS that was already fired during ACK playback
                            question_tts_future = chained_prefetch["tts_future"]
                            logger.info(
                                f"⚡ [process id: {process_id}][room id: {room_id}] "
                                f"Using CHAINED TTS prefetch (fired during ACK playback)"
                            )
                        elif question_to_ask and question_to_ask.strip() and continue_asking:
                            # Fallback: chain didn't fire, fire now
                            question_tts_future = prefetch_tts(
                                question_to_ask, room_id, process_id, tts_executor
                            )
                        
                        if current_question_no > config.questions_per_round + 1:
                            logger.warning(f"[process id: {process_id}][room id: {room_id}] Question number exceeded limit, ending interview")
                            break
                        
                        # ═══════════════════════════════════════════════════════════════════════
                        # Store intent for next call (for skill reuse logic)
                        previous_intent = intent
                        
                        # ═══════════════════════════════════════════════════════════════════════
                        # ✅ PROCESS RESPONSE: Update skill tracker (covered/skipped)
                        # ═══════════════════════════════════════════════════════════════════════
                        if client:
                            client.process_response(intent)
                        
                        # ═══════════════════════════════════════════════════════════════════════
                        # ✅ Trigger answer analysis NOW (after we know intent)
                        # Skip for: repeat, exit, clarify intents (non-substantive answers)
                        # ═══════════════════════════════════════════════════════════════════════
                        skip_intents = ["repeat", "exit", "clarify", "skip"]
                        if pending_analysis and intent not in skip_intents:
                            try:
                                trigger_answer_analysis_async(**pending_analysis)
                                logger.info(
                                    f"✓ [process id: {process_id}][room id: {room_id}] "
                                    f"Analysis triggered for Q{current_question_no} (intent={intent})"
                                )
                            except Exception as e:
                                logger.error(
                                    f"[process id: {process_id}][room id: {room_id}] "
                                    f"Analysis trigger failed (non-critical): {e}"
                                )
                        elif pending_analysis:
                            logger.info(
                                f"⏭️ [process id: {process_id}][room id: {room_id}] "
                                f"Skipping analysis for Q{current_question_no} (intent={intent})"
                            )
                        
                        logger.info(f"[process id: {process_id}][room id: {room_id}] Next Question: {question_to_ask} Question no:{next_question_no} Intent:{intent}")
                        
                        # ═══════════════════════════════════════════════════════════════════════
                        # ✅ VALIDATION: Ensure question_no is consistent with expected value
                        # ═══════════════════════════════════════════════════════════════════════
                        expected_question_no = current_question_no
                        if intent.upper() == "OTHERS" or intent.upper() == "SKIP":
                            expected_question_no = current_question_no + 1

                        if next_question_no != expected_question_no:
                            logger.warning(
                                f"⚠️ [process id: {process_id}][room id: {room_id}] "
                                f"question_no={next_question_no}, expected={expected_question_no}. Correcting."
                            )
                            next_question_no = expected_question_no

                        # Task 2: for a normal advance to a NEW JD question, source it from the
                        # cached bank (sequential) instead of the planner's text, and remember its
                        # Redis audio key so the speak seam plays cached audio. Resume slots and
                        # follow-ups stay live (candidate-specific / dynamic). Plan exhausted → live.
                        if (USE_CACHED_QUESTIONS and qbank_plan is not None
                                and intent.upper() in ("OTHERS", "SKIP")
                                and not use_followup):
                            try:
                                from core.responses_client import decide_blend_mode
                                _is_resume_slot = decide_blend_mode(next_question_no) == "resume"
                            except Exception:
                                _is_resume_slot = False
                            if not _is_resume_slot:
                                _cq = qbank_plan.next()
                                if _cq:
                                    question_to_ask = _cq["text"]
                                    question_audio_key = _cq["audio_key"]
                                    logger.info(
                                        f"📚 [process id: {process_id}][room id: {room_id}] "
                                        f"CACHED Q sourced [{_cq['group']}/{_cq['id']}] (sequential): "
                                        f"{question_to_ask[:60]}..."
                                    )

                    except Exception as e:
                        logger.error(f'\n[process id: {process_id}][room id: {room_id}] ❌ Split-brain error: {e}. Falling back to single-call mode.')
                        try:
                            if not previous_response_id:
                                messages.append({"role": "user", "content": answer})
                            ai_response, metrics, response_id = client.create_response(
                                question_number=current_question_no,
                                user_message=answer if previous_response_id else None,
                                messages=messages if not previous_response_id else None,
                                previous_response_id=previous_response_id,
                                use_stateful=bool(previous_response_id),
                                inject_reminders=CRITICAL_REMINDERS,
                                current_question_text=question_to_ask,
                                previous_intent=previous_intent,
                                context=context
                            )
                            # Guard: validate response before JSON parsing
                            if not ai_response or not ai_response.strip():
                                raise ValueError(f"create_response returned empty text (metrics={metrics})")
                            # Strip markdown code fences if present
                            ai_response_clean = ai_response.strip()
                            if ai_response_clean.startswith("```"):
                                ai_response_clean = re.sub(r'^```(?:json)?\s*', '', ai_response_clean)
                                ai_response_clean = re.sub(r'```\s*$', '', ai_response_clean).strip()
                            ai_response = json.loads(ai_response_clean)
                            logger.info(f'\n[process id: {process_id}][room id: {room_id}] 🤖 AI (single-call fallback): {ai_response}')
                            next_question_no = ai_response.get("question_no", current_question_no + 1)
                            ack = ai_response.get("ack", "")
                            intent = ai_response.get("intent", "others")
                            if intent.upper() != 'REPEAT':
                                question_to_ask = ai_response.get("next_question", question_to_ask)
                            ack = remove_question_from_ack(ack, question_to_ask)
                            previous_intent = intent
                            
                            expected_question_no = current_question_no
                            if intent.upper() in ("OTHERS", "SKIP"):
                                expected_question_no = current_question_no + 1
                            if next_question_no != expected_question_no:
                                next_question_no = expected_question_no
                            
                            messages.append({"role": "assistant", "content": json.dumps(ai_response)})
                            previous_response_id = response_id
                        except Exception as e2:
                            logger.error(f'\n[process id: {process_id}][room id: {room_id}] ❌ All fallbacks failed: {e2}')
                            if not previous_response_id and messages and messages[-1].get("role") == "user":
                                messages.pop()
                            continue
                    
                    end = time.time()
                    elapsed = end - start
                    logger.info(f"⏱️[process id: {process_id}][room id: {room_id}] Total orchestration time Q{current_question_no}: {elapsed:.6f} seconds")

                    # ========================================================================
                    # ✅ UPDATED INTENT HANDLING WITH ALL FIXES
                    # ========================================================================
                    if question_to_ask == "":
                        ai_speak_streamed_text(ack, driver, room_id, process_id)
                    
                    if intent.upper() == 'SKIP':
                        # NOTE: skip ack already spoken in split-brain block (reflex ack)
                        # No need to speak again
                        # No need to log to timeline again - handled by reflex capture
                        
                        # if 2 <= current_question_no:
                        if push_to_report:
                            responses = update_responses(responses, curr_question, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                            logger.info(f"[process id: {process_id}][room id: {room_id}] Saved response for Q{current_question_no} (SKIP)")
                        
                        # ✅ WE control question progression, not the AI
                        current_question_no = current_question_no + 1
                        repeat_qtn = False
                        
                    elif intent.upper() == 'EXIT':
                        # According to new prompt: question_no=SAME, ack="", next_question=SAME
                        exit_confirm_msg = "Are you sure you want to end the interview?"
                        ai_speak(exit_confirm_msg, driver, room_id, process_id)
                        add_to_conversation_timeline(conversation_timeline, 'exit', exit_confirm_msg, "", current_question_no, room_id, process_id)
                        
                        confirmation_dict = _capture_muted_window(driver, room_id, process_id)
                        if confirmation_dict:
                            confirmation=confirmation_dict["answer"]
                        else:
                            confirmation=""
                        add_to_conversation_timeline(conversation_timeline, 'exit', "", confirmation if confirmation else "No response", current_question_no, room_id, process_id)
                        
                        if confirm_user_want_to_exit(confirmation, openai_client):
                            answer = "BOT NOTE: the user exited the meeting in the middle of the interview"
                            # ✅ FIXED: Only save technical questions (Q3-Q15)
                            responses = update_responses(responses, curr_question, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                            logger.info(f"[process id: {process_id}][room id: {room_id}] Saved response for Q{current_question_no} (EXIT)")
                            greet_and_exit(candidate_name, driver, room_id, process_id)
                            # Candidate confirmed exit → skip Phase 4 Q&A kickoff so the
                            # bot doesn't say goodbye AND then "…do you have any questions?".
                            candidate_exit = True
                            continue_asking = False
                            is_last = False
                            break
                        else:
                            continue_msg = "Great, glad you're still with us! Okay, so let's jump back in \u2014 no worries at all about that, it happens."
                            ai_speak(continue_msg, driver, room_id, process_id)
                            add_to_conversation_timeline(conversation_timeline, 'exit', continue_msg, "", current_question_no, room_id, process_id)
                            # Question remains same, continue loop
                            if current_question_no!=1:
                                repeat_qtn = True
                            
                    elif intent.upper() == 'REPEAT':
                        # ⭐ SAFETY: Q1 greeting must NEVER be repeated
                        if current_question_no == 1:
                            logger.info(
                                f"🛡️ [process id: {process_id}][room id: {room_id}] "
                                f"Q1 POST-REPEAT BLOCKED: Greeting must never be repeated. "
                                f"Advancing to Q2."
                            )
                            current_question_no = 2
                            break
                        # repeat_qtn = True
                        if repeat_count == 6:
                            if current_question_no < config.questions_per_round + 1:
                                max_repeat_msg = "Hey, I think we might be going around in circles a little bit here, and I totally want to make sure we use our time wisely. So how about we just move forward to the next question — I think you'll find it a bit more engaging."
                                ai_speak(max_repeat_msg, driver, room_id, process_id)
                                add_to_conversation_timeline(conversation_timeline, 'repeat', max_repeat_msg, "", current_question_no, room_id, process_id)
                                # Get next question from AI
                                start = time.time()
                                curr_question = question_to_ask
                                
                                try:
                                    if previous_response_id:
                                        ai_response, metrics, response_id = client.create_response(
                                            question_number=current_question_no,
                                            user_message="skip this question",
                                            previous_response_id=previous_response_id,
                                            use_stateful=True,
                                            context=context
                                        )
                                    else:
                                        messages.append({"role": "user", "content": answer})
                                        ai_response, metrics, response_id = client.create_response(
                                            question_number=current_question_no,
                                            messages=messages,
                                            use_stateful=False,
                                            context=context
                                        )
                                    
                                    ai_response = json.loads(ai_response)
                                    # logger.info(ai_response)
                                    logger.info(f'\n[process id: {process_id}][room id: {room_id}] 🤖 AI: {ai_response}')
                                    # Extract response fields based on new prompt structure
                                    next_question_no = ai_response["question_no"]
                                    ack = ai_response["ack"]
                                    question_to_ask = ai_response["next_question"]
                                    ack = remove_question_from_ack(ack, question_to_ask)
                                    intent = ai_response["intent"]
                                    if question_to_ask and 2 <= current_question_no <= config.questions_per_round + 1:
                                        client.register_asked_question(current_question_no, question_to_ask)
                                    
                                    logger.info(f"[process id: {process_id}][room id: {room_id}] Next Question: {question_to_ask} Question no:{next_question_no} Intent:{intent}")
                                    
                                    messages.append({"role": "assistant", "content": json.dumps(ai_response)})
                                    previous_response_id = response_id

                                except (ValueError, ConnectionError, OSError) as e:
                                    try:
                                        messages.append({"role": "user", "content": answer})
                                        ai_response, metrics, response_id = client.create_response(
                                            question_number=current_question_no,
                                            messages=messages,
                                            use_stateful=False,
                                            context=context
                                        )
                                        ai_response = json.loads(ai_response)
                                        logger.info(f'\n[process id: {process_id}][room id: {room_id}] 🤖 AI (fallback): {ai_response}')
                                        
                                        next_question_no = ai_response["question_no"]
                                        ack = ai_response["ack"]
                                        question_to_ask = ai_response["next_question"]
                                        ack = remove_question_from_ack(ack, question_to_ask)
                                        intent = ai_response["intent"]
                                        
                                        messages.append({"role": "assistant", "content": json.dumps(ai_response)})
                                        previous_response_id = response_id
                                    except Exception as e2:
                                        logger.error(f'\n[process id: {process_id}][room id: {room_id}] ❌ Error: {e2}')
                                        messages.pop()  # Remove the just-added user message
                                        previous_response_id = None
                                        continue
                                
                                end = time.time()
                                elapsed = end - start
                                logger.info(f"⏱️[process id: {process_id}][room id: {room_id}] Execution time (for generating new question....question :{current_question_no}): {elapsed:.6f} seconds")

                            else:
                                max_repeat_msg = "So I've gone through that question a few times now, and I want to be mindful of our time here. How about we move on to the next one — sometimes a fresh topic can help get things flowing again, and there's absolutely no stress about it."
                                ai_speak(max_repeat_msg, driver, room_id, process_id)
                                add_to_conversation_timeline(conversation_timeline, 'repeat', max_repeat_msg, "", current_question_no, room_id, process_id)
                            
                            answer = "BOT NOTE: The user asked to repeat the question maximum number of times so considering the response to this question as NO RESPONSE."
                            # ✅ FIXED: Only save technical questions (Q3-Q15)
                            if push_to_report:
                                responses = update_responses(responses, curr_question, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                                logger.info(f"[process id: {process_id}][room id: {room_id}] Saved response for Q{current_question_no} (MAX REPEAT)")
                            
                            # ✅ WE control question progression
                            current_question_no = current_question_no + 1
                            break
                        else:
                            # Don't speak ack here - it will be spoken at top of loop on next iteration
                            if current_question_no==0:
                                repeat_qtn = True
                            # ✅ CRITICAL: question_no stays SAME, question_to_ask stays SAME
                            # DO NOT increment current_question_no here
                            
                    elif intent.upper() == "VIDEO":
                        # According to new prompt: question_no=SAME, ack="", next_question=SAME
                        # But we respond BEFORE continuing
                        is_face_ok = check_face_visibility(process_id, room_id, interview_id)
                        if is_face_ok:
                            video_ok_msg = random.choice([
                                "Yes, I can see you clearly.",
                                "Your video is visible without issues.",
                                "I can see you well.",
                                "Yes, your camera feed is clear.",
                                "I can see you just fine."
                            ])
                            ai_speak(video_ok_msg, driver, room_id, process_id)
                            add_to_conversation_timeline(conversation_timeline, 'clarification', video_ok_msg, "", current_question_no, room_id, process_id)
                        else:
                            video_off_msg = "it seems your video is off, please turn on your video."
                            ai_speak(video_off_msg, driver, room_id, process_id)
                            add_to_conversation_timeline(conversation_timeline, 'warning', video_off_msg, "", current_question_no, room_id, process_id)
                        if current_question_no!=1:
                            repeat_qtn = True
                        # ✅ question_no stays SAME, question_to_ask stays SAME
                        
                    elif intent.upper() == "AUDIO":
                        # According to new prompt: question_no=SAME, ack="", next_question=SAME
                        audio_ok_msg = random.choice([
                            "Yes, I can hear you clearly.",
                            "Your audio is coming through fine.",
                            "I can hear you without any issues.",
                            "Yes, your voice is clear.",
                            "I can hear you loud and clear."
                        ])
                        ai_speak(audio_ok_msg, driver, room_id, process_id)
                        add_to_conversation_timeline(conversation_timeline, 'clarification', audio_ok_msg, "", current_question_no, room_id, process_id)
                        if current_question_no!=1:
                            repeat_qtn = True
                        
                    elif intent.upper() == "REPHRASE":
                        # According to new prompt: question_no=SAME, next_question=REPHRASED
                        # Ack will be spoken at top of loop on next iteration
                        if current_question_no!=1:
                            repeat_qtn = True
                    else:  # intent.upper() == "OTHERS"
                        # According to new prompt: question_no=CURRENT+1, next_question=NEXT
                        
                        # ✅ CRITICAL FIX: Save response BEFORE checking for placeholders or interview end
                        # This ensures Q15 response is saved when moving to Q16
                        # Only save technical questions (Q3-Q15), NOT Q1 (greeting) or Q2 (background)
                        if push_to_report:
                            responses = update_responses(responses, curr_question, current_question_no, answer, False, room_id, process_id, interview_id=interview_id, round_number=context.current_round, skill=(", ".join(client.skill_tracker._question_history.get(current_question_no, [])) if client.skill_tracker and client.skill_tracker._question_history.get(current_question_no) else ""), skill_states=context.skill_states, vector_dict=current_vector_dict, candidate_id=candidate_id, job_id=job_id)
                            logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Saved response for Q{current_question_no} (OTHERS)")
                        
                        # ✅ CRITICAL: Detect placeholder questions and force regeneration
                        if question_to_ask and ('<Question' in question_to_ask or '[' in question_to_ask or 
                                               (question_to_ask.startswith('<') and question_to_ask.endswith('>'))):
                            logger.error(f"❌[process id: {process_id}][room id: {room_id}] AI returned PLACEHOLDER question: {question_to_ask}")
                            fallback_msg = "Let me rephrase that question for you."
                            ai_speak(fallback_msg, driver, room_id, process_id)
                            add_to_conversation_timeline(conversation_timeline, 'error', fallback_msg, "", current_question_no, room_id, process_id)
                            
                            # Force re-generation by repeating with explicit instruction
                            repeat_qtn = True
                            # Add explicit message to force real question
                            messages.append({"role": "user", "content": "CRITICAL: You just returned a placeholder. Generate the ACTUAL, COMPLETE question text that will be spoken aloud. NO placeholders like <Question X> allowed."})
                            previous_response_id = None  # Force stateless mode for correction
                            continue
                        
                        # Check if interview should end (after last technical question is answered)
                        if current_question_no >= config.questions_per_round + 1:
                            if next_question_no >= config.questions_per_round + 2 or not question_to_ask or not question_to_ask.strip():
                                logger.info(f"[process id: {process_id}][room id: {room_id}] Interview ending: current_question_no={current_question_no}, next_question_no={next_question_no}, next_question={'empty' if not question_to_ask else 'present'}")
                                
                                # ✅ BUG-3 FIX: Clean up closing ack to remove misleading "next/final question" language
                                # Since the interview is ending, phrases like "let's move on to the final question" are confusing
                                if ack and ack.strip():
                                    import re
                                    # Remove sentences containing "final question", "next question", "move on", "wrap up"
                                    sentences = re.split(r'(?<=[.!?])\s+', ack)
                                    cleaned_sentences = []
                                    for s in sentences:
                                        s_lower = s.lower()
                                        if any(phrase in s_lower for phrase in ['final question', 'last question', 'next question', "let's move on", "move on to", "wrap up", "one more question"]):
                                            logger.info(f"[process id: {process_id}][room id: {room_id}] 🧹 Stripped misleading phrase from closing ack: '{s}'")
                                        else:
                                            cleaned_sentences.append(s)
                                    ack = ' '.join(cleaned_sentences).strip()
                                
                                # Speak the final ack (closing message)
                                # MERGE FIX: When Phase 4 (candidate Q&A) runs next, its
                                # kickoff message already thanks the candidate AND asks
                                # "do you have any questions?". Speaking a separate goodbye
                                # ack here produced TWO back-to-back "thank you" messages.
                                # Phase 4 is skipped only when candidate_exit is True, so
                                # speak this standalone closing ONLY in that case; otherwise
                                # let the kickoff be the single closing+Q&A message.
                                if ack and ack.strip() and candidate_exit:
                                    ai_speak_streamed_text(ack, driver, room_id, process_id)
                                    add_to_conversation_timeline(conversation_timeline, 'closing', ack, "", current_question_no, room_id, process_id)
                                
                                # ✅ Set current_question_no to exit the while loop
                                current_question_no = config.questions_per_round + 2
                                continue_asking = False
                                break
                        
                        # ✅ WE control question progression, not the AI
                        current_question_no = current_question_no + 1
                        repeat_qtn = False
                        
                        # Safety check
                        if current_question_no > config.questions_per_round + 1:
                            logger.warning(f"[process id: {process_id}][room id: {room_id}] Question number exceeded limit, ending interview")
                            continue_asking = False
                            break
                    if current_question_no==1:
                        repeat_qtn=False
                    repeat_count += 1
                    push_to_report=True

                if candidate_exit:
                    break
            
            # Final closing if interview completed normally
            # Removed redundant ack_last_question call here since Phase 4 handles the completion & transition.

            thread_manager.stop_video_monitoring_thread()

            # --- START PHASE 4: Candidate Q&A ---
            if not candidate_exit:
                logger.info(f"🎙️[process id: {process_id}][room id: {room_id}] Starting Phase 4: Candidate Q&A (Max 3 rounds)")
                
                # Kickoff message (Combined conclusion and Q&A transition)
                kickoff_msg = f"Thank you for your responses, {candidate_name}. That concludes the technical portion of our interview. Our team will review your answers carefully. Before we wrap up, do you have any questions for me about the company or the role?"
                ai_speak(kickoff_msg, driver, room_id, process_id)
                add_to_conversation_timeline(conversation_timeline, 'qa_kickoff', kickoff_msg, "", None, room_id, process_id)
                
                # removed local imports to prevent UnboundLocalError
                
                qa_messages = [
                    {"role": "system", "content": f"""You are AIRA, the AI recruiter. We have just finished the technical interview. 
The candidate is now allowed to ask you questions about the company, the role, location, timings, etc.
Use the following context to answer:
- Job Description: {interview.get('job_description', 'Not provided')}
- Mandatory Skills: {interview.get('mandatory_skills', 'Not provided')}

CRITICAL RULES:
1. DO NOT disclose any salary details. If asked about salary, say "Compensation and salary details will be discussed in future rounds."
2. Provide details like timings, location, and role expectations ONLY if explicitly mentioned in the Job Description. If not mentioned, politely say you don't have that specific information but the recruitment team will be happy to answer it later.
3. If the candidate says they are "not interested", "dont have any questions", "want to exit", "no", "nope" or similar phrases indicating they want to end the interview, you MUST return a valid JSON object with the exact format:
   {{"intent": "exit_interview", "response": "Thank you for your time. Have a great day!"}}
   Do NOT ask for confirmation to exit. Just exit smoothly.
4. Keep your answers concise, conversational, and polite. Max 2-3 sentences.
5. If the candidate asks a valid question, answer it and ask if they have any other questions. Return your response as plain text, NOT JSON, unless they want to exit.
"""}
                ]
                
                qa_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
                
                for turn in range(3):
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Q&A Turn {turn+1}/3")
                    
                    answer_dict = _capture_normal(driver, room_id, process_id)
                    candidate_input = answer_dict.get("answer", "") if answer_dict else ""
                    
                    if candidate_input:
                        add_to_conversation_timeline(conversation_timeline, 'candidate_qa_response', "", candidate_input, 999, room_id, process_id)
                    
                    if not candidate_input or candidate_input.strip() == "":
                        logger.info(f"[process id: {process_id}][room id: {room_id}] Empty input in Q&A, treating as exit.")
                        exit_msg = "It seems you don't have any further questions. We will wrap up the interview here. Thank you for your time, the recruiting team will be in touch with you soon. Have a great day!"
                        ai_speak(exit_msg, driver, room_id, process_id)
                        add_to_conversation_timeline(conversation_timeline, 'closing', exit_msg, "", 999, room_id, process_id)
                        break
                        
                    qa_messages.append({"role": "user", "content": candidate_input})
                    
                    try:
                        response = qa_client.chat.completions.create(
                            model="gpt-4o",
                            messages=qa_messages,
                            temperature=0.7
                        )
                        ai_reply = response.choices[0].message.content.strip()
                        
                        # Check JSON intent for exit
                        if ai_reply.startswith("{") and "intent" in ai_reply:
                            try:
                                reply_json = json.loads(ai_reply)
                                if reply_json.get("intent") == "exit_interview":
                                    exit_response = reply_json.get("response", "Thank you for your time. Have a great day!")
                                    ai_speak(exit_response, driver, room_id, process_id)
                                    add_to_conversation_timeline(conversation_timeline, 'closing', exit_response, "", 999, room_id, process_id)
                                    break
                            except Exception:
                                pass # fallback to speak it normally if JSON somehow invalid
                        
                        # Normal conversational reply
                        ai_speak(ai_reply, driver, room_id, process_id)
                        add_to_conversation_timeline(conversation_timeline, 'qa_bot_reply', ai_reply, "", 999, room_id, process_id)
                        qa_messages.append({"role": "assistant", "content": ai_reply})
                        
                    except Exception as e:
                        logger.error(f"[process id: {process_id}][room id: {room_id}] Error in Q&A GPT call: {e}")
                        break
                else:
                    # After 3 turns finish naturally
                    final_qa_msg = "We've covered quite a bit! To respect your schedule, we'll wrap up here. Thank you for your time today, and the recruitment team will follow up with next steps. Have a great day!"
                    ai_speak(final_qa_msg, driver, room_id, process_id)
                    add_to_conversation_timeline(conversation_timeline, 'closing', final_qa_msg, "", 999, room_id, process_id)
            # --- END PHASE 4 ---

            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"📊 Answer analysis results: sessions/session_interview_{interview_id}.json"
            )
            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"📊 LLM interpretive layer: {'ENABLED' if os.getenv('ENABLE_LLM_INTERPRETER', 'false').lower() == 'true' else 'DISABLED (deterministic only)'}"
            )
            
            # Wait for all pending score calculations to complete
            logger.info(f"[process id: {process_id}][room id: {room_id}] Waiting for score calculations to complete...")
            thread_manager.score_queue.join()
            
            # Get final scores
            # final_scores = thread_manager.score_data.get_scores()
            # total_score = final_scores['total_score']
            # avg_score = final_scores['avg_score']
            # thread_manager.stop_score_thread()

            # logger.info(f"[process id: {process_id}][room id: {room_id}] Final scores - Total: {total_score}, Average: {avg_score}")
            
            # Sentinel room stop call removed — not used.

            
            try:
                end_meeting_for_all(room_id, process_id=process_id)
            except Exception:
                pass
            
            end = time.time()
            elapsed = end - start
            logger.info(f"[process id: {process_id}][room id: {room_id}] Execution time (time taken to quit driver and complete end meeting api...): {elapsed:.6f} seconds")
            
            # --- Final report and wrap-up ---
            actual_interview_end_time = datetime.now()

            # Conversation is over — freeze the recording NOW so the ~30s of
            # analysis / report / email / cleanup that follows isn't captured as a
            # blank (black + silence) tail. The recorder is finalized (mux) later
            # in the orchestrator's finally via stop(); this only stops capture.
            try:
                _fc_adapter = get_livekit_adapter()
                _fc_rec = getattr(getattr(_fc_adapter, "bot", None), "_local_recorder", None) if _fc_adapter else None
                if _fc_rec is not None:
                    _fc_rec.end_capture()
            except Exception as _fc_err:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] recorder end_capture failed: {_fc_err}")

            summary = {
                'responses': responses,
                'conversation_timeline': conversation_timeline,
                'candidate_name': candidate_name,
                'interview_id': interview_id,
                'total_questions': len(responses)
            }
            
            start = time.time()
            
            # ── Pre-Report Diagnostic: question params + difficulty trace ──
            from core.difficulty_policy import get_escalation_debug_info
            
            logger.info(
                f"\n{'='*70}\n"
                f"📋 [process id: {process_id}][room id: {room_id}] "
                f"PRE-REPORT DIAGNOSTIC — {len(responses)} questions\n"
                f"{'='*70}"
            )
            
            # Config snapshot
            if config:
                logger.info(
                    f"  ⚙️  CONFIG: base_difficulty={config.base_difficulty} | "
                    f"role={config.role_experience_required} | "
                    f"rounds={config.total_rounds} | "
                    f"q_per_round={config.questions_per_round} | "
                    f"resume_ratio={config.resume_question_ratio}"
                )
            
            # Skills snapshot
            if client and client.skill_tracker:
                tracker = client.skill_tracker
                asked_skills = sorted(list(set([s for skills in tracker._question_history.values() for s in skills])))
                logger.info(
                    f"  🎯 SKILLS: mandatory={tracker.mandatory_skills} | "
                    f"cycle={tracker._cycle_count} | "
                    f"asked={asked_skills}"
                )

            
            # Per-question breakdown
            logger.info(f"  {'─'*96}")
            logger.info(f"  FULL QUESTION BREAKDOWN (UNTRUNCATED):")
            logger.info(f"  {'─'*96}")
            
            for i, resp in enumerate(responses):
                q_num = resp.get('id', i + 1)
                q_text = resp.get('question', '').strip().replace('\n', ' ')
                
                # Metadata initialization
                metadata = {}
                level_str = "N/A"
                skills_list = ["N/A"]
                flag = False  # Explicit initialization
                
                if client and hasattr(client, '_question_log'):
                    # Find matching q_num entry
                    for entry in client._question_log:
                        if entry.get("q_num") == q_num:
                            metadata = entry
                            level_str = entry.get("level", "N/A")
                            skills_list = entry.get("skills", ["N/A"])
                            flag = entry.get("flag", False)
                            break
                
                skills_str = ", ".join(skills_list) if isinstance(skills_list, list) else str(skills_list)
                if len(skills_str) > 25:
                    skills_str = skills_str[:22] + "..."
                
                logger.info(
                    f"  Q{q_num:<2} | Level: {level_str}\n"
                    f"      Skills: {skills_list}\n"
                    f"      Question: {q_text}\n"
                    f"  {'-'*60}"
                )
                
                # Difficulty Level info
                if config:
                    round_for_q = (i // max(config.questions_per_round, 1)) + 1
                    debug = get_escalation_debug_info(
                        config.base_difficulty,
                        config.role_experience_required,
                        round_for_q,
                        q_num,
                        override_vector=resp.get("vector")
                    )
                    cap_tag = " [CAP]" if debug.get('capped', False) else ""
                    level_str = f"A{debug['A']} C{debug['C']} Amb{debug['Amb']} S{debug['S']}{cap_tag}"
                else:
                    level_str = "N/A"
                
                flag_str = " ⚠️FLAGGED" if flag else ""
                logger.info(f"  Q{q_num:<3} {level_str:<22} {skills_str:<25} {q_text}{flag_str}")
            
            logger.info(f"  {'─'*96}")
            logger.info(f"{'='*100}\n")
            
            final_analysis, analysis_data = generate_final_analysis_with_gpt(summary, interview_id, room_id, process_id)

            # ── Phase 3B: Post-interview baseline smoothing ──────────────
            try:
                from skill_adaptation import aggregate_interview_results
                smooth_session = AiraSession()
                # Query all completed evaluations for this interview
                eval_rows = smooth_session.query(InterviewQuestion).filter(
                    InterviewQuestion.interview_id == interview_id,
                    InterviewQuestion.evaluation_status == "completed",
                ).all()
                if eval_rows:
                    row_dicts = [
                        {
                            "skill": r.skill,
                            "confidence_score": r.confidence_score,
                            "effective_level": r.effective_level,
                            "evaluation_status": r.evaluation_status,
                        }
                        for r in eval_rows
                    ]
                    skill_agg = aggregate_interview_results(row_dicts)
                    for skill_name, agg in skill_agg.items():
                        update_skill_baseline(
                            smooth_session,
                            candidate_id=candidate_jd_id,
                            skill=skill_name,
                            interview_avg_confidence=agg["avg_confidence"],
                            interview_avg_complexity=agg["avg_complexity"],
                        )
                    logger.info(
                        "📈 [process id: %s][room id: %s] Phase 3B: Updated %d skill baselines for candidate %s",
                        process_id, room_id, len(skill_agg), candidate_jd_id,
                    )
                else:
                    logger.info(
                        "ℹ️ [process id: %s][room id: %s] Phase 3B: No completed evaluations to smooth",
                        process_id, room_id,
                    )
                smooth_session.close()
            except Exception as e:
                logger.warning(
                    "⚠️ [process id: %s][room id: %s] Phase 3B post-interview smoothing failed (non-blocking): %s",
                    process_id, room_id, e,
                )
            
            # Extract analyzed blocks for DB save
            analysis_blocks = analysis_data.get("analyzed_questions", []) if isinstance(analysis_data, dict) else analysis_data
            
            # Filter out blocks that were skipped/repeated or conversation-only blocks
            valid_analysis_blocks = []
            seen_questions = set()  # Track seen question texts to detect duplicates
            for block in analysis_blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("flag", False):
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Excluding flagged block from DB save: Q#{block.get('id', '?')}")
                    continue
                if not all(key in block for key in ["metrics", "analysis", "overall_score"]):
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] Excluding incomplete block from DB save: Q#{block.get('id', '?')}, has only: {list(block.keys())}")
                    continue
                
                # ✅ Exclude questions beyond Q15 (interview should end at Q15)
                block_id = block.get('id', 0)
                if block_id > config.questions_per_round + 2:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Excluding Q#{block_id} - beyond question limit")
                    continue
                
                # ✅ Exclude duplicate questions (same text as already processed)
                question_text = block.get('question', '').strip().lower()
                if question_text and question_text in seen_questions:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Excluding duplicate Q#{block_id} - question already processed")
                    continue
                if question_text:
                    seen_questions.add(question_text)
                
                valid_analysis_blocks.append(block)
            
            logger.info(f"[process id: {process_id}][room id: {room_id}] Filtered analysis blocks: Total={len(analysis_blocks)}, Valid={len(valid_analysis_blocks)}, Excluded={len(analysis_blocks) - len(valid_analysis_blocks)}")
            
            # Save analysis blocks
            try:
                if valid_analysis_blocks:
                    airadb.save_analysis_blocks(interview_id, valid_analysis_blocks, process_id, room_id)
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] No valid analysis blocks to save (all were flagged/incomplete)")
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Failed to save analysis blocks: {e}")
                logger.info(f"[process id: {process_id}][room id: {room_id}] Continuing with email generation despite save failure")
            
            # Save conversation timeline to database
            try:
                timeline_blocks = analysis_data.get("conversation_timeline", []) if isinstance(analysis_data, dict) else []
                if timeline_blocks:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Saving conversation timeline to database: {len(timeline_blocks)} items")
                    airadb.save_conversation_timeline(interview_id, timeline_blocks, process_id, room_id)
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] No conversation timeline to save")
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Failed to save conversation timeline: {e}")
                logger.info(f"[process id: {process_id}][room id: {room_id}] Continuing despite timeline save failure")
            
            # Candidate photo: captured by the engine's FaceDetector during the
            # interview (best frame after each ack) and written locally here — no
            # DB round-trip. Persisted to the DB at archive time.
            try:
                saved_path = _write_best_face_file(room_id, process_id)
                if saved_path:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Candidate photo ready for report: {saved_path}")
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] No best face captured for interview {interview_id}")
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Error writing candidate photo: {e}")
            
            # Handle audio/video issues
            if audio_video_issues:
                short, detailed = summarize_audio_video_issues(openai_client, audio_video_issues)
            else:
                short = ""
                detailed = ""

            image_path = f"images/{room_id}_best_face.png"
            try:
                if os.path.exists(image_path):
                    pass
                else:
                    short = short + " There was a technical issue while displaying candidate image."
            except Exception as e:
                logger.info(f"❌[process id: {process_id}][room id: {room_id}] Error while checking file: {e}")
            
            # Parse the JSON analysis
            json_final_analysis = json.loads(final_analysis)
            all_metrics = {}
            question_count = 0
            json_final_analysis["final_analysis"] = json_final_analysis.get("final_analysis", "") + short
            json_final_analysis["performance_overview"] = json_final_analysis.get("performance_overview", "") + detailed
            
            for block in analysis_blocks:
                if isinstance(block, dict) and block.get('metrics'):
                    question_count += 1
                    for metric in block['metrics']:
                        metric_name = metric['name']
                        metric_score = float(metric.get('score', 0))
                        if metric_name not in all_metrics:
                            all_metrics[metric_name] = {'total': 0, 'color': metric.get('color', '#666')}
                        all_metrics[metric_name]['total'] += metric_score
            
            # Calculate averages and create metrics list
            avg_metrics = []
            for metric_name, data in all_metrics.items():
                average_score = round(data['total'] / question_count, 1) if question_count > 0 else 0
                avg_metrics.append({
                    "name": metric_name,
                    "score": average_score,
                    "color": data['color']
                })
            
            def calculate_final_score(avg_metrics, number_of_questions=0, total_questions=config.questions_per_round):
                # Divide by total configured questions (not answered count)
                total_score = sum([(metric['score'] * number_of_questions) / total_questions for metric in avg_metrics])
                final_score = total_score / 6
                return round(final_score)
            
            json_final_analysis["final_score"] = calculate_final_score(avg_metrics, question_count)
            half_done_message = ""
            min_required = max(config.questions_per_round * 3 // 4, 3)  # At least 75% of configured questions
            if question_count < min_required:
                half_done_message = f"The candidate has not met the minimum question requirement for qualification. They answered {question_count} questions, while at least {min_required} out of {config.questions_per_round} are required to proceed."
            
            json_final_analysis["final_analysis"] = json_final_analysis.get("final_analysis", "") + half_done_message
            json_final_analysis["performance_overview"] = json_final_analysis.get("performance_overview", "") + half_done_message
            json_final_analysis["final_analysis"] = polish_text(json_final_analysis["final_analysis"])
            json_final_analysis["performance_overview"] = polish_text(json_final_analysis["performance_overview"])
            coverage_report = client.get_coverage_report()
            logger.info(f"[process id: {process_id}][room id: {room_id}] Final skill coverage: {coverage_report}")
            airadb.update_interview(interview_id, {
                "interview_score": json_final_analysis.get("final_score"),
                "interview_analysis": json_final_analysis.get("final_analysis"),
                # NOTE: interview_verdict is now calculated in interview_template_handler.py
                # after Skills metric override and final_score recalculation
                "interview_status": "Complete"
            }, process_id=process_id, room_id=room_id)

            # Duration and emails
            interview_duration_seconds = (actual_interview_end_time - interview_start_time).total_seconds()
            interview_duration_minutes = int(interview_duration_seconds // 60)
            interview_duration_remainder_seconds = int(interview_duration_seconds % 60)
            interview['actual_duration'] = (
                f"{interview_duration_minutes} minutes and {interview_duration_remainder_seconds} seconds"
                if interview_duration_minutes > 0
                else f"{interview_duration_remainder_seconds} seconds"
            )
            
            # =====================================================
            # AUTHORSHIP ANALYSIS - Generate AI detection summary
            # =====================================================
            try:
                logger.info(f"[process id: {process_id}][room id: {room_id}] Generating authorship analysis summary...")
                authorship_summary = airadb.generate_authorship_summary(
                    interview_id=interview_id,
                    process_id=process_id,
                    room_id=room_id
                )
                
                if authorship_summary and "error" not in authorship_summary:
                    # Save to database
                    airadb.update_interview(
                        interview_id, 
                        {"authorship_scores_summary": authorship_summary},
                        process_id=process_id, 
                        room_id=room_id
                    )
                    logger.info(
                        f"[process id: {process_id}][room id: {room_id}] ✅ Authorship summary saved: "
                        f"authenticity={authorship_summary.get('authenticity_score')}% ({authorship_summary.get('confidence_band')})"
                    )
                else:
                    logger.warning(
                        f"[process id: {process_id}][room id: {room_id}] ⚠️ Authorship summary not available: "
                        f"{authorship_summary.get('error', 'unknown')}"
                    )
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] ❌ Failed to generate authorship summary: {e}")
                import traceback
                logger.error(f"[process id: {process_id}][room id: {room_id}] Authorship error traceback: {traceback.format_exc()}")
            
            html_data = {}
            try:
                if EMAIL_ENABLED and email_client:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Sending interview summary email...")
                    html_data=email_client.send_interview_summary_email(
                        interview=interview,
                        json_final_analysis=json_final_analysis,
                        analysis_blocks=analysis_data,
                        process_id=process_id,
                        room_id=room_id
                    )
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Summary email sent successfully")
                    if html_data:
                        logger.info(f"[process id: {process_id}][room id: {room_id}] Received HTML data from email function:")
                        if 'summary_html' in html_data:
                            logger.info(f"  - Summary HTML: {len(html_data['summary_html'])} characters")
                        if 'detailed_html' in html_data:
                            logger.info(f"  - Detailed HTML: {len(html_data['detailed_html'])} characters")
                        if 'sentinel_html' in html_data:
                            logger.info(f"  - Sentinel HTML: {len(html_data['sentinel_html'])} characters")
                        
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Sending interview feedback email...")
                    email_client.send_interview_feedback_email(interview,DEPLOYMENT)
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Feedback email sent successfully")
                    time.sleep(3)
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] Email disabled or client not available")
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Failed to send emails: {e}")
                import traceback
                logger.error(f"[process id: {process_id}][room id: {room_id}] Email error traceback: {traceback.format_exc()}")
            
            # ═══════════════════════════════════════════════════════════════
            # ✅ SAVE STRUCTURED REPORT DATA — INDEPENDENT OF EMAIL PIPELINE
            # This runs regardless of whether emails are enabled/succeeded.
            # ═══════════════════════════════════════════════════════════════
            try:
                report_data_dict = None
                
                # Try to get report data from the email pipeline first (template-processed)
                if html_data and isinstance(html_data, dict):
                    report_data_dict = html_data.get("report_data_dict")
                
                # Fallback: Build report data directly from raw analysis if email pipeline didn't produce it
                if not report_data_dict:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Building report data from raw analysis (email pipeline didn't produce it)...")
                    
                    # Extract strengths/weaknesses from analysis blocks
                    all_strengths = []
                    all_weaknesses = []
                    questions_data_list = []
                    for block in (analysis_blocks if isinstance(analysis_blocks, list) else []):
                        if isinstance(block, dict) and not block.get("flag", False):
                            all_strengths.extend(block.get("strengths", []))
                            all_weaknesses.extend(block.get("weaknesses", []))
                            questions_data_list.append({
                                "id": block.get("id"),
                                "question": block.get("question"),
                                "answer": block.get("answer"),
                                "analysis": block.get("analysis"),
                                "overall_score": block.get("overall_score"),
                                "metrics": [{"name": m.get("name"), "score": m.get("score", 0)} for m in block.get("metrics", [])],
                                "strengths": block.get("strengths", []),
                                "weaknesses": block.get("weaknesses", []),
                                "flag": block.get("flag", False),
                            })
                    
                    # Build conversation timeline
                    timeline_data = analysis_data.get("conversation_timeline", []) if isinstance(analysis_data, dict) else []

                    # CAMPUS Phase 5: faculty evaluation — per-skill mastery, progression,
                    # formative recommendations. Reframes the hire/no-hire verdict into a
                    # learning-readiness summary for faculty + students.
                    try:
                        from campus.evaluation import skill_records_from, build_faculty_evaluation
                        _qhist = getattr(getattr(client, "skill_tracker", None), "_question_history", {}) or {}
                        _skill_records = skill_records_from(analysis_blocks, _qhist)
                        faculty_eval = build_faculty_evaluation(_skill_records)
                        logger.info(f"[process id: {process_id}][room id: {room_id}] 🎓 Faculty eval: "
                                    f"{faculty_eval['summary']['headline']} | recs={len(faculty_eval['recommendations'])}")
                    except Exception as _fe:
                        logger.warning(f"[process id: {process_id}][room id: {room_id}] Faculty eval failed (non-blocking): {_fe}")
                        faculty_eval = None
                    _readiness = (faculty_eval or {}).get("summary", {}) if faculty_eval else {}

                    report_data_dict = {
                        "interview_duration": interview.get("actual_duration"),
                        "questions_asked_count": question_count,
                        "total_questions_configured": config.questions_per_round if config else 15,
                        "resume_analysis_score": interview.get("resume_match_score", 0.0),
                        "resume_analysis_text": interview.get("resume_job_analysis"),
                        "interview_final_score": json_final_analysis.get("final_score", 0.0),
                        "interview_analysis_text": json_final_analysis.get("final_analysis"),
                        # CAMPUS: learning-readiness, not hire/no-hire.
                        "faculty_evaluation": faculty_eval,
                        "readiness_level": _readiness.get("level"),
                        "verdict_passed": json_final_analysis.get("final_score", 0) >= 3,
                        "verdict_status": (_readiness.get("headline")
                                           or f"Readiness: {_readiness.get('level', 'Beginning')}"),
                        "overall_assessment": json_final_analysis.get("performance_overview"),
                        "strengths": all_strengths[:10] if all_strengths else [],
                        "weaknesses": all_weaknesses[:10] if all_weaknesses else [],
                        "performance_summary": json_final_analysis.get("performance_overview"),
                        "avg_metrics": [{"name": m.get("name"), "score": m.get("score", 0)} for m in avg_metrics],
                        "questions_data": questions_data_list,
                        "conversation_timeline": timeline_data,
                        "has_mandatory_skills": bool(interview.get("has_mandatory_skills")),
                        "mandatory_skills_text": interview.get("mandatory_skills"),
                        "mandatory_skills_evaluation": None,
                        "authorship_analysis": interview.get("authorship_scores_summary") if isinstance(interview.get("authorship_scores_summary"), dict) else None,
                        "behavioral_metrics": None,
                        "json_final_analysis": json_final_analysis,
                        "analysis_blocks_raw": analysis_blocks if isinstance(analysis_blocks, list) else [],
                    }
                
                if report_data_dict:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] 💾 Saving structured report data to interview_report_data table...")
                    airadb.save_interview_report_data(
                        interview_id=interview_id,
                        report_data=report_data_dict,
                        process_id=process_id,
                        room_id=room_id
                    )
                    logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Structured report data saved successfully (interview_id={interview_id})")
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] ⚠️ No report data available to save")
                    
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] ❌ Failed to save structured report data: {e}")
                import traceback
                logger.error(f"[process id: {process_id}][room id: {room_id}] Report data save traceback: {traceback.format_exc()}")
            
            end = time.time()
            elapsed = end - start
            logger.info(f"[process id: {process_id}][room id: {room_id}] Execution time (time taken to complete analysis and send emails...): {elapsed:.6f} seconds")
            
            # Persist the best face to the DB BEFORE deleting the local file, so
            # archive_and_delete_interview copies it into interview_history. The
            # orchestrator's archive-time persist runs AFTER this cleanup, so if we
            # delete first it finds nothing — that's why best_face showed up in the
            # report (embedded from this file) but never in the archive table.
            image_path = f"images/{room_id}_best_face.png"
            try:
                _bf_bytes = read_best_face_bytes(room_id)
                if _bf_bytes:
                    airadb.update_interview(interview_id, {"best_face": _bf_bytes},
                                            process_id=process_id, room_id=room_id)
                    logger.info(f"🛡[process id: {process_id}][room id: {room_id}] Best face persisted to DB before cleanup ({len(_bf_bytes)} bytes)")
                else:
                    logger.info(f"🛡[process id: {process_id}][room id: {room_id}] No best face captured — nothing to persist")
            except Exception as e:
                logger.warning(f"⚠️[process id: {process_id}][room id: {room_id}] best_face DB persist failed: {e}")

            # Clean up the local image file (already persisted above + embedded in report)
            try:
                if os.path.exists(image_path):
                    os.remove(image_path)
                    logger.info(f"✅[process id: {process_id}][room id: {room_id}] Deleted existing file: {image_path}")
                else:
                    logger.info(f"⚠️[process id: {process_id}][room id: {room_id}] File not found: {image_path}")
            except Exception as e:
                logger.info(f"❌[process id: {process_id}][room id: {room_id}] Error while deleting file: {e}")

            # ═══════════════════════════════════════════════════════════════
            # Candidate live data cleanup (after all rounds complete)
            # ═══════════════════════════════════════════════════════════════
            try:
                if current_round >= config.total_rounds and candidate_jd_id:
                    logger.info(
                        "[process id: %s][room id: %s] 🧹 Final round %d/%d complete — cleaning up live data for candidate %s",
                        process_id, room_id, current_round, config.total_rounds, candidate_jd_id,
                    )
                    cleanup_result = cleanup_candidate_live_data(
                        candidate_id=candidate_id,
                        job_id=job_id,
                        process_id=process_id,
                        room_id=room_id,
                    )
                    logger.info(
                        "[process id: %s][room id: %s] 🧹 Cleanup result: %s",
                        process_id, room_id, cleanup_result,
                    )
                else:
                    logger.info(
                        "[process id: %s][room id: %s] ⏳ Round %d/%d — live data retained for next round",
                        process_id, room_id, current_round, config.total_rounds,
                    )
            except Exception as e:
                logger.error(
                    "[process id: %s][room id: %s] ❌ Candidate cleanup failed (non-blocking): %s",
                    process_id, room_id, e,
                )
        
        else:
            # Handle case where sentinel status is not OK
            ai_speak(f"Welcome {candidate_name}, I'm sorry to inform you that this interview is terminated as there was a technical issue in the backend. We will get back to you as soon as possible.", driver, room_id, process_id)
            try:
                json_final_analysis = {
                    "final_analysis": "Interview terminated due to technical issue.",
                    "final_score": 0,
                    "performance_overview": "Interview terminated due to technical issue."
                }
                analysis_blocks = []
                if EMAIL_ENABLED and email_client:
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Sending fallback summary email due to technical issue...")
                    email_client.send_interview_summary_email(
                        interview=interview,
                        json_final_analysis=json_final_analysis,
                        analysis_blocks=analysis_blocks,
                        process_id=process_id,
                        room_id=room_id
                    )
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Sending fallback feedback email...")
                    email_client.send_interview_feedback_email(interview,DEPLOYMENT)
                    time.sleep(2)
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Failed to send fallback emails: {e}")
                import traceback
                logger.error(f"[process id: {process_id}][room id: {room_id}] Fallback email error traceback: {traceback.format_exc()}")
            
            logger.info(f"❌[process id: {process_id}][room id: {room_id}] Interview terminated due to technical issues.")
    
    finally:
        # Stop all background threads
        if thread_manager:
            thread_manager.stop_threads()
        
        # Clean-up
        try:
            end_meeting_for_all(room_id, process_id=process_id)
        except Exception:
            pass
        







# =============================================================================
# BARGE-IN AUDIO PLAYER: Play ack audio + monitor candidate mic simultaneously
# If candidate speaks during ack, stops playback and captures their speech.
# Returns: { success, interrupted, transcript (if interrupted), playedMs }
# =============================================================================




# =============================================================================
# STREAMING PIPELINE: GPT text stream → sentence-chunked TTS → WebRTC audio
# Uses producer-consumer pattern for true overlap:
#   Producer thread: GPT tokens → sentence buffer → fire TTS in thread pool
#   Consumer (main): pull completed TTS audio → play through WebRTC
# All three stages overlap: GPT generates while TTS fires while audio plays
# =============================================================================
















