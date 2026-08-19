
import argparse
import time
import json
import os
import warnings
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import integrations.aira_sql as airadb
import webrtc.interview as webrtc_interview   # LiveKit interview engine (set_livekit_adapter / ask_questions_in_webrtc / read_best_face_bytes)
import pymysql
import multiprocessing as mp
import shutil
import random
import requests
import threading
import logging
from openai import OpenAI
import integrations.MSEmailClient as email_client

# Suppress cryptography deprecation warnings from paramiko
warnings.filterwarnings("ignore", category=DeprecationWarning, module="paramiko")
warnings.filterwarnings("ignore", message=".*TripleDES.*")

load_dotenv(override=True)
from logger_config import setup_logging
setup_logging()
logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
from env_config import DEPLOYMENT, SENTINEL_BASE_URL, CONNECT_BASE_URL
logger.info(f"🔧 Environment: {DEPLOYMENT} | Sentinel: {SENTINEL_BASE_URL} | Connect: {CONNECT_BASE_URL}")

# EC2 SSH Details
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
        logging.info("🔗 EC2_HOST is None/empty - connecting directly to localhost")
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

ACCESS_TOKEN = None

# Credentials and URL
JD_CLIENT_ID = os.getenv("JOBDIVA_CLIENT_ID")
JD_USERNAME = os.getenv("JOBDIVA_USERNAME")
JD_PASSWORD = os.getenv("JOBDIVA_PASSWORD")
JD_AUTH_URL = os.getenv("JOBDIVA_AUTH_URL")
JD_API_BASE = os.getenv("JOBDIVA_API_BASE")
JD_TIMEZONE = os.getenv("JOBDIVA_TIMEZONE")


def get_headers():
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

def authenticate():
    global ACCESS_TOKEN

    params = {
        "clientid": JD_CLIENT_ID,
        "username": JD_USERNAME,
        "password": JD_PASSWORD
    }

    try:
        response = requests.get(JD_AUTH_URL, params=params)
        response.raise_for_status()
        ACCESS_TOKEN = response.text.strip('"')  # Remove quotes if response is a string token
        # logger.info("Access token obtained successfully.")
        # logger.info(ACCESS_TOKEN)
    except requests.RequestException as e:
        logger.error(f"Failed to authenticate: {e}")        
# === CONFIG ===
# COMPANY_ID = '11307088'
OUTPUT_DIR = "candidate_analysis"
os.makedirs(OUTPUT_DIR, exist_ok=True)


def extract_room_id_from_url(url):
    import re
    if not url:  # guard against None/empty
        return None
    match = re.search(r'roomId=([\w-]+)', url)
    if match:
        return match.group(1)
    # Try to extract from JWT if present
    import base64
    import json
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if 't' in qs:
        jwt = qs['t'][0]
        try:
            payload = jwt.split('.')[1]
            # Pad base64
            payload += '=' * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return data.get('roomId')
        except Exception:
            pass
    return None

def validate_jwt_token(webrtc_url,process_id,room_id):
    """Validate JWT token in WebRTC URL and return token details"""
    import base64
    import json
    from urllib.parse import urlparse, parse_qs
    from datetime import datetime, timezone
    
    try:
        logger.info("🔐[process id: %s][room id: %s] Starting JWT token validation...",process_id,room_id)
        
        # Extract JWT from URL
        parsed_url = urlparse(webrtc_url)
        query_params = parse_qs(parsed_url.query)
        
        # Look for JWT in common parameter names
        jwt_token = None
        jwt_param_name = None
        
        for param_name in ['t', 'token', 'jwt', 'auth']:
            if param_name in query_params:
                jwt_token = query_params[param_name][0]
                jwt_param_name = param_name
                break
        
        if not jwt_token:
            logger.error("❌[process id: %s][room id: %s] No JWT token found in WebRTC URL",process_id,room_id)
            logger.error("🔍[process id: %s][room id: %s] Available parameters: %s",process_id,room_id, list(query_params.keys()))
            return False, "No JWT token found in URL"
        
        logger.info("✅[process id: %s][room id: %s] JWT token found in parameter: %s",process_id,room_id, jwt_param_name)
        logger.info("🔍[process id: %s][room id: %s] JWT token (first 20 chars): %s...",process_id,room_id, jwt_token[:20])
        
        # Parse JWT token
        try:
            # Split JWT into parts
            parts = jwt_token.split('.')
            if len(parts) != 3:
                logger.error("❌[process id: %s][room id: %s] Invalid JWT format: expected 3 parts, got %d",process_id,room_id, len(parts))
                return False, f"Invalid JWT format: {len(parts)} parts"
            
            header_b64, payload_b64, signature_b64 = parts
            
            # Decode header
            try:
                # Pad base64 if needed
                header_b64 += '=' * (-len(header_b64) % 4)
                header_data = json.loads(base64.urlsafe_b64decode(header_b64))
                logger.info("🔖[process id: %s][room id: %s] JWT Header: %s",process_id,room_id, header_data)
            except Exception as header_error:
                logger.warning("⚠️[process id: %s][room id: %s] Could not decode JWT header: %s",process_id,room_id, header_error)
                header_data = {}
            
            # Decode payload
            try:
                # Pad base64 if needed
                payload_b64 += '=' * (-len(payload_b64) % 4)
                payload_data = json.loads(base64.urlsafe_b64decode(payload_b64))
                logger.info("📋[process id: %s][room id: %s] JWT Payload: %s",process_id,room_id, payload_data)
            except Exception as payload_error:
                logger.error("❌[process id: %s][room id: %s] Could not decode JWT payload: %s",process_id,room_id, payload_error)
                return False, f"Invalid JWT payload: {payload_error}"
            
            # Validate token expiration
            current_time = datetime.now(timezone.utc).timestamp()
            
            if 'exp' in payload_data:
                exp_time = payload_data['exp']
                exp_datetime = datetime.fromtimestamp(exp_time, timezone.utc)
                logger.info("⏰[process id: %s][room id: %s] Token expires at: %s UTC",process_id,room_id, exp_datetime.strftime('%Y-%m-%d %H:%M:%S'))
                
                if current_time > exp_time:
                    logger.error("❌[process id: %s][room id: %s] JWT token has EXPIRED!",process_id,room_id)
                    logger.error("⏰[process id: %s][room id: %s] Current time: %s UTC",process_id,room_id, 
                               datetime.fromtimestamp(current_time, timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
                    logger.error("⏰[process id: %s][room id: %s] Expired at: %s UTC",process_id,room_id, exp_datetime.strftime('%Y-%m-%d %H:%M:%S'))
                    return False, "JWT token has expired"
                else:
                    time_until_expiry = exp_time - current_time
                    logger.info("✅[process id: %s][room id: %s] Token is valid for %.1f minutes",process_id,room_id, time_until_expiry / 60)
            else:
                logger.warning("⚠️[process id: %s][room id: %s] No expiration time (exp) found in JWT token",process_id,room_id)
            
            # Validate issued at time
            if 'iat' in payload_data:
                iat_time = payload_data['iat']
                iat_datetime = datetime.fromtimestamp(iat_time, timezone.utc)
                logger.info("🕐[process id: %s][room id: %s] Token issued at: %s UTC",process_id,room_id, iat_datetime.strftime('%Y-%m-%d %H:%M:%S'))
                
                if iat_time > current_time:
                    logger.error("❌[process id: %s][room id: %s] JWT token issued in the future!",process_id,room_id)
                    return False, "JWT token issued in future"
            
            # Log important claims
            important_claims = ['sub', 'aud', 'iss', 'roomId', 'userId', 'role', 'name']
            for claim in important_claims:
                if claim in payload_data:
                    logger.info("🏷️ JWT %s: %s", claim, payload_data[claim])
            
            # Validate room ID matches
            if 'roomId' in payload_data:
                jwt_room_id = payload_data['roomId']
                url_room_id = extract_room_id_from_url(webrtc_url)
                if jwt_room_id != url_room_id:
                    logger.warning("⚠️[process id: %s][room id: %s] Room ID mismatch - JWT: %s, URL: %s",process_id,room_id, jwt_room_id, url_room_id)
                else:
                    logger.info("✅[process id: %s][room id: %s] Room ID matches between JWT and URL: %s",process_id,room_id, jwt_room_id)
            
            # Check if JWT allows bot role
            jwt_role = payload_data.get('role', '').lower()
            if jwt_role and jwt_role not in ['bot', 'admin', 'interviewer']:
                logger.warning("⚠️[process id: %s][room id: %s] JWT role '%s' may not have bot permissions",process_id,room_id, jwt_role)
            else:
                logger.info("✅[process id: %s][room id: %s] JWT role compatible with bot operation: %s",process_id,room_id, jwt_role or 'not specified')
            
            # Validate required claims for WebRTC
            required_claims = ['roomId']
            missing_claims = [claim for claim in required_claims if claim not in payload_data]
            if missing_claims:
                logger.error("❌[process id: %s][room id: %s] Missing required JWT claims: %s",process_id,room_id, missing_claims)
                return False, f"Missing required claims: {', '.join(missing_claims)}"
            
            logger.info("✅ JWT token validation successful")
            return True, "Valid JWT token"
            
        except Exception as jwt_parse_error:
            logger.error("❌[process id: %s][room id: %s] JWT parsing failed: %s",process_id,room_id, jwt_parse_error)
            return False, f"JWT parsing error: {jwt_parse_error}"
            
    except Exception as validation_error:
        logger.error("❌[process id: %s][room id: %s] JWT validation error: %s",process_id,room_id, validation_error)
        return False, f"Validation error: {validation_error}"






def ensure_bot_role_in_url(webrtc_url,room_id,process_id):
    """Ensure the WebRTC URL includes bot role parameter"""
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    
    parsed = urlparse(webrtc_url)
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    
    # Add role=bot parameter if not present
    if 'role' not in query_params:
        query_params['role'] = ['bot']
        logger.info("🤖[process id: %s][room id: %s] Added role=bot parameter to WebRTC URL",process_id,room_id)
    else:
        existing_role = query_params.get('role', [None])[0]
        if existing_role != 'bot':
            query_params['role'] = ['bot']
            logger.info(f"🤖[process id: {process_id}][room id: {room_id}] Changed role from '{existing_role}' to 'bot' in WebRTC URL")
        else:
            logger.info("✅[process id: %s][room id: %s] WebRTC URL already has role=bot parameter",process_id,room_id)
    
    # Add bot name parameter if not present
    if 'name' not in query_params:
        query_params['name'] = ['AIRA Interview Bot']
        logger.info("🤖[process id: %s][room id: %s] Added name='AIRA Interview Bot' parameter to WebRTC URL",process_id,room_id)
    
    # Reconstruct URL with modified parameters
    new_query = urlencode(query_params, doseq=True)
    modified_parsed = parsed._replace(query=new_query)
    modified_url = urlunparse(modified_parsed)
    
    return modified_url

def get_room_participants(room_id,base_url):
    try:
        api_url = f"{base_url}/api/rooms/{room_id}"
        resp = requests.get(api_url, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get('participants', [])
        else:
            logger.warning(f"Room API returned {resp.status_code} for room {room_id}")
            return []
    except Exception as e:
        logger.warning(f"Could not get room participants: {e}")
        return []

def log_jobdiva_note(candidate_id, job_id, action, note_text):   
    authenticate()  
    url = f"{JD_API_BASE}/jobdiva/createCandidateNote"
    params = {
        "candidateid": candidate_id,
        "note": note_text,
        "action": action,
        "link2AnOpenJob": job_id,
        "setAsAuto": "true"
    }
    logger.info(f"Adding note to JobDiva for candidate {candidate_id} with job ID {job_id}")
    logger.info(f"Note text: {note_text}")
    try:
        response = requests.post(url, headers=get_headers(), params=params)
        if response.status_code == 200:
            logger.info(f"📝 Note added to JobDiva for candidate {candidate_id}")
        else:
            logger.info(f"❌ Failed to update note for candidate {candidate_id}: {response.text}")
    except Exception as e:
        logger.info(f"❌ Error updating JobDiva note: {e}")

def end_meeting_for_all(room_id: str, timeout: int = 5,process_id=0,base_url="") -> bool:
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
    


def wait_for_participant(interview,base_url ,room_id, process_id, role="candidate", timeout=600):
    logger.info(f"🔍[process id: {process_id}][room id: {room_id}] Waiting for participant with role '{role}' in room: {room_id}")
    start = time.time()
    interview_id     = interview['id']
    candidate_name   = interview['candidate_name']
    interview_time   = interview['interview_time']
    candidate_jd_id  = interview['candidate_jd_id']
    job_jd_id        = interview['job_jd_id']
    candidate_id=interview['candidate_id']
    while time.time() - start < timeout:
        # Prefer the bot's NATIVE LiveKit presence (the bot is in the room and sees
        # participants directly). Robust when Connect's /api/rooms can't report live
        # participants (e.g. a LiveKit key mismatch). Falls back to the Connect poll below.
        if role == "candidate":
            try:
                _adp = webrtc_interview.get_livekit_adapter()
                if _adp is not None and getattr(_adp, "candidate_present", False):
                    logger.info(f"✅[process id: {process_id}][room id: {room_id}] Candidate detected natively via LiveKit adapter.")
                    return True
            except Exception:
                pass
        participants = get_room_participants(room_id,base_url)
        for p in participants:
            if p.get('role') == role:
                logger.info(f"✅[process id: {process_id}][room id: {room_id}] Participant '{p.get('name')}' with role '{role}' joined.")
                return True
        logger.info(f"[process id: {process_id}][room id: {room_id}]...waiting for {role}...")
        time.sleep(5)
    logger.warning(f"⏰[process id: {process_id}][room id: {room_id}] Timeout waiting for {role} in room {room_id}")
    try:
        end_meeting_for_all(room_id=room_id, base_url=base_url,process_id=process_id)
        logger.error("❌ No candidate joined. Terminating interview session.")

        logger.info("🛑 Interview terminated due to no candidate joining.")
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
            return target_dt   # return datetime, not str

        converted = convert_timezone(interview["interview_time"], interview["interview_timezone"], interview["zone"])
        interview["interview_time"] = converted
        if interview["zone"]:
            interview["interview_timezone"] = interview["zone"]
                # --- Email Notification ---
        email_client.send_candidate_absence_notification(interview)

        logger.info("📧 Candidate absence email sent successfully.")

        log_jobdiva_note(
            candidate_jd_id,
            job_jd_id,
            "AIRA: Interview Terminated",
            f"Terminated - Participant {candidate_name} did not join interview"
        )

        airadb.update_interview(interview_id, {
            "interview_status": "Terminated - Participant did not join interview"
        }, process_id=f"terminator-{room_id}", room_id=room_id)

    except Exception as e:
        logger.exception(f"⚠ Error during no-candidate termination handling: {e}")

    return False

def start_room(room_id: str,interview_id:str):
    url = f"{SENTINEL_BASE_URL}/rooms/start/{room_id}_{interview_id}"
    logger.info(url)
    try:
        response = requests.post(url)
        logger.info(response)
        response.raise_for_status()  # Raise an exception for HTTP errors
        return response.json()  # Return the JSON response from the API
    except requests.exceptions.RequestException as e:
        print(f"Error starting room {room_id}: {e}")
        return {"ok": False, "error": str(e)}


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


def rsync_recording_to_sentinel(video_path: str, process_id=0, room_id="", max_attempts=None):
    """Copy the finished recording to the Sentinel VM's recordings dir via rsync,
    so the Sentinel can find it before /process is called. This is the in-code
    replacement for the old sync_webm.sh watcher (which is NOT deployed). Bounded
    retries (so the worker can't hang forever) — tune with SENTINEL_RSYNC_MAX_ATTEMPTS.
    Recordings orphaned by a hard crash or an all-retries-failed rsync are recovered
    on the next run by _sweep_orphan_recordings(). Returns True on success."""
    import subprocess
    if max_attempts is None:
        max_attempts = int(os.getenv("SENTINEL_RSYNC_MAX_ATTEMPTS", "5"))
    if not video_path or not os.path.exists(video_path):
        logger.warning("📤[process id: %s][room id: %s] rsync skipped — file missing: %s",
                       process_id, room_id, video_path)
        return False

    dest = os.getenv("SENTINEL_RSYNC_DEST",
                     "root@3.230.103.46:/home/ubuntu/Sentinel/recordings/")
    cmd = ["rsync", "-avz", "--timeout=60", video_path, dest]
    fname = os.path.basename(video_path)

    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("📤[process id: %s][room id: %s] rsync OK (attempt %d): %s → %s",
                            process_id, room_id, attempt, fname, dest)
                return True
            logger.warning("📤[process id: %s][room id: %s] rsync attempt %d failed (rc=%d): %s",
                           process_id, room_id, attempt, result.returncode,
                           (result.stderr or "")[-300:])
        except Exception as e:
            logger.warning("📤[process id: %s][room id: %s] rsync attempt %d error: %s",
                           process_id, room_id, attempt, e)
        if attempt < max_attempts:
            time.sleep(10)

    logger.error("📤[process id: %s][room id: %s] rsync FAILED after %d attempts — skipping Sentinel /process for %s",
                 process_id, room_id, max_attempts, fname)
    return False


def rsync_recording_to_sentinel(video_path: str, process_id=0, room_id="", max_attempts=None):
    """Copy the finished recording to the Sentinel VM's recordings dir via rsync,
    so the Sentinel can find it before /process is called. This is the in-code
    replacement for the old sync_webm.sh watcher (which is NOT deployed). Bounded
    retries (so the worker can't hang forever) — tune with SENTINEL_RSYNC_MAX_ATTEMPTS.
    Recordings orphaned by a hard crash or an all-retries-failed rsync are recovered
    on the next run by _sweep_orphan_recordings(). Returns True on success."""
    import subprocess
    if max_attempts is None:
        max_attempts = int(os.getenv("SENTINEL_RSYNC_MAX_ATTEMPTS", "5"))
    if not video_path or not os.path.exists(video_path):
        logger.warning("📤[process id: %s][room id: %s] rsync skipped — file missing: %s",
                       process_id, room_id, video_path)
        return False

    dest = os.getenv("SENTINEL_RSYNC_DEST",
                     "root@3.230.103.46:/home/ubuntu/Sentinel/recordings/")
    cmd = ["rsync", "-avz", "--timeout=60", video_path, dest]
    fname = os.path.basename(video_path)

    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("📤[process id: %s][room id: %s] rsync OK (attempt %d): %s → %s",
                            process_id, room_id, attempt, fname, dest)
                return True
            logger.warning("📤[process id: %s][room id: %s] rsync attempt %d failed (rc=%d): %s",
                           process_id, room_id, attempt, result.returncode,
                           (result.stderr or "")[-300:])
        except Exception as e:
            logger.warning("📤[process id: %s][room id: %s] rsync attempt %d error: %s",
                           process_id, room_id, attempt, e)
        if attempt < max_attempts:
            time.sleep(10)

    logger.error("📤[process id: %s][room id: %s] rsync FAILED after %d attempts — skipping Sentinel /process for %s",
                 process_id, room_id, max_attempts, fname)
    return False


def rsync_recording_to_sentinel(video_path: str, process_id=0, room_id="", max_attempts=5):
    """Copy the finished recording to the Sentinel VM's recordings dir via rsync,
    so the Sentinel can find it before /process is called. Replaces the standalone
    sync_webm.sh watcher. Bounded retries (so the worker can't hang forever).
    Returns True on success."""
    import subprocess
    if not video_path or not os.path.exists(video_path):
        logger.warning("📤[process id: %s][room id: %s] rsync skipped — file missing: %s",
                       process_id, room_id, video_path)
        return False

    dest = os.getenv("SENTINEL_RSYNC_DEST",
                     "root@134.195.138.133:/home/ubuntu/Sentinel/recordings/")
    cmd = ["rsync", "-avz", "--timeout=60", video_path, dest]
    fname = os.path.basename(video_path)

    for attempt in range(1, max_attempts + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                logger.info("📤[process id: %s][room id: %s] rsync OK (attempt %d): %s → %s",
                            process_id, room_id, attempt, fname, dest)
                return True
            logger.warning("📤[process id: %s][room id: %s] rsync attempt %d failed (rc=%d): %s",
                           process_id, room_id, attempt, result.returncode,
                           (result.stderr or "")[-300:])
        except Exception as e:
            logger.warning("📤[process id: %s][room id: %s] rsync attempt %d error: %s",
                           process_id, room_id, attempt, e)
        if attempt < max_attempts:
            time.sleep(10)

    logger.error("📤[process id: %s][room id: %s] rsync FAILED after %d attempts — skipping Sentinel /process for %s",
                 process_id, room_id, max_attempts, fname)
    return False


def process_recording(room_id: str, interview_id, video_path: str,
                      sentinel_enabled=True, process_id=0):
    """POST the finished recording to the Sentinel /process endpoint for
    post-interview analysis. Called once the .webm has been muxed.

    Payload matches the Sentinel contract:
        {interview_id, room_id, video_filename, source, sentinel_enabled}
    Non-fatal: logs and returns on any failure."""
    if not video_path:
        logger.info("🛡[process id: %s][room id: %s] No recording to send to Sentinel /process.",
                    process_id, room_id)
        return {"ok": False, "error": "no video_path"}

    payload = {
        "interview_id": str(interview_id),
        "room_id": room_id,
        "video_filename": os.path.basename(video_path),
        "source": "hr-hub",
        "sentinel_enabled": True,
    }
    url = f"{SENTINEL_BASE_URL}/process"
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        logger.info("🛡[process id: %s][room id: %s] Sentinel /process accepted: %s",
                    process_id, room_id, str(response.text)[:300])
        return response.json() if response.content else {"ok": True}
    except requests.exceptions.RequestException as e:
        logger.error("❌[process id: %s][room id: %s] Sentinel /process failed: %s",
                     process_id, room_id, e)
        return {"ok": False, "error": str(e)}


def _sweep_orphan_recordings(process_id=0):
    """Self-healing backstop: ship recordings left behind by prior runs.

    The per-interview finally ships on every normal ending, but it can't cover a hard
    process kill BEFORE the finally ran (only intermediate files on disk, un-muxed) or
    an rsync that failed all retries (muxed file stranded). Run at orchestrator startup,
    this sweeps RECORDINGS_DIR: re-mux crash-orphans if needed, then rsync → /process →
    delete. mtime-guarded (SENTINEL_SWEEP_STALE_S, default 600s) so an in-progress
    recording from a concurrent worker is never touched. Idempotent and best-effort.

    Filenames: base = "{room_id}_{interview_id}"; final = "{base}_{isoTs}Z.webm" or the
    audio fallback "{base}.wav"; intermediates = "{base}_video.webm|_candidate.wav|_bot.wav".
    """
    try:
        from local_recorder import RECORDINGS_DIR, LocalVideoRecorder
    except Exception as e:
        logger.warning("🧹[sweep] local_recorder import failed: %s", e)
        return

    rec_dir = os.path.abspath(RECORDINGS_DIR)
    if not os.path.isdir(rec_dir):
        return
    stale_s = int(os.getenv("SENTINEL_SWEEP_STALE_S", "600"))
    now = time.time()

    _INTER = ("_video.mp4", "_video.webm", "_candidate.wav", "_bot.wav")
    bases = {}   # base -> {"finals": [...], "inters": [...], "newest": mtime}
    try:
        names = os.listdir(rec_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(rec_dir, name)
        if not os.path.isfile(path):
            continue
        base = None
        kind = None
        matched_inter = next((s for s in _INTER if name.endswith(s)), None)
        if matched_inter:
            base, kind = name[:-len(matched_inter)], "inter"
        elif name.endswith("Z.mp4") or name.endswith("Z.webm"):   # muxed video final
            base, kind = name.rsplit("_", 1)[0], "final"
        elif name.endswith(".wav"):             # merged audio-only fallback "{base}.wav"
            base, kind = name[:-4], "final"
        else:
            continue
        if not base:
            continue
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        e = bases.setdefault(base, {"finals": [], "inters": [], "newest": 0.0})
        (e["finals"] if kind == "final" else e["inters"]).append(path)
        e["newest"] = max(e["newest"], mt)

    for base, e in bases.items():
        # Skip anything an active worker may still be writing / just finalized.
        if now - e["newest"] < stale_s:
            continue
        parts = base.split("_")
        if len(parts) < 2:
            continue
        room_id, interview_id = parts[0], parts[1]
        try:
            ship_path = e["finals"][0] if e["finals"] else None
            if not ship_path:
                # Crash orphan → re-mux the intermediates into a final file.
                ship_path = LocalVideoRecorder(room_id, interview_id, process_id).salvage()
            if not ship_path or not os.path.exists(ship_path):
                continue
            logger.info("🧹[sweep] Orphan recording for %s → shipping %s",
                        base, os.path.basename(ship_path))
            if rsync_recording_to_sentinel(ship_path, process_id, room_id):
                process_recording(room_id=room_id, interview_id=interview_id,
                                  video_path=ship_path, sentinel_enabled=True,
                                  process_id=process_id)
                # NOTE: intentionally NOT deleting local files after rsync.
                # Recordings are kept on disk for reliability.
                logger.info("🧹[sweep] Orphan %s shipped — keeping local copy", base)
            else:
                logger.warning("🧹[sweep] rsync failed for %s — leaving for next run", base)
        except Exception as _sweep_err:
            logger.warning("🧹[sweep] error handling orphan %s: %s", base, _sweep_err)


def process_recording(room_id: str, interview_id, video_path: str,
                      sentinel_enabled=True, process_id=0):
    """POST the finished recording to the Sentinel /process endpoint for
    post-interview analysis. Called once the .webm has been muxed.

    Payload matches the Sentinel contract:
        {interview_id, room_id, video_filename, source, sentinel_enabled}
    Non-fatal: logs and returns on any failure."""
    if not video_path:
        logger.info("🛡[process id: %s][room id: %s] No recording to send to Sentinel /process.",
                    process_id, room_id)
        return {"ok": False, "error": "no video_path"}

    payload = {
        "interview_id": str(interview_id),
        "room_id": room_id,
        "video_filename": os.path.basename(video_path),
        "source": os.getenv("SENTINEL_PROCESS_SOURCE", "engine"),
        "sentinel_enabled": bool(sentinel_enabled),
    }
    url = f"{SENTINEL_BASE_URL}/process"
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        logger.info("🛡[process id: %s][room id: %s] Sentinel /process accepted: %s",
                    process_id, room_id, str(response.text)[:300])
        return response.json() if response.content else {"ok": True}
    except requests.exceptions.RequestException as e:
        logger.error("❌[process id: %s][room id: %s] Sentinel /process failed: %s",
                     process_id, room_id, e)
        return {"ok": False, "error": str(e)}


def _sweep_orphan_recordings(process_id=0):
    """Self-healing backstop: ship recordings left behind by prior runs.

    The per-interview finally ships on every normal ending, but it can't cover a hard
    process kill BEFORE the finally ran (only intermediate files on disk, un-muxed) or
    an rsync that failed all retries (muxed file stranded). Run at orchestrator startup,
    this sweeps RECORDINGS_DIR: re-mux crash-orphans if needed, then rsync → /process →
    delete. mtime-guarded (SENTINEL_SWEEP_STALE_S, default 600s) so an in-progress
    recording from a concurrent worker is never touched. Idempotent and best-effort.

    Filenames: base = "{room_id}_{interview_id}"; final = "{base}_{isoTs}Z.webm" or the
    audio fallback "{base}.wav"; intermediates = "{base}_video.webm|_candidate.wav|_bot.wav".
    """
    try:
        from local_recorder import RECORDINGS_DIR, LocalVideoRecorder
    except Exception as e:
        logger.warning("🧹[sweep] local_recorder import failed: %s", e)
        return

    rec_dir = os.path.abspath(RECORDINGS_DIR)
    if not os.path.isdir(rec_dir):
        return
    stale_s = int(os.getenv("SENTINEL_SWEEP_STALE_S", "600"))
    now = time.time()

    _INTER = ("_video.mp4", "_video.webm", "_candidate.wav", "_bot.wav")
    bases = {}   # base -> {"finals": [...], "inters": [...], "newest": mtime}
    try:
        names = os.listdir(rec_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(rec_dir, name)
        if not os.path.isfile(path):
            continue
        base = None
        kind = None
        matched_inter = next((s for s in _INTER if name.endswith(s)), None)
        if matched_inter:
            base, kind = name[:-len(matched_inter)], "inter"
        elif name.endswith("Z.mp4") or name.endswith("Z.webm"):   # muxed video final
            base, kind = name.rsplit("_", 1)[0], "final"
        elif name.endswith(".wav"):             # merged audio-only fallback "{base}.wav"
            base, kind = name[:-4], "final"
        else:
            continue
        if not base:
            continue
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        e = bases.setdefault(base, {"finals": [], "inters": [], "newest": 0.0})
        (e["finals"] if kind == "final" else e["inters"]).append(path)
        e["newest"] = max(e["newest"], mt)

    for base, e in bases.items():
        # Skip anything an active worker may still be writing / just finalized.
        if now - e["newest"] < stale_s:
            continue
        parts = base.split("_")
        if len(parts) < 2:
            continue
        room_id, interview_id = parts[0], parts[1]
        try:
            ship_path = e["finals"][0] if e["finals"] else None
            if not ship_path:
                # Crash orphan → re-mux the intermediates into a final file.
                ship_path = LocalVideoRecorder(room_id, interview_id, process_id).salvage()
            if not ship_path or not os.path.exists(ship_path):
                continue
            logger.info("🧹[sweep] Orphan recording for %s → shipping %s",
                        base, os.path.basename(ship_path))
            if rsync_recording_to_sentinel(ship_path, process_id, room_id):
                process_recording(room_id=room_id, interview_id=interview_id,
                                  video_path=ship_path, sentinel_enabled=True,
                                  process_id=process_id)
                for p in set([ship_path] + e["finals"] + e["inters"]):
                    try:
                        os.remove(p)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass
            else:
                logger.warning("🧹[sweep] rsync failed for %s — leaving for next run", base)
        except Exception as _sweep_err:
            logger.warning("🧹[sweep] error handling orphan %s: %s", base, _sweep_err)


def extract_mandatory_skills(job_description: str) -> str:
    """
    Extract mandatory/required skills from a job description using GPT.
    Works for both technical and non-technical roles.
    Returns a comma-separated string of skills, or empty string if none found.
    """
    if not job_description or not job_description.strip():
        return ""

    client = OpenAI(api_key=OPENAI_API_KEY)
    MODEL = "gpt-4o-mini"

    prompt = f"""Analyze this job description and extract ONLY the mandatory/required skills.

RULES:
1. ONLY extract skills explicitly marked as "required", "mandatory", "must-have", "essential", or listed under a "Requirements" or "Qualifications" section.

2. DO NOT extract:
   - Skills marked as "nice to have", "preferred", "bonus", or "optional"
   - Soft skills (communication, teamwork, leadership, problem-solving, collaboration, attention to detail, time management, etc.)
   - Generic phrases ("ability to learn", "self-motivated", "fast-paced environment")
   - Years of experience requirements (e.g., "5+ years experience")
   - Education requirements (degrees like Bachelor's, Master's) unless it's a specific certification
   - Job responsibilities or duties
   - Company benefits or culture descriptions

3. DO extract concrete, measurable skills such as:
   - Technical: Programming languages, frameworks, databases, cloud platforms, DevOps tools
   - Marketing: SEO, SEM, Google Analytics, HubSpot, Marketo, Google Ads, social media platforms
   - Sales: CRM tools (Salesforce, HubSpot), sales methodologies, specific sales tools
   - Finance/Accounting: Excel (advanced), ERP systems (SAP, Oracle), QuickBooks, GAAP, financial modeling
   - HR: HRIS systems (Workday, ADP, BambooHR), payroll software, ATS platforms
   - Design: Figma, Sketch, Adobe Creative Suite, Photoshop, Illustrator, UI/UX tools
   - Project Management: Jira, Asana, MS Project, Monday.com, Agile/Scrum
   - Data/Analytics: SQL, Tableau, Power BI, Excel, Python, R
   - Legal: Contract law, compliance frameworks, specific legal research tools
   - Any domain-specific tools, software, platforms, methodologies, or certifications

4. If the job description has NO clearly marked mandatory/required skills section, return an empty list. Do NOT guess or infer skills from responsibilities.

5. Return ONLY valid JSON in this exact format:
{{"skills": ["skill1", "skill2", "skill3"]}}

If no mandatory skills found, return:
{{"skills": []}}

JOB DESCRIPTION:
{job_description}
"""

    try:
        response = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        
        raw = response.choices[0].message.content or ""
        data = json.loads(raw)
        skills = data.get("skills", [])
        
        if isinstance(skills, list) and skills:
            seen = set()
            unique_skills = []
            for skill in skills:
                skill_lower = skill.strip().lower()
                if skill_lower and skill_lower not in seen:
                    seen.add(skill_lower)
                    unique_skills.append(skill.strip())
            return ", ".join(unique_skills)
        
    except Exception as e:
        print(f"Error extracting skills: {e}")
    
    return ""



import threading
from concurrent.futures import ThreadPoolExecutor

def orchestrate_interview(interview, launch_index=0):
    process_id = os.getpid()
    logger.info(interview)
    import re
    from datetime import datetime
    # Dynamically convert ALL camelCase keys from the Spring Boot API to snake_case
    for api_key in list(interview.keys()):
        snake_key = re.sub('([a-z0-9])([A-Z])', r'\1_\2', api_key).lower()
        val = interview[api_key]
        # Automatically convert ISO format strings into datetime objects for timestamp fields
        if val and isinstance(val, str) and snake_key in ('interview_time', 'created_at', 'updated_at', 'deleted_at'):
            try:
                # Handle ISO 'T' separation and optional fractional seconds
                val = datetime.fromisoformat(val.replace('Z', '+00:00'))
            except ValueError:
                pass
        
        if snake_key not in interview:
            interview[snake_key] = val
        else:
            interview[snake_key] = val
            
    # --- EXACT LOCAL DB SCHEMA REPLICATION --- 
    # The original local SQL query uses JOINs which append table-prefixed fields
    # and includes various static or computed defaults. This block guarantees 
    # a 1:1 identical footprint.
    interview["id"] = interview.get("id", interview.get("interview_id"))
    # candidate_id: API may send candidateId OR studentId
    interview["candidate_id"] = (
        interview.get("candidate_id")
        or interview.get("student_id")       # studentId → student_id after camelCase conversion
    )
    # job_id: API may send jobId OR roleId
    interview["job_id"] = (
        interview.get("job_id")
        or interview.get("role_id")          # roleId → role_id after camelCase conversion
    )
    interview["recruiter_name"] = interview.get("recruiter_name", "")
    interview["recruiter_email"] = interview.get("recruiter_email", "")
    interview["resume_text"] = interview.get("resume_text")
    interview["resume_job_analysis"] = interview.get("resume_job_analysis")
    interview["resume_match_score"] = interview.get("resume_match_score", 0.0)
    interview["interview_time"] = interview.get("interview_time")
    interview["interview_timezone"] = interview.get("interview_timezone")
    if "interview_questions" not in interview or interview["interview_questions"] is None:
        interview["interview_questions"] = {}
    interview["interview_score"] = interview.get("interview_score")
    interview["interview_analysis"] = interview.get("interview_analysis")
    interview["interview_verdict"] = interview.get("interview_verdict")
    interview["interview_status"] = interview.get("interview_status", "Queued")
    interview["is_scheduled"] = interview.get("is_scheduled", 1)
    interview["is_email_sent"] = interview.get("is_email_sent", 1)
    interview["is_jd_interview_updated"] = interview.get("is_jd_interview_updated", 1)
    interview["is_jd_interview_result_updates"] = interview.get("is_jd_interview_result_updates")
    interview["created_at"] = interview.get("created_at")
    interview["updated_at"] = interview.get("updated_at")
    interview["zoom_host_url"] = interview.get("zoom_host_url")
    interview["zoom_join_url"] = interview.get("zoom_join_url")
    interview["is_reminder_email_sent"] = interview.get("is_reminder_email_sent")
    interview["recruiter_id"] = interview.get("recruiter_id")
    interview["recruiter_join_url"] = interview.get("recruiter_join_url")
    interview["candidate_join_url"] = interview.get("candidate_join_url")
    interview["bot_join_url"] = interview.get("bot_join_url")
    interview["interview_mode"] = interview.get("interview_mode", "Connect")
    interview["zone"] = interview.get("zone", "Asia/Kolkata")
    interview["best_face"] = interview.get("best_face")
    interview["candidate_present"] = interview.get("candidate_present", 1)
    interview["candidate_opresent"] = interview.get("candidate_opresent")
    interview["mandatory_skills"] = interview.get("mandatory_skills")
    interview["room_id"] = interview.get("room_id")
    interview["has_mandatory_skills"] = interview.get("has_mandatory_skills", 1)
    interview["sentinel_url"] = interview.get("sentinel_url")
    interview["sentinel_analysis"] = interview.get("sentinel_analysis")
    interview["recording_enabled"] = interview.get("recording_enabled", 1)
    interview["sentinel_enabled"] = interview.get("sentinel_enabled", 1)
    interview["recording_path"] = interview.get("recording_path")
    interview["summary_pdf"] = interview.get("summary_pdf")
    interview["detailed_pdf"] = interview.get("detailed_pdf")
    interview["summary_html"] = interview.get("summary_html")
    interview["detailed_html"] = interview.get("detailed_html")
    interview["sentinel_html"] = interview.get("sentinel_html")
    interview["original_resume_text"] = interview.get("original_resume_text")
    interview["authorship_scores_summary"] = interview.get("authorship_scores_summary")
    interview["email_config"] = interview.get("email_config")
    interview["interview_config"] = interview.get("interview_config")
    interview["current_round_number"] = interview.get("current_round_number", 1)
    interview["skills"] = interview.get("job_skills")
    interview["optional_skills"] = interview.get("optional_skills")
    interview["number_of_tabs_switched"] = interview.get("number_of_tabs_switched", 0)
    interview["round_type"] = interview.get("round_type", "technical")
    
    interview["c.id"] = interview.get("candidate_id")
    interview["candidate_jd_id"] = interview.get("candidate_jd_id")
    interview["candidate_name"] = interview.get("candidate_name")
    interview["candidate_phone"] = interview.get("candidate_phone")
    interview["candidate_email"] = interview.get("candidate_email")
    interview["c.created_at"] = interview.get("created_at")
    interview["c.resume_text"] = interview.get("resume_text")
    
    interview["j.id"] = interview.get("job_id")
    interview["job_jd_ref_number"] = interview.get("job_jd_ref_number")
    interview["job_jd_id"] = interview.get("job_jd_id")
    interview["job_title"] = interview.get("job_title")
    interview["job_description"] = interview.get("job_description", "")
    interview["company_name"] = interview.get("company_name")
    interview["company_id"] = interview.get("company_id")
    interview["job_status"] = interview.get("job_status", "Active")
    interview["j.created_at"] = interview.get("created_at")
    interview["j.skills"] = interview.get("job_skills")
    interview["j.mandatory_skills"] = interview.get("mandatory_skills")
    interview["j.has_mandatory_skills"] = interview.get("has_mandatory_skills", 1)
    
    interview["r.id"] = interview.get("recruiter_id")
    interview["recruiter_jd_id"] = interview.get("recruiter_jd_id")
    interview["r.recruiter_name"] = interview.get("recruiter_name")
    interview["r.recruiter_email"] = interview.get("recruiter_email")
    interview["is_active"] = interview.get("recruiter_is_active", 1)
    interview["r.created_at"] = interview.get("created_at")
    
    # Ensure id is present if interviewId is present
    if "interviewId" in interview and "id" not in interview:
        interview["id"] = interview["interviewId"]
        
    # Default interview_mode
    if "interview_mode" not in interview or interview["interview_mode"] is None:
        interview["interview_mode"] = ""
        
    # Ensure interview_questions is an empty dict if missing or null
    if "interview_questions" not in interview or interview["interview_questions"] is None:
        interview["interview_questions"] = {}
        
    # =========================================================================
    # 🔍 USER COMPARISON LOGGING (SQL vs API)
    # Fetch the exact same interview natively from the local database
    # and print it side-by-side with our newly synthesized API dictionary.
    # =========================================================================
    try:
        from integrations.aira_sql import Session as AiraSession   # AiraSession was renamed to Session
        session = AiraSession()
        query = f"""
            SELECT j.*,c.*,r.*, i.*, j.has_mandatory_skills, r.recruiter_name, r.recruiter_email, j.skills
            FROM interviews i
            JOIN candidates c ON i.candidate_id = c.id
            JOIN jobs j ON c.job_id = j.id
            JOIN recruiters r ON j.recruiter_id = r.id
            WHERE i.id = {interview.get('id', 0)}
        """
        result = session.execute(query)
        sql_dict = dict(result.fetchone() or {})
        session.close()
        
        logger.info("\n\n" + "="*80)
        logger.info("🚨 SQL VS API DICTIONARY COMPARISON 🚨")
        logger.info("="*80)
        
        logger.info("\n⬇️⬇️⬇️ 1. RAW DIRECT SQL DICTIONARY (Legacy Behavior) ⬇️⬇️⬇️")
        logger.info(str(sql_dict))
        
        logger.info("\n⬇️⬇️⬇️ 2. FULLY MAPPED API DICTIONARY (New Behavior) ⬇️⬇️⬇️")
        logger.info(str(interview))
        
        logger.info("="*80 + "\n\n")
    except Exception as e:
        logger.warning(f"Failed to generate comparison log: {e}")
    # =========================================================================
    
    # Stagger browser launches by 2 seconds per interview
    if launch_index > 0:
        stagger_delay = launch_index * 2
        logger.info(f"[process id: {process_id}] Staggering start by {stagger_delay}s (index: {launch_index})")
        time.sleep(stagger_delay)
    process_id = os.getpid()
    interview_id     = interview.get('id')
    candidate_name   = interview.get('candidate_name', 'Unknown Candidate')
    interview_time   = interview.get('interview_time')
    questions_data   = interview.get('interview_questions', {})
    candidate_jd_id  = interview.get('candidate_jd_id', '')
    job_jd_id        = interview.get('job_jd_id', '')
    job_description  = interview.get("job_description", "")
    sentinel_enabled=interview.get("sentinel_enabled")

    # Mandatory skills come from the DB `mandatory_skills` column. We IGNORE the
    # has_mandatory_skills flag entirely and instead check the column itself:
    #   • non-empty  → use the DB skills EXACTLY as-is (no GPT, no trimming)
    #   • empty/None → fall back to GPT-extracting skills from the JD
    # Done synchronously so the skills are populated before the interview reads them.
    _existing_mand = interview.get("mandatory_skills")
    _mand_is_empty = (
        _existing_mand is None
        or (isinstance(_existing_mand, str) and not _existing_mand.strip())
        or (isinstance(_existing_mand, (list, tuple)) and len(_existing_mand) == 0)
    )
    if _mand_is_empty and job_description:
        try:
            logger.info(f"[process id: {process_id}] mandatory_skills empty — GPT-extracting from JD (fallback)...")
            _skills = extract_mandatory_skills(job_description)
            if _skills:
                interview["mandatory_skills"] = _skills
                airadb.update_interview(interview_id, {"mandatory_skills": _skills}, process_id, "skill_extraction")
                logger.info(f"[process id: {process_id}] Mandatory skills extracted (fallback): {_skills}")
            else:
                logger.info(f"[process id: {process_id}] No mandatory skills found in job description")
        except Exception as e:
            logger.error(f"[process id: {process_id}] Fallback skill extraction failed: {e}")

    if interview["interview_mode"].lower() in ["connect", "webrtc"]:
        webrtc_url       = interview['bot_join_url']
        logger.info("[process id: %s] found the webrtc url....")
    elif interview["interview_mode"].lower()=="zoom":
        webrtc_url=interview["zoom_host_url"]
        logger.info("[process id: %s] found zoom link...")
        logger.warning("[process id: %s] stopping the process here...")
        return
    recruiter_email  = interview['recruiter_email']

    # Debug: log incoming URL & params
    logger.info("🔍[process id: %s] Original WebRTC URL: %s",process_id, webrtc_url)
    if not webrtc_url:
        logger.error("[process id: %s] webrtc_url is None — cannot process interview %s",
                     process_id, interview.get('id'))
        return

    from urllib.parse import urlparse, parse_qs
    parsed_url      = urlparse(webrtc_url)
    existing_params = parse_qs(parsed_url.query)

    # Derive room/profile info up-front
    room_id = extract_room_id_from_url(webrtc_url) or f"room_{int(time.time())}"
    profile_dir, _debug_port = _profile_params_for(room_id)
    # Use CONNECT_BASE_URL from config instead of deriving from the join URL
    # This fixes path-based routing (e.g. dev.airahr.ai/connect/...) where urlparse strips the path
    base_url = CONNECT_BASE_URL
    logger.info(f"🎯[process id: {process_id}][room id: {room_id}] base url (from config): {base_url}")
    logger.info("📋[process id: %s] Existing URL parameters: %s",process_id, existing_params)
    logger.info(f"🎯[process id: {process_id}][room id: {room_id}] interview started in {DEPLOYMENT} environment.")
    
    logger.info(f"🎯[process id: {process_id}][room id: {room_id}] Interview {interview['id']} is being processed by process {process_id}")
    logger.info("🚀[process id: %s][room id: %s] Starting interview for %s at %s (room=%s, profile=%s) at process id: %s",process_id,room_id,
                candidate_name, interview_time, room_id, profile_dir,process_id)

    # Mark as in-progress as soon as we take ownership
    try:
        airadb.update_interview(interview_id, {"interview_status": "InProgress-Initiated"},process_id,room_id)
    except Exception as e:
        logger.warning("[process id: %s][room id: %s] Could not pre-mark interview InProgress-Initiated: %s for room id: %s at process id: %s",process_id,room_id, e,room_id,process_id)
    # Sentinel room start/stop calls removed — not used. `result` kept as a
    # skipped-status stub because it's passed downstream as sentinel_status.
    result = {"ok": True, "data": {"status": "skipped"}}
    
    # ═══════════════════════════════════════════════════════════
    # TRANSPORT DECISION: LiveKit (no browser) vs Selenium (legacy)
    # ═══════════════════════════════════════════════════════════
    USE_LIVEKIT = True  # LiveKit-only: browser/Selenium removed, no fallback
    driver = None
    _livekit_bot = None
    _livekit_adapter = None
    _livekit_presence = None   # Socket.IO presence client (bot visible in UI)
    _local_recorder = None     # wall-clock recorder; started at bot-join, stopped at bot-exit
    _local_recorder = None     # wall-clock recorder; started at bot-join, stopped at bot-exit

    try:
        if USE_LIVEKIT:
            # ── LiveKit path: NO browser, NO Chrome, NO Selenium ──
            logger.info("🔌[process id: %s][room id: %s] LiveKit mode — skipping browser entirely", process_id, room_id)
            try:
                start = time.time()
                from webrtc.bot_adapter import create_adapter_from_env
                _livekit_bot, _livekit_adapter = create_adapter_from_env(room_id, process_id)

                # Inject adapter into webrtc_interview so all audio functions route through LiveKit
                webrtc_interview.set_livekit_adapter(_livekit_adapter)

                elapsed = time.time() - start
                logger.info("✅[process id: %s][room id: %s] LiveKit bot connected in %.1fs (no browser)", process_id, room_id, elapsed)

                # ── Start recording the MOMENT the bot joins ─────────────────
                # Anchored to a single wall clock from here; the recorder fills
                # silence/black whenever the candidate is absent, so candidate
                # no-show / early-leave and recruiter join never affect it.
                if interview.get("recording_enabled", 1):
                    try:
                        from local_recorder import LocalRecorder
                        _local_recorder = LocalRecorder(room_id, interview_id, process_id)
                        _local_recorder.start()
                        bot_obj = getattr(_livekit_adapter, 'bot', None) or _livekit_bot
                        if bot_obj is not None:
                            bot_obj._local_recorder = _local_recorder
                        logger.info("🎬[process id: %s][room id: %s] Synchronized recording started at bot-join", process_id, room_id)
                    except Exception as _rec_err:
                        logger.warning("⚠️[process id: %s][room id: %s] Recorder start error (non-fatal): %s",
                                       process_id, room_id, _rec_err)
                        _local_recorder = None

                # ── Start recording the MOMENT the bot joins ─────────────────
                # Anchored to a single wall clock from here; the recorder fills
                # silence/black whenever the candidate is absent, so candidate
                # no-show / early-leave and recruiter join never affect it.
                if interview.get("recording_enabled", 1):
                    try:
                        from local_recorder import LocalRecorder
                        _local_recorder = LocalRecorder(room_id, interview_id, process_id)
                        _local_recorder.start()
                        bot_obj = getattr(_livekit_adapter, 'bot', None) or _livekit_bot
                        if bot_obj is not None:
                            bot_obj._local_recorder = _local_recorder
                        logger.info("🎬[process id: %s][room id: %s] Synchronized recording started at bot-join", process_id, room_id)
                    except Exception as _rec_err:
                        logger.warning("⚠️[process id: %s][room id: %s] Recorder start error (non-fatal): %s",
                                       process_id, room_id, _rec_err)
                        _local_recorder = None

                # ── Bot presence: join the Connect Socket.IO room so the UI shows the bot ──
                # The Python LiveKit bot connects to the SFU directly and never sends a Socket.IO
                # 'join' event, so without this the candidate UI shows no bot in the participant list.
                try:
                    from livekit_bot_presence import BotPresenceClient
                    _livekit_presence = BotPresenceClient(
                        base_url=base_url,
                        bot_join_url=webrtc_url,   # contains ?t=<bot-JWT>
                        room_id=room_id,
                        bot_name="AIRA Interviewer",
                    )
                    presence_ok = _livekit_presence.connect_background(timeout=20)
                    if presence_ok:
                        logger.info("✅[process id: %s][room id: %s] Bot presence established — bot visible in room UI", process_id, room_id)
                    else:
                        logger.warning("⚠️[process id: %s][room id: %s] Bot presence FAILED — bot may not show in participant list", process_id, room_id)
                        _livekit_presence = None
                except Exception as presence_error:
                    logger.warning("⚠️[process id: %s][room id: %s] BotPresenceClient error: %s", process_id, room_id, presence_error)
                    _livekit_presence = None
            except Exception as lk_error:
                # Engine is LiveKit-only — there is NO browser fallback. A LiveKit failure
                # is a hard error: fail the interview loudly rather than silently degrade.
                logger.error("❌[process id: %s][room id: %s] LiveKit connection FAILED (no fallback): %s", process_id, room_id, lk_error, exc_info=True)
                airadb.update_interview(interview_id, {
                    "interview_status": "Failed-LiveKit-Error",
                    "interview_analysis": f"LiveKit connection failed and there is no browser fallback: {lk_error}",
                }, process_id, room_id)
                return


        # Wait for candidate and run interview
        logger.info("👤[process id: %s][room id: %s] Waiting for candidate to join...",process_id,room_id)
        joined=wait_for_participant(interview,base_url, room_id, process_id, role="candidate", timeout=600)
        if not joined:
            logger.warning("⚠️[process id: %s][room id: %s] CANDIDATE DIDNT JOINED THE INTERVIEW",process_id,room_id)
            logger.info("[process id: %s][room id: %s] Meeting ended fo id: %s",process_id,room_id,room_id)
            # Recording (started at bot-join) is finalized in the finally block.
        else:
            logger.info("🎙️[process id: %s][room id: %s] Launching AI Interview flow...",process_id,room_id)
            if driver is None and _livekit_adapter is None:
                logger.warning("⚠️[process id: %s][room id: %s] No browser driver and no LiveKit adapter; proceeding in audio-only mode",process_id,room_id)

            # ── Recording already started at bot-join (above) ─────────────────

            # ── Initialize face detector (replaces Sentinel pico.js) ──────────
            try:
                _bot = None
                if _livekit_adapter and hasattr(_livekit_adapter, 'bot'):
                    _bot = _livekit_adapter.bot
                elif _livekit_bot:
                    _bot = _livekit_bot
                if _bot:
                    _bot.init_face_detector(interview_id, process_id)
                    logger.info("🛡[process id: %s][room id: %s] Face detector initialized", process_id, room_id)
            except Exception as _fd_err:
                logger.warning("⚠️[process id: %s][room id: %s] Face detector init error (non-fatal): %s",
                               process_id, room_id, _fd_err)

            # Task 2 (local testing): attach the job's cached question_bank so the engine can
            # play cached JD-question audio. In production /claim already carries it on the
            # interview; this fills it in locally from jobs.question_bank (seeded by
            # scripts/seed_question_bank.py). No-op unless USE_CACHED_QUESTIONS is on.
            if (os.getenv("USE_CACHED_QUESTIONS", "false").lower() == "true"
                    and not interview.get("question_bank") and interview.get("job_id")):
                try:
                    _qb = airadb.get_job_question_bank(interview.get("job_id"))
                    if _qb:
                        interview["question_bank"] = _qb
                        logger.info("📚[process id: %s][room id: %s] Attached question_bank for job %s",
                                    process_id, room_id, interview.get("job_id"))
                except Exception as _qbe:
                    logger.warning("[process id: %s][room id: %s] question_bank attach failed: %s",
                                   process_id, room_id, _qbe)

            start=time.time()
            webrtc_interview.ask_questions_in_webrtc(
                base_url_param=base_url,
                interview=interview,
                driver=driver,                     # None when LiveKit — adapter handles audio
                room_id=room_id,
                process_id=process_id,
                check_participant_fn=get_room_participants,
                video_ready=True if driver else False,
                sentinel_status=result
            )
            end = time.time()
            elapsed = end - start
            logger.info(f"[process id: {process_id}][room id: {room_id}]Execution time of interview: {elapsed:.6f} seconds")
            logger.info("✅[process id: %s][room id: %s] Interview questions complete",process_id,room_id)

            # Allow any final audio to flush
            logger.info("🔊[process id: %s][room id: %s] Waiting for audio to drain...",process_id,room_id)
            time.sleep(3)

            # ── Recording is finalized (stop → rsync → /process) in the finally
            # block below, so it runs no matter how the interview ends. ──

            # Best candidate face is written locally to images/<room_id>_best_face.png
            # by the engine during the interview (used by the report). It's persisted
            # to the DB at archive time — see the best_face push before archive below.

            # ── Signal meeting end to Connect (triggers browser recording upload) ──
            try:
                end_meeting_for_all(room_id=room_id, base_url=base_url, process_id=process_id)
                logger.info("📢[process id: %s][room id: %s] meeting:ended signal sent to Connect", process_id, room_id)
                # Give browser a moment to stop recording and upload
                time.sleep(3)
            except Exception as _end_err:
                logger.warning("⚠️[process id: %s][room id: %s] Failed to signal meeting end: %s",
                               process_id, room_id, _end_err)

            # ── Cleanup: LiveKit or Browser ──
            if _livekit_presence:
                try:
                    _livekit_presence.disconnect()
                    logger.info("🔌[process id: %s][room id: %s] Bot presence disconnected", process_id, room_id)
                except Exception as e:
                    logger.warning("⚠️[process id: %s][room id: %s] Error disconnecting bot presence: %s", process_id, room_id, e)
            if _livekit_adapter:
                try:
                    _livekit_adapter.close()
                    webrtc_interview.set_livekit_adapter(None)
                    logger.info("🔌[process id: %s][room id: %s] LiveKit adapter closed", process_id, room_id)
                except Exception as e:
                    logger.warning("⚠️[process id: %s][room id: %s] Error closing LiveKit adapter: %s", process_id, room_id, e)
            airadb.update_interview(interview_id, {"interview_status": "Completed"},process_id,room_id)
    finally:
        # ── Finalize the recording no matter how the interview ended (completed,
        # candidate exit, mid-interview crash, or no-show). Stop → save path → DB. ──
        _final_rec_path = None
        try:
            if _local_recorder and _local_recorder.is_active:
                _final_rec_path = _local_recorder.stop()
                if _final_rec_path:
                    logger.info("🎬[process id: %s][room id: %s] Recording saved: %s", process_id, room_id, _final_rec_path)
                    try:
                        airadb.update_interview(interview_id, {"recording_path": _final_rec_path}, process_id, room_id)
                    except Exception as _db_err:
                        logger.warning("⚠️[process id: %s][room id: %s] Could not save recording_path to DB: %s",
                                       process_id, room_id, _db_err)
        except Exception as _rec_err:
            logger.warning("⚠️[process id: %s][room id: %s] Recorder stop error (non-fatal): %s",
                           process_id, room_id, _rec_err)

        try:
            # Persist the best candidate face (written locally during the interview)
            # to the DB via the hub API right before archiving, so the archive copies
            # it into interview_history. Stored as bytea/base64 by update_interview.
            try:
                _face_bytes = webrtc_interview.read_best_face_bytes(room_id)
                if _face_bytes:
                    airadb.update_interview(interview_id, {"best_face": _face_bytes}, process_id, room_id)
                    logger.info("🛡[process id: %s][room id: %s] Best face persisted to DB before archive (%d bytes)",
                                process_id, room_id, len(_face_bytes))
                else:
                    logger.info("🛡[process id: %s][room id: %s] No best face file to persist before archive",
                                process_id, room_id)
            except Exception as _bf_err:
                logger.warning("⚠️[process id: %s][room id: %s] best_face persist before archive failed: %s",
                               process_id, room_id, _bf_err)

            success, history_id = airadb.archive_and_delete_interview(
                room_id=room_id,
                process_id=process_id,
                company_name=interview.get("company_name"),
                interview_id=interview_id,
            )

            if success:
                print(f"Interview archived with history ID: {history_id}")
            else:
                logger.info(f"[process id: {process_id}][room id: {room_id}] Archive returned False (may have been archived already by HR flow)")
        except Exception as e:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Archive in finally block failed (may already be archived): {e}")

        # ── Ship the finished recording to the Sentinel VM, then trigger /process.
        # Gated on rsync success so the Sentinel never processes a missing file. ──
        try:
            if _final_rec_path:
                if rsync_recording_to_sentinel(_final_rec_path, process_id, room_id):
                    process_recording(
                        room_id=room_id,
                        interview_id=interview_id,
                        video_path=_final_rec_path,
                        sentinel_enabled=sentinel_enabled,
                        process_id=process_id,
                    )
                    # Keep the local copy — do NOT delete after rsync.
                    logger.info("📤[process id: %s][room id: %s] rsync OK — keeping local copy: %s",
                                process_id, room_id, os.path.basename(_final_rec_path))
                else:
                    logger.error("⚠️[process id: %s][room id: %s] rsync failed — NOT calling Sentinel /process.",
                                 process_id, room_id)
            else:
                logger.info("📤[process id: %s][room id: %s] No recording produced — skipping rsync/Sentinel /process.",
                            process_id, room_id)
        except Exception as _sen_err:
            logger.warning("⚠️[process id: %s][room id: %s] Sentinel sync/process error (non-fatal): %s",
                           process_id, room_id, _sen_err)


def _profile_params_for(room_id: str):
    """Return a unique Chrome user-data-dir and a stable remote debugging port per room."""
    safe_room = (room_id or f"room_{int(time.time())}").replace("/", "_")
    profile_dir = f"/tmp/aira_profile_{safe_room}"
    debug_port = 9222 + (abs(hash(safe_room)) % 5000)  # spread ports
    return profile_dir, debug_port


def _claim_ready_interviews(connection, now_str, limit):

    """

    Atomically move 'Not Started' -> 'Queued' for up to `limit` rows,

    then return those rows for workers to process.
 
    Runtime policy:

      - DEVELOPMENT: only pick interviews where candidate_email ends with @airahr.ai

      - PRODUCTION: no email restrictions

      - everything else: also no restrictions

    """

    # ── API mode: call POST /interviews/claim ──
    if not airadb._IS_LOCAL:
        body = {"beforeTime": now_str, "limit": limit}
        env = DEPLOYMENT.upper()
        # TEMP: email restriction removed for DEV until 2026-03-17 08:00 IST
        # if env == "DEVELOPMENT":
        #     body["emailPattern"] = "%@airahr.ai"
        try:
            result = airadb._api_call("POST", "/interviews/claim", json=body, process_id="aira_3_orchestrator")
            return result if isinstance(result, list) else []
        except Exception as e:
            logger.error(f"❌ _claim_ready_interviews API error: {e}")
            return []

    # ── LOCAL: direct DB (unchanged) ──
    env = DEPLOYMENT.upper()

    email_pattern = "%@airahr.ai"
 
    # Determine where-clause snippets
    # TEMP: email restriction removed for DEV until 2026-03-17 08:00 IST
    # if env == "DEVELOPMENT":

    #     claim_email_condition = (

    #         "AND candidate_id IN ("

    #         "   SELECT id FROM candidates WHERE candidate_email LIKE %s"

    #         ")"

    #     )

    #     fetch_email_condition = "AND c.candidate_email LIKE %s"

    # else:

    claim_email_condition = ""

    fetch_email_condition = ""
 
    with connection.cursor() as cursor:

        # Build the claim SQL

        claim_sql = f"""

            UPDATE interviews

               SET interview_status = 'Queued'

             WHERE interview_status = 'Not Started'

               AND interview_time <= %s

               {claim_email_condition}

             ORDER BY interview_time ASC

             LIMIT %s

        """
 
        claim_params = [now_str]

        if claim_email_condition:

            claim_params.append(email_pattern)

        claim_params.append(limit)
 
        cursor.execute(claim_sql, tuple(claim_params))

        connection.commit()
 
        # Build the fetch SQL

        fetch_sql = f"""

            SELECT i.*, c.*, j.*, r.*

              FROM interviews i

              JOIN candidates c ON i.candidate_id = c.id

              JOIN jobs j ON i.job_id = j.id

              JOIN recruiters r ON i.recruiter_id = r.id

             WHERE i.interview_status = 'Queued'

               {fetch_email_condition}

             ORDER BY i.interview_time ASC

             LIMIT %s

        """
 
        fetch_params = []
        if fetch_email_condition:
            fetch_params.append(email_pattern)
        fetch_params.append(limit)
        cursor.execute(fetch_sql, tuple(fetch_params))
        return cursor.fetchall()


 

AIRA_MAX_PARALLEL = int(os.getenv("AIRA_MAX_PARALLEL", 20))
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

def process_interviews():
    JD_TIMEZONE  = os.getenv("JOBDIVA_TIMEZONE")
    eastern_time = datetime.now(ZoneInfo(JD_TIMEZONE))
    now_str      = eastern_time.strftime('%Y-%m-%d %H:%M')

    logger.info(f"🕒 Current Eastern Time: {eastern_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Backstop: ship any recordings orphaned by a prior crash / failed rsync BEFORE we
    # start this run's interviews. Best-effort, mtime-guarded (won't touch live files).
    if os.getenv("AIRA_SKIP_SWEEP", "True").lower() in ("1", "true", "yes"):
        logger.info("🧹[sweep] skipped (AIRA_SKIP_SWEEP set)")
    else:
        try:
            _sweep_orphan_recordings()
        except Exception as _sweep_err:
            logger.warning("🧹[sweep] orphan sweep failed (non-fatal): %s", _sweep_err)

    tunnel = None
    connection = None
    try:
        if airadb._IS_LOCAL:
            connection, tunnel = get_db_connection()
        
        # _claim_ready_interviews handles both API and local modes internally
        jobs = _claim_ready_interviews(connection, now_str, AIRA_MAX_PARALLEL)
        if not jobs:
            logger.info("ℹ️ No runnable interviews (none claimed).")
            return

        # Spawn up to N workers (1 per interview)
        procs = min(AIRA_MAX_PARALLEL, len(jobs))
        logger.info("🚀 Launching %d parallel worker(s) for %d interview(s)", procs, len(jobs))

        # Add index to each job for staggered launch
        jobs_with_index = [(job, idx) for idx, job in enumerate(jobs)]
        
        with mp.Pool(processes=procs) as pool:
            pool.starmap(orchestrate_interview, jobs_with_index)
        logger.info("✅ All parallel workers finished.")

    except KeyboardInterrupt:
        logger.warning("🛑 KeyboardInterrupt detected. Shutting down interview processing gracefully...")
        # Optional: terminate worker pool if it’s mid-flight
        try:
            mp.active_children()
            for p in mp.active_children():
                p.terminate()
                logger.debug(f"⚙️ Terminated worker process PID {p.pid}")
        except Exception as cleanup_error:
            logger.debug(f"⚠️ Cleanup after KeyboardInterrupt failed: {cleanup_error}")

    except Exception as e:
        logger.exception(f"❌ process_interviews error: {e}")

    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if tunnel is not None:
            tunnel.stop()
            logger.info("🔌 SSH Tunnel closed.")
        logger.info("🏁 Interview processing terminated.")



if __name__ == "__main__":
    process_interviews()
