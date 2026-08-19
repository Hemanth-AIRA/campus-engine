from sqlalchemy import create_engine, Column, BigInteger, String, Text,  Float, JSON, DateTime, Boolean, TIMESTAMP , SmallInteger, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from dotenv import load_dotenv
from sqlalchemy import (
    Column, BigInteger, Text, String, Float, Integer, DateTime, 
    ForeignKey, JSON, LargeBinary, Boolean
)
import os
from typing import Any, Dict
# import mysql.connector  # Not from mysql import connector
from datetime import datetime
import inspect
import logging
import pymysql
import traceback
import platform
import requests as _http_requests
from env_config import DEPLOYMENT as _env_deployment, ENGINE_API_BASE as _env_engine_api_base
 
logger = logging.getLogger(__name__)
load_dotenv(override=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Deployment mode detection — config-driven via env_config.py
# _IS_LOCAL kept for local Mac development (direct DB access)
# ═══════════════════════════════════════════════════════════════════════════════
_IS_LOCAL = platform.system() == "Darwin"   # True on Mac → direct DB; False on Linux servers → remote API
# NOTE: was previously forced False for server deploys. Remove that override for local testing.
_ENGINE_API_BASE = _env_engine_api_base
_DEPLOYMENT = _env_deployment
logger.info("🔧 aira_sql mode: %s (API base: %s)", _DEPLOYMENT, _ENGINE_API_BASE)


def _api_call(method, path, json=None, params=None, timeout=30, process_id=None, room_id=None, allow_404=False):
    """Central HTTP helper for API mode. Raises on HTTP errors."""
    url = f"{_ENGINE_API_BASE}{path}"
    
    # Construct formatting strings for contextual logging
    log_prefix = ""
    if process_id is not None:
        log_prefix += f"[process id: {process_id}]"
    if room_id is not None:
        log_prefix += f"[room id: {room_id}]"
    if log_prefix:
        log_prefix += " "
        
    try:
        logger.info("%s📡 API REQUEST: %s %s | params=%s | json=%s", log_prefix, method, url, params, json)
        resp = _http_requests.request(method, url, json=json, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        logger.info("%s📥 API RESPONSE (%s %s): %s", log_prefix, method, path, data)
        return data
    except _http_requests.exceptions.HTTPError as e:
        if allow_404 and e.response is not None and e.response.status_code == 404:
            logger.info("%s📥 API RESPONSE (%s %s): 404 Not Found (allowed)", log_prefix, method, path)
            return None
        logger.error("%s❌ API call failed: %s %s → %s", log_prefix, method, url, e)
        if hasattr(e, 'response') and e.response is not None:
            logger.error("%s❌ ERROR RESPONSE BODY: %s", log_prefix, e.response.text)
        raise
    except Exception as e:
        logger.error("%s❌ API call failed: %s %s → %s", log_prefix, method, url, e)
        if hasattr(e, 'response') and e.response is not None:
            logger.error("%s❌ ERROR RESPONSE BODY: %s", log_prefix, e.response.text)
        raise

 
MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASS = os.getenv("MYSQL_PASS")
MYSQL_DB   = os.getenv("MYSQL_DB")
 
DB_CONFIG = {
    'host': MYSQL_HOST,  
    'port': MYSQL_PORT,
    'user': MYSQL_USER,
    'password': MYSQL_PASS,
    'database': MYSQL_DB,
    'cursorclass': pymysql.cursors.DictCursor
}
Base = declarative_base()
 
# ENUM definitions
# class JobStatus(enum.Enum):
#     Created = "Created"
#     Open = "Open"
#     On_Hold = "On Hold"
#     Closed = "Closed"
#     Filled = "Filled"
#     Cancelled = "Cancelled"
 
# class InterviewStatus(enum.Enum):
#     Not_Started = "Not Started"
#     In_Progress = "In Progress"
#     Completed = "Completed"
#     Analyzed = "Analyzed"
#     Zoom_Failes = "Zoom Meeting Failed",
#     Zoom_Initiates = "InProgress-Initiated",
#     Zoom_Waiting_For_Participant = "InProgress-Waiting for participant",
#     Zoom_Participant_Joined="InProgress-Participant joined",
#     Zoom_Participant_Not_Joined = "InProgress-Participant not joined",
#     Zoom_Participant_Left = "InProgress-Participant left",
#     Interview_Recording = "InProgress-Recording",
#     Analysis_Verdict="Analyzing verdict",
#     Analysing_Interview ="Analyzing interview",
#     Followup="Follow-Up",
#     Cancelled="Cancelled",
    
# Table definitions
class Candidate(Base):
    __tablename__ = 'candidates'
    # id = Column(BigInteger, primary_key=True)
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    candidate_jd_id = Column(String(50))
    candidate_name = Column(String(255))
    candidate_email = Column(String(255), unique=True)
    # resume_text = Column(Text,nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow,nullable=True)
 
class Job(Base):
    __tablename__ = 'jobs'
    id = Column(BigInteger, primary_key=True)
    job_jd_ref_number = Column(String(50), unique=True)
    job_title = Column(String(255))
    job_description = Column(Text)
    company_name = Column(String(255))
    company_id = Column(BigInteger)
    job_jd_id = Column(BigInteger)
    job_status = Column(String(50), default="Created")
    mandatory_skills = Column(Text, nullable=True)
    has_mandatory_skills = Column(Boolean, default=False)
    optional_skills = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class Interview(Base):
    __tablename__ = 'interviews'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id = Column(BigInteger)
    job_id = Column(BigInteger)
    interview_time = Column(DateTime)
    interview_timezone = Column(String(255))
    recruiter_name = Column(String(255))
    recruiter_email = Column(String(255))
    interview_questions = Column(JSON)
    resume_match_score = Column(Float)
    resume_job_analysis = Column(Text)
    interview_score = Column(Float)
    interview_analysis = Column(Text)
    interview_verdict = Column(String(50))
    resume_text = Column(Text,nullable=True)
    is_scheduled = Column(Boolean)
    is_email_sent = Column(Boolean)
    is_jd_interview_updated = Column(Boolean)
    is_jd_interview_result_updates = Column(Boolean)
    interview_status = Column(String(255))
    zoom_host_url = Column(Text) #Zoom
    zoom_join_url = Column(Text)
    recruiter_join_url = Column(Text) #WebRTC
    candidate_join_url = Column(Text)
    bot_join_url = Column(Text)
    is_reminder_email_sent = Column(Boolean)
    recruiter_id = Column(BigInteger, nullable=True)
    mandatory_skills = Column(Text, nullable=True)
    has_mandatory_skills = Column(Boolean, default=False)
    optional_skills = Column(Text, nullable=True)
    interview_mode = Column(Text)
    zone = Column(Text)
    sentinel_url = Column(Text)
    room_id = Column(String(255))
    recording_enabled = Column(SmallInteger)
    sentinel_enabled = Column(SmallInteger)
    recording_path = Column(String(1024), nullable=True)  # Path to interview recording
    summary_html = Column(Text)
    detailed_html = Column(Text)
    sentinel_html = Column(Text)  
    best_face = Column(LargeBinary) 
    original_resume_text = Column(Text)
    authorship_scores_summary = Column(JSON, nullable=True)  # AI detection analysis results
    email_config = Column(JSON, nullable=True)  # Custom email recipients for notifications
    interview_config = Column(JSON, nullable=True)            # Full config JSON from backend
    current_round_number = Column(BigInteger, default=1)      # Which round is currently active
    round_type = Column(String(50), default='technical')       # 'technical' or 'hr'
class InterviewAnalysis(Base):
    __tablename__ = 'interviewqa'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger)
    question = Column(Text)
    answer = Column(Text)
    analysis = Column(Text)
    score = Column(Float)
    question_number = Column(BigInteger)
    metrics = Column(JSON)
    strengths = Column(JSON)
    weaknesses = Column(JSON)

class InterviewTimeline(Base):
    __tablename__ = 'interview_timeline'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger)
    timestamp = Column(DateTime)
    conversation_type = Column(String(50))  # 'setup', 'technical_qa', 'warning', 'repeat', 'clarification', 'exit'
    aira_said = Column(Text)
    candidate_said = Column(Text)
    question_index = Column(BigInteger, nullable=True)  # Which question this relates to (null for pre-interview)
    flag = Column(Boolean, default=False)  # For flagged/skipped questions
    sequence_number = Column(BigInteger)  # Order of conversation in interview
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

class InterviewHistory(Base):
    __tablename__ = "interview_history"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=True)  # Original interview ID from interviews table
    candidate_id = Column(BigInteger)
    job_id = Column(BigInteger)
    recruiter_id = Column(BigInteger, nullable=True)
    interview_time = Column(DateTime)
    interview_timezone = Column(String(255))
    room_id = Column(String(255), unique=True)
    recruiter_name = Column(String(255))
    recruiter_email = Column(String(255))
    interview_mode = Column(Text)
    round_type = Column(String(50), default='technical')
    zone = Column(Text)
    resume_text = Column(Text, nullable=True)
    resume_match_score = Column(Float)
    resume_job_analysis = Column(Text)
    interview_score = Column(Float)
    interview_analysis = Column(Text)
    interview_verdict = Column(String(50))
    interview_status = Column(String(255))
    is_scheduled = Column(Boolean)
    is_email_sent = Column(Boolean)
    is_jd_interview_updated = Column(Boolean)
    is_jd_interview_result_updates = Column(Boolean)
    is_reminder_email_sent = Column(Boolean)
    mandatory_skills = Column(Text, nullable=True)
    has_mandatory_skills = Column(Boolean, default=False)
    optional_skills = Column(Text, nullable=True)
    recording_enabled = Column(SmallInteger)
    sentinel_enabled = Column(SmallInteger)
    recording_path = Column(String(1024), nullable=True)  # Path to interview recording
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    archived_at = Column(TIMESTAMP, default=datetime.utcnow)

    summary_html = Column(Text)
    detailed_html = Column(Text)
    sentinel_html = Column(Text)
    best_face = Column(LargeBinary)
    original_resume_text = Column(Text)
    authorship_scores_summary = Column(JSON, nullable=True)  # AI detection analysis results
    email_config = Column(JSON, nullable=True)  # Custom email recipients for notifications
    interview_config = Column(JSON, nullable=True)            # Full config JSON from backend
    current_round_number = Column(BigInteger, default=1)      # Which round was active

class Recruiter(Base):
    __tablename__ = 'recruiters'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recruiter_jd_id = Column(BigInteger)
    recruiter_name = Column(String(255))
    recruiter_email = Column(String(255), unique=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3A: Real-time per-question evaluation tables
# ═══════════════════════════════════════════════════════════════════════════════

class InterviewQuestion(Base):
    """Per-question row inserted synchronously when answer is received.
    Evaluation fields updated asynchronously by background thread."""
    __tablename__ = 'interview_questions_live'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False)
    candidate_id = Column(BigInteger, nullable=True)  # For cross-round cleanup
    job_id = Column(BigInteger, nullable=True)          # For cross-round queries (candidate+job)
    round_number = Column(BigInteger, default=1)
    question_number = Column(BigInteger, nullable=False)
    skill = Column(Text)
    effective_level = Column(BigInteger)
    constraint_name = Column(String(255))
    question_text = Column(Text)
    answer_text = Column(Text)
    evaluation_status = Column(String(50), default="pending")  # pending → completed | failed
    summary = Column(Text)
    confidence_score = Column(Float)
    evaluation_tags = Column(JSON)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    evaluated_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        Index('idx_iq_interview_id', 'interview_id'),
        Index('idx_iq_candidate_id', 'candidate_id'),
        Index('idx_iq_candidate_job', 'candidate_id', 'job_id'),
    )


class InterviewProject(Base):
    """Projects mentioned by candidates, deduplicated per interview."""
    __tablename__ = 'interview_projects'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False)
    candidate_id = Column(BigInteger, nullable=True)  # For cross-round cleanup
    job_id = Column(BigInteger, nullable=True)          # For cross-round queries (candidate+job)
    project_name = Column(String(500))
    normalized_name = Column(String(500))
    tech_stack = Column(JSON)
    ownership = Column(String(255))
    production_exposure = Column(Boolean, default=False)
    scale_context = Column(Text)
    first_mentioned_question = Column(BigInteger)
    last_mentioned_round = Column(BigInteger)
    last_mentioned_question = Column(BigInteger)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_ip_interview_id', 'interview_id'),
        Index('idx_ip_candidate_id', 'candidate_id'),
        Index('idx_ip_candidate_job', 'candidate_id', 'job_id'),
    )


class CandidateSkillProfile(Base):
    """Longitudinal skill baseline — updated after each interview via EMA."""
    __tablename__ = 'candidate_skill_profiles'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    candidate_id = Column(BigInteger, nullable=False)
    skill = Column(String(255), nullable=False)
    baseline_complexity = Column(Float, default=0.0)
    baseline_confidence = Column(Float, default=0.0)
    confidence_variance = Column(Float, default=0.0)
    total_interviews = Column(BigInteger, default=0)
    last_interview_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_csp_candidate_skill', 'candidate_id', 'skill'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# HR Interview Engine Models
# ═══════════════════════════════════════════════════════════════════════════

class EvaluationDimension(Base):
    """Behavioral evaluation dimensions — system defaults + recruiter custom."""
    __tablename__ = 'evaluation_dimensions'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    account_id = Column(BigInteger, nullable=True)
    dimension_name = Column(String(255), nullable=False)
    dimension_key = Column(String(100), nullable=False)
    description = Column(Text)
    weight = Column(Float, default=1.0)
    is_default = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)


class InterviewDimension(Base):
    """Links dimensions to interviews with snapshot for historical stability."""
    __tablename__ = 'interview_dimensions'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False)
    dimension_id = Column(BigInteger, nullable=False)
    dimension_key = Column(String(100))
    dimension_name = Column(String(255))

    __table_args__ = (
        Index('idx_idim_interview', 'interview_id'),
    )


class InterviewEvaluationReport(Base):
    """Raw LLM evaluation JSON — debugging safety net."""
    __tablename__ = 'interview_evaluation_reports'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False)
    report_json = Column(JSON)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_ier_interview', 'interview_id'),
    )


class InterviewEvaluationResult(Base):
    """Per-dimension evaluation score and justification."""
    __tablename__ = 'interview_evaluations'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False)
    dimension_id = Column(BigInteger, nullable=False)
    score = Column(Float)
    justification = Column(Text)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_ie_interview', 'interview_id'),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Interview Report Data — Structured storage for ETL & report regeneration
# Single FK to interview_history. Zero redundancy.
# ═══════════════════════════════════════════════════════════════════════════════

class InterviewReportData(Base):
    """Stores all structured report data for ETL, analytics, and report regeneration.
    Single FK: interview_id → interview_history.interview_id.
    All candidate/job/recruiter info accessed via joins — zero duplication."""
    __tablename__ = 'interview_report_data'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False, unique=True)

    # Interview stats (calculated at runtime, not stored elsewhere)
    interview_duration = Column(String(100), nullable=True)
    questions_asked_count = Column(BigInteger, default=0)
    total_questions_configured = Column(BigInteger, default=15)

    # Scores
    resume_analysis_score = Column(Float, default=0.0)
    resume_analysis_text = Column(Text, nullable=True)
    interview_final_score = Column(Float, default=0.0)
    interview_analysis_text = Column(Text, nullable=True)

    # Verdict
    verdict_passed = Column(Boolean, default=False)
    verdict_status = Column(String(100), nullable=True)
    overall_assessment = Column(Text, nullable=True)
    strengths = Column(JSON, nullable=True)
    weaknesses = Column(JSON, nullable=True)

    # Performance overview
    performance_summary = Column(Text, nullable=True)
    avg_metrics = Column(JSON, nullable=True)

    # Per-question detail
    questions_data = Column(JSON, nullable=True)
    conversation_timeline = Column(JSON, nullable=True)

    # Mandatory skills
    has_mandatory_skills = Column(Boolean, default=False)
    mandatory_skills_text = Column(Text, nullable=True)
    mandatory_skills_evaluation = Column(JSON, nullable=True)

    # Authorship / AI detection
    authorship_analysis = Column(JSON, nullable=True)

    # Behavioral metrics
    behavioral_metrics = Column(JSON, nullable=True)

    # Raw analysis inputs (for full regeneration)
    json_final_analysis = Column(JSON, nullable=True)
    analysis_blocks_raw = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_ird_interview_id', 'interview_id'),
        Index('idx_ird_verdict', 'verdict_passed'),
        Index('idx_ird_score', 'interview_final_score'),
        Index('idx_ird_created', 'created_at'),
    )


class HRInterviewReportData(Base):
    """Stores HR behavioral interview report data for ETL and report regeneration.
    Mirrors hr_interview_report_data table from migration."""
    __tablename__ = 'hr_interview_report_data'

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    interview_id = Column(BigInteger, nullable=False, unique=True)

    # Overall score (0-5 scale)
    overall_score = Column(Float, default=0.0)
    summary = Column(Text, nullable=True)

    # Verdict
    verdict_passed = Column(Boolean, default=False)
    verdict_status = Column(String(100), nullable=True)

    # Dimension scorecard (JSON array)
    dimension_scores = Column(JSON, nullable=True)

    # Strengths & concerns (JSON arrays)
    strengths = Column(JSON, nullable=True)
    concerns = Column(JSON, nullable=True)

    # Full transcript (JSON array)
    transcript = Column(JSON, nullable=True)

    # Completion metadata
    topics_covered = Column(BigInteger, default=0)
    topics_total = Column(BigInteger, default=0)
    total_responses = Column(BigInteger, default=0)
    explorations = Column(BigInteger, default=0)
    duration_minutes = Column(Float, default=0.0)
    completion_status = Column(String(50), nullable=True)
    phase = Column(BigInteger, default=1)

    # Raw LLM evaluation JSON
    raw_evaluation = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(TIMESTAMP, default=datetime.utcnow)
    updated_at = Column(TIMESTAMP, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        Index('idx_hrd_interview_id', 'interview_id'),
        Index('idx_hrd_verdict', 'verdict_passed'),
        Index('idx_hrd_score', 'overall_score'),
        Index('idx_hrd_created', 'created_at'),
    )

# Database connection
DATABASE_URL = (
    f"mysql+mysqlconnector://{MYSQL_USER}:{MYSQL_PASS}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"
)
 
 
engine = create_engine(
    DATABASE_URL,
    connect_args={"connect_timeout": 5},
    pool_pre_ping=True,
)
Session = sessionmaker(bind=engine)
try:
    session = Session()
except Exception as _e:
    logger.warning("⚠️ Could not create default DB session at import time: %s", _e)
    session = None


def get_job_question_bank(job_id):
    """Task 2: return the job's cached `question_bank` dict from the LOCAL DB, or None.

    In production the /claim response carries `question_bank` on the interview payload;
    this is the local-testing path that reads jobs.question_bank (seeded by
    scripts/seed_question_bank.py). Safe no-op remotely or before the column is seeded.
    """
    if not _IS_LOCAL or not job_id:
        return None
    try:
        import json
        import pymysql
        conn = pymysql.connect(**DB_CONFIG)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM information_schema.columns "
                    "WHERE table_schema=DATABASE() AND table_name='jobs' AND column_name='question_bank'"
                )
                row = cur.fetchone()
                if (row["c"] if isinstance(row, dict) else row[0]) == 0:
                    return None
                cur.execute("SELECT question_bank FROM jobs WHERE id=%s", (job_id,))
                r = cur.fetchone()
        finally:
            conn.close()
        qb = (r["question_bank"] if isinstance(r, dict) else r[0]) if r else None
        if not qb:
            return None
        return qb if isinstance(qb, dict) else json.loads(qb)
    except Exception as e:
        logger.warning("get_job_question_bank(%s) failed: %s", job_id, e)
        return None
 
# Utility functions

# Unified mapping for Snake Case (Python DB models) to Camel Case (Java API DTOs)
SNAKE_TO_CAMEL_MAP = {
    "candidate_id": "candidateId", "job_id": "jobId", "recruiter_id": "recruiterId",
    "interview_time": "interviewTime", "interview_timezone": "interviewTimezone",
    "recruiter_name": "recruiterName", "recruiter_email": "recruiterEmail",
    "interview_questions": "interviewQuestions", "resume_text": "resumeText",
    "interview_mode": "interviewMode", "zone": "zone", "room_id": "roomId",
    "recruiter_join_url": "recruiterJoinUrl", "candidate_join_url": "candidateJoinUrl",
    "bot_join_url": "botJoinUrl", "zoom_host_url": "zoomHostUrl", "zoom_join_url": "zoomJoinUrl",
    "recording_enabled": "recordingEnabled", "sentinel_enabled": "sentinelEnabled",
    "mandatory_skills": "mandatorySkills", "optional_skills": "optionalSkills", 
    "has_mandatory_skills": "hasMandatorySkills", "interview_status": "interviewStatus", 
    "email_config": "emailConfig", "interview_config": "interviewConfig", 
    "current_round_number": "currentRoundNumber", "round_type": "roundType", 
    "resume_match_score": "resumeMatchScore", "resume_job_analysis": "resumeJobAnalysis", 
    "original_resume_text": "originalResumeText", "sentinel_url": "sentinelUrl",
    "interview_score": "interviewScore", "interview_verdict": "interviewVerdict", 
    "interview_analysis": "interviewAnalysis", "summary_html": "summaryHtml", 
    "detailed_html": "detailedHtml", "sentinel_html": "sentinelHtml", 
    "best_face": "bestFace", "authorship_scores_summary": "authorshipScoresSummary",
    "is_scheduled": "isScheduled", "is_email_sent": "isEmailSent", 
    "is_jd_interview_updated": "isJdInterviewUpdated", 
    "is_jd_interview_result_updates": "isJdInterviewResultUpdates",
    "recording_path": "recordingPath", "is_reminder_email_sent": "isReminderEmailSent",
    "candidate_phone": "candidatePhone", "comments": "comments", "updated_at": "updatedAt",
    "job_status": "jobStatus", "job_skills": "jobSkills", "recruiter_is_active": "recruiterIsActive",
    "candidate_present": "candidatePresent",
}

def insert_or_get_candidate(candidate_data, process_id: int = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("POST", "/candidate/upsert", json={
            "candidateJdId": candidate_data.get("candidate_jd_id"),
            "candidateName": candidate_data.get("candidate_name"),
            "candidateEmail": candidate_data.get("candidate_email"),
        }, process_id=process_id, room_id=room_id)
        return result.get("id")
    # ── LOCAL: direct DB ──
    try:
        candidate = Candidate(**candidate_data)
        session.add(candidate)
        session.commit()
        logger.info("Inserted new candidate.")
        return candidate.id
    except Exception:
        # Rollback the failed transaction
        session.rollback()
        
        # Fetch the existing candidate
        existing = session.query(Candidate).filter_by(candidate_email=candidate_data['candidate_email']).first()
        print(existing)
        if existing:
            # Update all fields except email
            for key, value in candidate_data.items():
                if key != 'candidate_email':
                    setattr(existing, key, value)
            
            session.commit()
            logger.info("Updated existing candidate with email: %s", candidate_data['candidate_email'])
            return existing.id
        else:
            logger.warning("Integrity error, but no existing candidate found for email: %s", candidate_data['candidate_email'])
            return None
def update_candidate(candidate_id, update_data):
    session.query(Candidate).filter_by(id=candidate_id).update(update_data)
    session.commit()
    logger.info("Candidate updated.")
 
def insert_or_get_job(job_data, process_id: int = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("POST", "/job/upsert", json={
            "jobJdRefNumber": job_data.get("job_jd_ref_number"),
            "jobTitle": job_data.get("job_title"),
            "jobDescription": job_data.get("job_description"),
            "companyName": job_data.get("company_name"),
            "companyId": job_data.get("company_id"),
            "jobJdId": job_data.get("job_jd_id"),
            "mandatorySkills": job_data.get("mandatory_skills"),
            "hasMandatorySkills": job_data.get("has_mandatory_skills"),
            "optionalSkills": job_data.get("optional_skills"),
        }, process_id=process_id, room_id=room_id)
        return result.get("id")
    # ── LOCAL: direct DB ──
    try:
        job = Job(**job_data)
        session.add(job)
        session.commit()
        logger.info("Inserted new job.")
        return job.id
    except IntegrityError:
        session.rollback()
        existing = session.query(Job).filter_by(job_jd_ref_number=job_data['job_jd_ref_number']).first()
        logger.info("Duplicate job found.")
        return existing.id if existing else None
 
def update_job(job_id, update_data):
    session.query(Job).filter_by(id=job_id).update(update_data)
    session.commit()
    logger.info("Job updated.")
 
# def insert_or_get_interview(interview_data):
#     try:
#         interview = Interview(**interview_data)
#         session.add(interview)
#         session.commit()
#         logger.info("Inserted new interview.")
#         return interview.id
#     except IntegrityError:
#         session.rollback()
#         existing = session.query(Interview).filter_by(
#             candidate_id=interview_data['candidate_id'],
#             job_id=interview_data['job_id']
#         ).first()
#         logger.info("Duplicate interview found.")
#         return existing.id if existing else None

def simplify_metrics(metrics_list):
    return {m["name"].lower().replace(" ", "_"): m["score"] for m in metrics_list}

def save_analysis_blocks(interview_id: int, analysis_blocks: list,process_id: str = None,room_id: str = None):
    if not _IS_LOCAL:
        blocks_payload = []
        for block in analysis_blocks:
            if not isinstance(block, dict) or block.get("flag", False):
                continue
            
            clean_metrics = []
            for m in block.get("metrics", []):
                if isinstance(m, dict):
                    try:
                        metric_score = float(m.get("score", 0))
                    except (ValueError, TypeError):
                        metric_score = 0.0
                    clean_metrics.append({
                        "name": m.get("name"),
                        "score": metric_score
                    })
                    
            try:
                ov_score = float(block.get("overall_score", 0))
            except (ValueError, TypeError):
                ov_score = 0.0

            blocks_payload.append({
                "question": block.get("question"),
                "answer": block.get("answer"),
                "analysis": block.get("analysis"),
                "overallScore": ov_score,
                "metrics": clean_metrics,
                "strengths": block.get("strengths", []),
                "weaknesses": block.get("weaknesses", []),
                "flag": block.get("flag", False),
            })
        result = _api_call("POST", "/analysis/save", json={
            "interviewId": interview_id,
            "analysisBlocks": blocks_payload,
        }, process_id=process_id, room_id=room_id)
        return result
    # ── LOCAL: direct DB ──
    try:
        total_blocks = len(analysis_blocks)
        saved_count = 0
        skipped_count = 0
        
        for i, block in enumerate(analysis_blocks, start=1):
            # LAYER 1: Type validation - block must be a dictionary
            if not isinstance(block, dict):
                logger.warning(
                    "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: Expected dict, got %s. "
                    "First 200 chars: %s",
                    process_id, room_id, i, type(block).__name__, str(block)[:200]
                )
                skipped_count += 1
                continue  # Skip this invalid block, process others
            
            # LAYER 1.5: Skip flagged/conversation blocks (these are not analysis data)
            if block.get("flag", False):
                logger.info(
                    "[process id: %s][room id: %s] ℹ️ SKIPPING BLOCK #%d: Flagged as conversation/skipped question "
                    "(flag=True). This is expected behavior.",
                    process_id, room_id, i
                )
                skipped_count += 1
                continue  # Skip flagged blocks - they don't have analysis data
            
            # LAYER 2: Required keys validation
            required_keys = ["metrics", "question", "answer", "analysis", "overall_score"]
            missing_keys = [key for key in required_keys if key not in block]
            if missing_keys:
                logger.warning(
                    "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: Missing keys %s. "
                    "Available keys: %s",
                    process_id, room_id, i, missing_keys, list(block.keys())
                )
                skipped_count += 1
                continue  # Skip this incomplete block
            
            # LAYER 3: Metrics type validation - must be non-None list
            if block["metrics"] is None or not isinstance(block["metrics"], list):
                logger.warning(
                    "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: 'metrics' must be a list, got %s. "
                    "Value: %s",
                    process_id, room_id, i, type(block["metrics"]).__name__, str(block["metrics"])[:200]
                )
                skipped_count += 1
                continue  # Skip this block with invalid metrics
            
            # LAYER 4: Metrics content validation - each metric must be a dict with 'name' and 'score'
            invalid_metrics = False
            for j, metric in enumerate(block["metrics"]):
                if not isinstance(metric, dict):
                    logger.warning(
                        "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: metrics[%d] is not a dict, got %s",
                        process_id, room_id, i, j, type(metric).__name__
                    )
                    invalid_metrics = True
                    break
                if "name" not in metric or "score" not in metric:
                    logger.warning(
                        "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: metrics[%d] missing 'name' or 'score'. "
                        "Keys: %s",
                        process_id, room_id, i, j, list(metric.keys())
                    )
                    invalid_metrics = True
                    break
            
            if invalid_metrics:
                skipped_count += 1
                continue  # Skip this block with invalid metrics structure
            
            # LAYER 5: Field value validation - ensure non-None critical fields
            if block["question"] is None or block["answer"] is None:
                logger.warning(
                    "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: 'question' or 'answer' is None",
                    process_id, room_id, i
                )
                skipped_count += 1
                continue
            
            # Try to parse metrics safely
            try:
                simplified_metrics = simplify_metrics(block["metrics"])
            except Exception as e:
                logger.warning(
                    "[process id: %s][room id: %s] ❌ INVALID BLOCK #%d: Failed to simplify metrics: %s",
                    process_id, room_id, i, str(e)
                )
                skipped_count += 1
                continue
            record = InterviewAnalysis(
                interview_id=interview_id,
                question=block["question"],
                answer=block["answer"],
                analysis=block["analysis"],
                score=block["overall_score"],
                question_number=i,
                metrics=simplified_metrics,
                strengths=block.get("strengths", []),
                weaknesses=block.get("weaknesses", [])
            )
            session.add(record)
            saved_count += 1
            
        session.commit()
        
        # Summary logging
        if skipped_count > 0:
            logger.warning(
                "[process id: %s][room id: %s] ⚠️ Analysis blocks saved with issues: "
                "Total=%d, Saved=%d, Skipped=%d",
                process_id, room_id, total_blocks, saved_count, skipped_count
            )
        else:
            logger.info(
                "[process id: %s][room id: %s] ✅ All analysis blocks saved successfully: %d/%d",
                process_id, room_id, saved_count, total_blocks
            )
        
        return True
    except IntegrityError:
        session.rollback()
        logger.warning("[process id: %s][room id: %s]Duplicate entry exception for interview analysis, but no existing record found.",process_id,room_id)
        return None
    except Exception as e:
        session.rollback()
        logger.error("[process id: %s][room id: %s] Error saving analysis blocks: %s",process_id,room_id, str(e))
        return None

def save_conversation_timeline(interview_id: int, conversation_timeline: list, process_id, room_id):
    if not _IS_LOCAL:
        items = []
        for i, item in enumerate(conversation_timeline, start=1):
            if not isinstance(item, dict):
                continue
            items.append({
                "timestamp": item.get("timestamp", ""),
                "conversationType": item.get("conversation_type", "unknown"),
                "airaSaid": item.get("aira_said", ""),
                "candidateSaid": item.get("candidate_said", ""),
                "questionIndex": item.get("question_index"),
                "flag": item.get("flag", False),
                "sequenceNumber": i,
            })
        result = _api_call("POST", "/timeline/add", json={
            "interviewId": interview_id,
            "timeline": items,
        }, process_id=process_id, room_id=room_id)
        return True
    # ── LOCAL: direct DB ──
    """
    Save complete conversation timeline to database for future report regeneration.
    
    Args:
        interview_id: Interview ID
        conversation_timeline: List of timeline items from interview
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        True if successful, None if failed
    """
    try:
        total_items = len(conversation_timeline)
        saved_count = 0
        skipped_count = 0
        
        logger.info(
            "[process id: %s][room id: %s] 📝 Saving conversation timeline: %d items",
            process_id, room_id, total_items
        )
        
        for i, timeline_item in enumerate(conversation_timeline, start=1):
            # Validate timeline item
            if not isinstance(timeline_item, dict):
                logger.warning(
                    "[process id: %s][room id: %s] ❌ INVALID TIMELINE ITEM #%d: Expected dict, got %s",
                    process_id, room_id, i, type(timeline_item).__name__
                )
                skipped_count += 1
                continue
            
            # Extract fields with defaults
            timestamp_str = timeline_item.get("timestamp")
            conversation_type = timeline_item.get("conversation_type", "unknown")
            aira_said = timeline_item.get("aira_said", "")
            candidate_said = timeline_item.get("candidate_said", "")
            question_index = timeline_item.get("question_index")
            flag = timeline_item.get("flag", False)
            
            # Parse timestamp
            try:
                if timestamp_str:
                    timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                else:
                    timestamp = datetime.utcnow()
            except (ValueError, TypeError) as e:
                logger.warning(
                    "[process id: %s][room id: %s] ⚠️ TIMELINE ITEM #%d: Invalid timestamp '%s', using current time: %s",
                    process_id, room_id, i, timestamp_str, str(e)
                )
                timestamp = datetime.utcnow()
            
            # Create timeline record
            record = InterviewTimeline(
                interview_id=interview_id,
                timestamp=timestamp,
                conversation_type=conversation_type,
                aira_said=aira_said[:65535] if aira_said else "",  # Limit TEXT field size
                candidate_said=candidate_said[:65535] if candidate_said else "",
                question_index=question_index,
                flag=flag,
                sequence_number=i
            )
            session.add(record)
            saved_count += 1
        
        session.commit()
        
        # Summary logging
        if skipped_count > 0:
            logger.warning(
                "[process id: %s][room id: %s] ⚠️ Timeline saved with issues: "
                "Total=%d, Saved=%d, Skipped=%d",
                process_id, room_id, total_items, saved_count, skipped_count
            )
        else:
            logger.info(
                "[process id: %s][room id: %s] ✅ Complete timeline saved successfully: %d/%d items",
                process_id, room_id, saved_count, total_items
            )
        
        return True
        
    except IntegrityError as e:
        session.rollback()
        logger.warning(
            "[process id: %s][room id: %s] ⚠️ Duplicate timeline entry: %s",
            process_id, room_id, str(e)
        )
        return None
    except Exception as e:
        session.rollback()
        logger.error(
            "[process id: %s][room id: %s] ❌ Error saving conversation timeline: %s",
            process_id, room_id, str(e)
        )
        import traceback
        logger.error(traceback.format_exc())
        return None

def get_conversation_timeline(interview_id: int, process_id=0, room_id=""):
    if not _IS_LOCAL:
        items = _api_call("GET", f"/interview/timeline/{interview_id}", process_id=process_id, room_id=room_id)
        # Convert camelCase back to snake_case for callers
        timeline = []
        for item in (items if isinstance(items, list) else []):
            timeline.append({
                "timestamp": item.get("timestamp", ""),
                "conversation_type": item.get("conversationType", item.get("conversation_type", "")),
                "aira_said": item.get("airaSaid", item.get("aira_said", "")),
                "candidate_said": item.get("candidateSaid", item.get("candidate_said", "")),
                "question_index": item.get("questionIndex", item.get("question_index")),
                "flag": item.get("flag", False),
                "sequence_number": item.get("sequenceNumber", item.get("sequence_number")),
            })
        return timeline
    # ── LOCAL: direct DB ──
    """
    Retrieve conversation timeline from database for report regeneration.
    
    Args:
        interview_id: Interview ID
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        List of timeline items in chronological order
    """
    try:
        timeline_records = session.query(InterviewTimeline).filter_by(
            interview_id=interview_id
        ).order_by(InterviewTimeline.sequence_number).all()
        
        timeline_list = []
        for record in timeline_records:
            timeline_item = {
                "timestamp": record.timestamp.strftime("%Y-%m-%d %H:%M:%S") if record.timestamp else "",
                "conversation_type": record.conversation_type,
                "aira_said": record.aira_said,
                "candidate_said": record.candidate_said,
                "question_index": record.question_index,
                "flag": record.flag,
                "sequence_number": record.sequence_number
            }
            timeline_list.append(timeline_item)
        
        logger.info(
            "[process id: %s][room id: %s] ✅ Retrieved %d timeline items for interview %d",
            process_id, room_id, len(timeline_list), interview_id
        )
        
        return timeline_list
        
    except Exception as e:
        logger.error(
            "[process id: %s][room id: %s] ❌ Error retrieving timeline: %s",
            process_id, room_id, str(e)
        )
        return []
 
# ═══════════════════════════════════════════════════════════════════════════════
# Interview Report Data — Save & Retrieve structured report data
# ═══════════════════════════════════════════════════════════════════════════════

def save_interview_report_data(interview_id: int, report_data: dict, process_id=None, room_id=None):
    """
    Save structured interview report data for ETL and report regeneration.
    Uses upsert pattern — inserts new row or updates existing one for this interview_id.
    
    Args:
        interview_id: Original interview ID (references interview_history.interview_id)
        report_data: Dict of report fields from extract_report_data_for_storage()
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        True if successful, None if failed
    """
    if not _IS_LOCAL:
        payload = {"interviewId": interview_id}
        payload.update(report_data)
        try:
            _api_call("POST", "/report-data/save", json=payload, process_id=process_id, room_id=room_id)
            return True
        except Exception as e:
            logger.error(
                "[process id: %s][room id: %s] ❌ API save_interview_report_data failed: %s",
                process_id, room_id, e
            )
            return None

    # ── LOCAL: direct DB ──
    local_session = Session()
    try:
        # Check if report data already exists for this interview
        existing = local_session.query(InterviewReportData).filter_by(
            interview_id=interview_id
        ).first()

        if existing:
            logger.info(
                "[process id: %s][room id: %s] Updating existing report data for interview_id=%d",
                process_id, room_id, interview_id
            )
            for key, value in report_data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            existing.updated_at = datetime.utcnow()
        else:
            report_data['interview_id'] = interview_id
            record = InterviewReportData(**report_data)
            local_session.add(record)
            logger.info(
                "[process id: %s][room id: %s] Creating new report data for interview_id=%d",
                process_id, room_id, interview_id
            )

        local_session.commit()
        logger.info(
            "[process id: %s][room id: %s] ✅ Interview report data saved successfully for interview_id=%d",
            process_id, room_id, interview_id
        )
        return True

    except Exception as e:
        local_session.rollback()
        logger.error(
            "[process id: %s][room id: %s] ❌ Error saving interview report data: %s",
            process_id, room_id, str(e)
        )
        logger.error(traceback.format_exc())
        return None
    finally:
        local_session.close()


def get_interview_report_data(interview_id: int, process_id=None, room_id=None):
    """
    Retrieve structured interview report data for report regeneration.
    
    Args:
        interview_id: Original interview ID
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        Dict of report data fields, or None if not found
    """
    if not _IS_LOCAL:
        try:
            result = _api_call("GET", f"/report-data/{interview_id}", process_id=process_id, room_id=room_id)
            return result if result else None
        except Exception as e:
            logger.error(
                "[process id: %s][room id: %s] ❌ API get_interview_report_data failed: %s",
                process_id, room_id, e
            )
            return None

    # ── LOCAL: direct DB ──
    try:
        record = session.query(InterviewReportData).filter_by(
            interview_id=interview_id
        ).first()

        if not record:
            logger.warning(
                "[process id: %s][room id: %s] No report data found for interview_id=%d",
                process_id, room_id, interview_id
            )
            return None

        # Convert ORM object to dict
        report_dict = {
            'interview_id': record.interview_id,
            'interview_duration': record.interview_duration,
            'questions_asked_count': record.questions_asked_count,
            'total_questions_configured': record.total_questions_configured,
            'resume_analysis_score': record.resume_analysis_score,
            'resume_analysis_text': record.resume_analysis_text,
            'interview_final_score': record.interview_final_score,
            'interview_analysis_text': record.interview_analysis_text,
            'verdict_passed': record.verdict_passed,
            'verdict_status': record.verdict_status,
            'overall_assessment': record.overall_assessment,
            'strengths': record.strengths,
            'weaknesses': record.weaknesses,
            'performance_summary': record.performance_summary,
            'avg_metrics': record.avg_metrics,
            'questions_data': record.questions_data,
            'conversation_timeline': record.conversation_timeline,
            'has_mandatory_skills': record.has_mandatory_skills,
            'mandatory_skills_text': record.mandatory_skills_text,
            'mandatory_skills_evaluation': record.mandatory_skills_evaluation,
            'authorship_analysis': record.authorship_analysis,
            'behavioral_metrics': record.behavioral_metrics,
            'json_final_analysis': record.json_final_analysis,
            'analysis_blocks_raw': record.analysis_blocks_raw,
            'created_at': record.created_at,
            'updated_at': record.updated_at,
        }

        logger.info(
            "[process id: %s][room id: %s] ✅ Retrieved report data for interview_id=%d",
            process_id, room_id, interview_id
        )
        return report_dict

    except Exception as e:
        logger.error(
            "[process id: %s][room id: %s] ❌ Error retrieving interview report data: %s",
            process_id, room_id, str(e)
        )
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# HR Interview Report Data — Save & Retrieve structured HR report data
# ═══════════════════════════════════════════════════════════════════════════════

def save_hr_report_data(interview_id: int, report_data: dict, process_id=None, room_id=None):
    """
    Save structured HR behavioral interview report data.
    Uses upsert pattern — inserts new row or updates existing one.
    
    Args:
        interview_id: Interview ID (references interview_history.interview_id)
        report_data: Dict with HR report fields (overall_score, summary, dimension_scores, etc.)
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        True if successful, None if failed
    """
    if not _IS_LOCAL:
        payload = {"interviewId": interview_id}
        payload.update(report_data)
        try:
            _api_call("POST", "/hr-report-data/save", json=payload, process_id=process_id, room_id=room_id)
            return True
        except Exception as e:
            logger.error(
                "[process id: %s][room id: %s] ❌ API save_hr_report_data failed: %s",
                process_id, room_id, e
            )
            return None

    # ── LOCAL: direct DB ──
    local_session = Session()
    try:
        existing = local_session.query(HRInterviewReportData).filter_by(
            interview_id=interview_id
        ).first()

        if existing:
            logger.info(
                "[process id: %s][room id: %s] Updating existing HR report data for interview_id=%d",
                process_id, room_id, interview_id
            )
            for key, value in report_data.items():
                if hasattr(existing, key):
                    setattr(existing, key, value)
            existing.updated_at = datetime.utcnow()
        else:
            report_data['interview_id'] = interview_id
            record = HRInterviewReportData(**report_data)
            local_session.add(record)
            logger.info(
                "[process id: %s][room id: %s] Creating new HR report data for interview_id=%d",
                process_id, room_id, interview_id
            )

        local_session.commit()
        logger.info(
            "[process id: %s][room id: %s] ✅ HR report data saved successfully for interview_id=%d",
            process_id, room_id, interview_id
        )
        return True

    except Exception as e:
        local_session.rollback()
        logger.error(
            "[process id: %s][room id: %s] ❌ Error saving HR report data: %s",
            process_id, room_id, str(e)
        )
        logger.error(traceback.format_exc())
        return None
    finally:
        local_session.close()


def get_hr_report_data(interview_id: int, process_id=None, room_id=None):
    """
    Retrieve structured HR behavioral interview report data.
    
    Args:
        interview_id: Interview ID
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        Dict of HR report data fields, or None if not found
    """
    if not _IS_LOCAL:
        try:
            result = _api_call("GET", f"/hr-report-data/{interview_id}", process_id=process_id, room_id=room_id)
            return result if result else None
        except Exception as e:
            logger.error(
                "[process id: %s][room id: %s] ❌ API get_hr_report_data failed: %s",
                process_id, room_id, e
            )
            return None

    # ── LOCAL: direct DB ──
    try:
        record = session.query(HRInterviewReportData).filter_by(
            interview_id=interview_id
        ).first()

        if not record:
            logger.warning(
                "[process id: %s][room id: %s] No HR report data found for interview_id=%d",
                process_id, room_id, interview_id
            )
            return None

        report_dict = {
            'interview_id': record.interview_id,
            'overall_score': record.overall_score,
            'summary': record.summary,
            'verdict_passed': record.verdict_passed,
            'verdict_status': record.verdict_status,
            'dimension_scores': record.dimension_scores,
            'strengths': record.strengths,
            'concerns': record.concerns,
            'transcript': record.transcript,
            'topics_covered': record.topics_covered,
            'topics_total': record.topics_total,
            'total_responses': record.total_responses,
            'explorations': record.explorations,
            'duration_minutes': record.duration_minutes,
            'completion_status': record.completion_status,
            'phase': record.phase,
            'raw_evaluation': record.raw_evaluation,
            'created_at': record.created_at,
            'updated_at': record.updated_at,
        }

        logger.info(
            "[process id: %s][room id: %s] ✅ Retrieved HR report data for interview_id=%d",
            process_id, room_id, interview_id
        )
        return report_dict

    except Exception as e:
        logger.error(
            "[process id: %s][room id: %s] ❌ Error retrieving HR report data: %s",
            process_id, room_id, str(e)
        )
        return None

def insert_or_get_interview(interview_data, process_id: int = None, room_id: str = None):
    if not _IS_LOCAL:
        payload = {}
        # Convert snake_case keys to camelCase for Java API
        for k, v in interview_data.items():
            camel = SNAKE_TO_CAMEL_MAP.get(k, k)
            # Convert datetime to ISO string for JSON
            if isinstance(v, datetime):
                payload[camel] = v.isoformat()
            else:
                payload[camel] = v
        result = _api_call("POST", "/interview/create", json=payload, process_id=process_id, room_id=room_id)
        return result.get("id")
    # ── LOCAL: direct DB ──
    try:
        interview = Interview(**interview_data)
        session.add(interview)
        session.commit()
        logger.info("Inserted new interview.")
        return interview.id
    except IntegrityError:
        session.rollback()
        existing = session.query(Interview).filter_by(
            candidate_id=interview_data['candidate_id'],
            job_id=interview_data['job_id']
        ).first()
        if existing:
            logger.info(f"Duplicate interview found. Updating existing record. \n {existing} \n {interview_data}")
            # Update existing record with new data
            for key, value in interview_data.items():
                setattr(existing, key, value)
            session.commit()
            return existing.id
        else:
            logger.warning("Duplicate entry exception, but no existing record found.")
            return None
 
def update_interview(interview_id: int, update_data: Dict[str, Any], process_id: int = -10, room_id: str = "") -> None:
    if not _IS_LOCAL:
        import base64 as _b64
        payload = {"interviewId": interview_id, "fields": {}}
        for k, v in update_data.items():
            camel = SNAKE_TO_CAMEL_MAP.get(k, k)
            if isinstance(v, (bytes, bytearray)):
                payload["fields"][camel] = _b64.b64encode(v).decode("ascii")
            elif isinstance(v, datetime):
                payload["fields"][camel] = v.isoformat()
            else:
                payload["fields"][camel] = v
        _api_call("PUT", "/interview/update", json=payload, process_id=process_id, room_id=room_id)
        return
    # ── LOCAL: direct DB ──
    bool_keys = ['is_scheduled', 'is_email_sent', 'is_jd_interview_updated', 'is_jd_interview_result_updates']
    for key in bool_keys:
        if key in update_data:
            val = update_data[key]
            if isinstance(val, str):
                update_data[key] = val.lower() in ('true', '1', 't', 'yes')
            else:
                update_data[key] = bool(val)
    if 'interview_status' in update_data:
        update_data['interview_status'] = str(update_data['interview_status'])

    # ✅ FIX: Ensure HTML fields are STRINGS (not bytes)
    for html_field in ('summary_html', 'detailed_html', 'sentinel_html'):
        if html_field in update_data:
            val = update_data[html_field]
            if val is None:
                # explicit None means "set to NULL" — acceptable
                continue
            if not isinstance(val, str):  # ✅ Changed from bytes to str
                logger.warning(
                    "[process_id:%s][room_id:%s] Ignoring %s because it's not a string (type=%s).",
                    process_id, room_id, html_field, type(val).__name__
                )
                update_data.pop(html_field, None)  # drop invalid value
    
    # ✅ Ensure best_face field is bytes (if present)
    if 'best_face' in update_data:
        val = update_data['best_face']
        if val is None:
            # explicit None means "set to NULL" — acceptable
            pass
        elif not isinstance(val, (bytes, bytearray)):
            logger.warning(
                "[process_id:%s][room_id:%s] Ignoring best_face because it's not bytes (type=%s).",
                process_id, room_id, type(val).__name__
            )
            update_data.pop('best_face', None)  # drop invalid value

    # Avoid logging large HTML contents; log keys only and size info
    keys_list = list(update_data.keys())
    size_info = []
    for key in ['summary_html', 'detailed_html', 'sentinel_html']:
        if key in update_data and update_data[key]:
            size_info.append(f"{key}={len(update_data[key])} chars")
    
    size_str = f" ({', '.join(size_info)})" if size_info else ""
    logger.info("[process_id:%s][room_id:%s] Updating interview_id=%s with fields=%s%s",
                process_id, room_id, interview_id, keys_list, size_str)

    try:
        session.query(Interview).filter_by(id=interview_id).update(update_data)
        session.commit()
        logger.info("[process_id:%s][room_id:%s] Interview %s updated successfully.", process_id, room_id, interview_id)
    except Exception as e:
        session.rollback()
        logger.error("[process_id:%s][room_id:%s] Error updating interview %s: %s", process_id, room_id, interview_id, e)
        logger.error(traceback.format_exc())
        raise
 
def save_response(interview_data):
    try:
        interview = InterviewAnalysis(**interview_data)
        session.add(interview)
        session.commit()
        logger.info("Inserted new interview analysis.")
        return interview.id
    except IntegrityError:
        session.rollback()
        logger.warning("Duplicate entry exception for interview analysis, but no existing record found.")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3A: Thread-safe DB helpers (accept session parameter, never use global)
# ═══════════════════════════════════════════════════════════════════════════════

import re as _re


def insert_question_row(
    thread_session,
    interview_id: int,
    round_number: int,
    question_number: int,
    skill: str,
    effective_level: int,
    constraint_name: str,
    question_text: str,
    answer_text: str,
    candidate_id: int = None,
    job_id: int = None,
    process_id: str = None,
    room_id: str = None
) -> int:
    if not _IS_LOCAL:
        result = _api_call("POST", "/questions/add", json={
            "interviewId": interview_id,
            "candidateId": candidate_id,
            "jobId": job_id,
            "roundNumber": round_number,
            "questionNumber": question_number,
            "skill": skill,
            "effectiveLevel": effective_level,
            "constraintName": constraint_name or "None",
            "questionText": question_text,
            "answerText": answer_text,
        }, process_id=process_id, room_id=room_id)
        return result.get("id", 0)
    # ── LOCAL: direct DB ──
    """Synchronously insert a question row. Returns row ID.
    Called from the main thread before spawning evaluation."""
    row = InterviewQuestion(
        interview_id=interview_id,
        candidate_id=candidate_id,
        job_id=job_id,
        round_number=round_number,
        question_number=question_number,
        skill=skill,
        effective_level=effective_level,
        constraint_name=constraint_name or "None",
        question_text=question_text,
        answer_text=answer_text,
        evaluation_status="pending",
    )
    thread_session.add(row)
    thread_session.commit()
    logger.info(
        "📝 Inserted question row id=%s (interview=%s, candidate=%s, Q%s)",
        row.id, interview_id, candidate_id, question_number,
    )
    return row.id


def update_question_evaluation(
    thread_session,
    row_id: int,
    summary: str,
    confidence_score: float,
    evaluation_tags: list,
    status: str = "completed",
    process_id: str = None,
    room_id: str = None
) -> bool:
    if not _IS_LOCAL:
        try:
            safe_conf = float(confidence_score)
        except (ValueError, TypeError):
            safe_conf = 0.0
            
        safe_tags = []
        if isinstance(evaluation_tags, list):
            safe_tags = [str(t) for t in evaluation_tags]
        elif isinstance(evaluation_tags, str):
            safe_tags = [evaluation_tags]

        result = _api_call("PUT", "/questions/evaluate", json={
            "rowId": int(row_id) if row_id else 0,
            "summary": str(summary) if summary else "",
            "confidenceScore": safe_conf,
            "evaluationTags": safe_tags,
            "status": str(status),
        }, process_id=process_id, room_id=room_id)
        return result.get("updated", True)
    # ── LOCAL: direct DB ──
    """Idempotent update: only transitions pending → completed/failed.
    Called from background evaluation thread."""
    updated = thread_session.query(InterviewQuestion).filter(
        InterviewQuestion.id == row_id,
        InterviewQuestion.evaluation_status == "pending",  # idempotent guard
    ).update({
        "summary": summary,
        "confidence_score": confidence_score,
        "evaluation_tags": evaluation_tags,
        "evaluation_status": status,
        "evaluated_at": datetime.utcnow(),
    })
    thread_session.commit()
    if updated == 0:
        logger.warning(
            "⚠️ Row %s was not in 'pending' state — skipped update (idempotent guard)",
            row_id,
        )
    return updated > 0


def mark_evaluation_failed(thread_session, row_id: int, process_id: str = None, room_id: str = None) -> None:
    if not _IS_LOCAL:
        _api_call("PUT", "/questions/fail", json={"rowId": row_id}, process_id=process_id, room_id=room_id)
        return
    # ── LOCAL: direct DB ──
    """Mark evaluation as failed with timestamp."""
    thread_session.query(InterviewQuestion).filter(
        InterviewQuestion.id == row_id,
        InterviewQuestion.evaluation_status == "pending",
    ).update({
        "evaluation_status": "failed",
        "evaluated_at": datetime.utcnow(),
    })
    thread_session.commit()


def find_project(thread_session, candidate_id: int, job_id: int, normalized_name: str):
    """Find existing project by (candidate_id, job_id, normalized_name) for cross-round lookup."""
    return thread_session.query(InterviewProject).filter(
        InterviewProject.candidate_id == candidate_id,
        InterviewProject.job_id == job_id,
        InterviewProject.normalized_name == normalized_name,
    ).first()


def upsert_project(
    thread_session,
    interview_id: int,
    project: dict,
    question_number: int,
    round_number: int,
    candidate_id: int = None,
    job_id: int = None,
    process_id: str = None,
    room_id: str = None
) -> None:
    if not _IS_LOCAL:
        # The hub expects the project fields nested under a "project" object
        # (400 "The 'project' object is missing from the request payload." otherwise).
        result = _api_call("POST", "/projects/upsert", json={
            "interviewId": interview_id,
            "candidateId": candidate_id,
            "jobId": job_id,
            "questionNumber": question_number,
            "roundNumber": round_number,
            "project": {
                "projectName": project.get("name"),
                "normalizedName": project.get("normalized_name"),
                "rawContext": project.get("raw_context"),
            },
        }, process_id=process_id, room_id=room_id)
        return result.get("id")
    # ── LOCAL: direct DB ──
    """Insert new project or update last_mentioned fields if exists.
    Uses (candidate_id, job_id) for cross-round dedup."""
    raw_name = (project.get("name") or "unknown").strip()
    normalized = _re.sub(r'\s+', ' ', raw_name.lower())

    existing = find_project(thread_session, candidate_id, job_id, normalized)
    if existing:
        existing.last_mentioned_round = round_number
        existing.last_mentioned_question = question_number
        thread_session.commit()
        logger.info("🔄 Updated project '%s' last_mentioned Q%s R%s", normalized, question_number, round_number)
    else:
        row = InterviewProject(
            interview_id=interview_id,
            candidate_id=candidate_id,
            job_id=job_id,
            project_name=raw_name,
            normalized_name=normalized,
            tech_stack=project.get("tech_stack", []),
            ownership=project.get("ownership", ""),
            production_exposure=bool(project.get("production_exposure", False)),
            scale_context=project.get("scale_context", ""),
            first_mentioned_question=question_number,
            last_mentioned_round=round_number,
            last_mentioned_question=question_number,
        )
        thread_session.add(row)
        thread_session.commit()
        logger.info("🆕 Inserted project '%s' from Q%s (candidate=%s)", normalized, question_number, candidate_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Candidate live data cleanup (after all rounds complete)
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_candidate_live_data(candidate_id: int, job_id: int, process_id: str = None, room_id: str = "") -> dict:
    if not _IS_LOCAL:
        result = _api_call("DELETE", "/interview/live-cleanup", json={
            "candidateId": candidate_id,
            "jobId": job_id,
        }, process_id=process_id, room_id=room_id)
        return result
    # ── LOCAL: direct DB ──
    """Delete all entries for a candidate+job from interview_questions_live and interview_projects.
    Called after all rounds are complete and reports are finalized.
    Returns counts of deleted rows."""
    cleanup_session = Session()
    try:
        q_deleted = cleanup_session.query(InterviewQuestion).filter(
            InterviewQuestion.candidate_id == candidate_id,
            InterviewQuestion.job_id == job_id,
        ).delete(synchronize_session='fetch')

        p_deleted = cleanup_session.query(InterviewProject).filter(
            InterviewProject.candidate_id == candidate_id,
            InterviewProject.job_id == job_id,
        ).delete(synchronize_session='fetch')

        cleanup_session.commit()
        logger.info(
            "🧹 [process id: %s][room id: %s] Candidate %s (job=%s) cleanup complete: "
            "deleted %d questions, %d projects from live tables",
            process_id, room_id, candidate_id, job_id, q_deleted, p_deleted,
        )
        return {"questions_deleted": q_deleted, "projects_deleted": p_deleted}
    except Exception as e:
        cleanup_session.rollback()
        logger.error(
            "❌ [process id: %s][room id: %s] Candidate %s (job=%s) cleanup failed: %s",
            process_id, room_id, candidate_id, job_id, e,
        )
        return {"questions_deleted": 0, "projects_deleted": 0, "error": str(e)}
    finally:
        cleanup_session.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3B: Longitudinal skill baseline helpers
# ═══════════════════════════════════════════════════════════════════════════════

def load_skill_baselines(thread_session, candidate_id: int, process_id: int = None, room_id: str = None) -> dict:
    if not _IS_LOCAL:
        result = _api_call("GET", f"/skills/baseline/{candidate_id}", process_id=process_id, room_id=room_id)
        # result is expected to be {"baselines": {skill: {confidence, complexity, ...}}}
        return result.get("baselines", result) if isinstance(result, dict) else {}
    # ── LOCAL: direct DB ──
    """Load all skill baselines for a candidate. Returns {skill: row_dict}.
    Called once at interview start — no hot-path impact."""
    rows = thread_session.query(CandidateSkillProfile).filter(
        CandidateSkillProfile.candidate_id == candidate_id
    ).all()
    baselines = {}
    for row in rows:
        baselines[row.skill] = {
            "baseline_complexity": row.baseline_complexity,
            "baseline_confidence": row.baseline_confidence,
            "confidence_variance": row.confidence_variance,
            "total_interviews": row.total_interviews,
        }
    logger.info(
        "📊 Loaded %d skill baselines for candidate %s",
        len(baselines), candidate_id,
    )
    return baselines

def load_previous_round_answers(
    thread_session,
    candidate_id: int,
    job_id: int,
    current_round: int,
    process_id: str = None,
    room_id: str = None
) -> list:
    if not _IS_LOCAL:
        result = _api_call("GET", f"/questions/previous/{candidate_id}/{job_id}/{current_round}", process_id=process_id, room_id=room_id)
        return result if isinstance(result, list) else []
    # ── LOCAL: direct DB ──
    """Load answer texts from previous rounds for the same candidate+job.
    Used to seed the authenticity session so Round 2+ can detect from Q1.
    Returns list of answer_text strings ordered by (round_number, question_number).
    Filters out empty/very-short answers (< 20 words)."""
    if current_round <= 1:
        return []
    rows = (
        thread_session.query(InterviewQuestion.answer_text)
        .filter(
            InterviewQuestion.candidate_id == candidate_id,
            InterviewQuestion.job_id == job_id,
            InterviewQuestion.round_number < current_round,
        )
        .order_by(InterviewQuestion.round_number, InterviewQuestion.question_number)
        .all()
    )
    answers = [
        r.answer_text
        for r in rows
        if r.answer_text and len(r.answer_text.split()) >= 20
    ]
    logger.info(
        "📊 Loaded %d previous-round answers for candidate=%s job=%s (rounds < %d)",
        len(answers), candidate_id, job_id, current_round,
    )
    return answers


def update_skill_baseline(
    thread_session,
    candidate_id: int,
    skill: str,
    interview_avg_confidence: float,
    interview_avg_complexity: float,
    alpha: float = 0.3,
    process_id: str = None,
    room_id: str = None
) -> None:
    if not _IS_LOCAL:
        result = _api_call("POST", "/skills/update", json={
            "candidateId": candidate_id,
            "skill": skill,
            "interviewAvgConfidence": interview_avg_confidence,
            "interviewAvgComplexity": interview_avg_complexity,
            "alpha": alpha,
        }, process_id=process_id, room_id=room_id)
        return result
    # ── LOCAL: direct DB ──
    """Update longitudinal baseline using exponential smoothing.
    alpha=0.3 means 30% new data, 70% history. Called at interview end."""
    existing = thread_session.query(CandidateSkillProfile).filter(
        CandidateSkillProfile.candidate_id == candidate_id,
        CandidateSkillProfile.skill == skill,
    ).first()

    if existing:
        old_conf = existing.baseline_confidence
        new_conf = alpha * interview_avg_confidence + (1 - alpha) * old_conf
        new_complexity = alpha * interview_avg_complexity + (1 - alpha) * existing.baseline_complexity

        # Update variance (running estimate)
        delta = interview_avg_confidence - old_conf
        existing.confidence_variance = (
            (1 - alpha) * existing.confidence_variance + alpha * (delta ** 2)
        )

        existing.baseline_confidence = new_conf
        existing.baseline_complexity = new_complexity
        existing.total_interviews += 1
        existing.last_interview_at = datetime.utcnow()
        thread_session.commit()
        logger.info(
            "📈 Updated baseline for candidate=%s skill='%s': conf=%.3f→%.3f complexity=%.2f→%.2f var=%.4f",
            candidate_id, skill, old_conf, new_conf,
            existing.baseline_complexity, new_complexity,
            existing.confidence_variance,
        )
    else:
        row = CandidateSkillProfile(
            candidate_id=candidate_id,
            skill=skill,
            baseline_complexity=interview_avg_complexity,
            baseline_confidence=interview_avg_confidence,
            confidence_variance=0.0,
            total_interviews=1,
            last_interview_at=datetime.utcnow(),
        )
        thread_session.add(row)
        thread_session.commit()
        logger.info(
            "🆕 Created baseline for candidate=%s skill='%s': conf=%.3f complexity=%.2f",
            candidate_id, skill, interview_avg_confidence, interview_avg_complexity,
        )


# ═══════════════════════════════════════════════════════════════════════════
# HR Interview Engine — DB Helpers
# ═══════════════════════════════════════════════════════════════════════════

def load_default_dimensions(thread_session, process_id: str = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("GET", "/dimensions/default", process_id=process_id, room_id=room_id)
        return result if isinstance(result, list) else []
    # ── LOCAL: direct DB ──
    """Load all system-default evaluation dimensions."""
    return (
        thread_session.query(EvaluationDimension)
        .filter(EvaluationDimension.is_default == True)
        .all()
    )


def load_account_dimensions(thread_session, account_id, process_id: str = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("GET", f"/dimensions/account/{account_id}", process_id=process_id, room_id=room_id)
        return result if isinstance(result, list) else []
    # ── LOCAL: direct DB ──
    """Load recruiter/company custom evaluation dimensions."""
    if not account_id:
        return []
    return (
        thread_session.query(EvaluationDimension)
        .filter(EvaluationDimension.account_id == account_id)
        .all()
    )


def attach_dimensions_to_interview(thread_session, interview_id, dimensions, process_id: str = None, room_id: str = None):
    if not _IS_LOCAL:
        dims = []
        for dim in dimensions:
            dims.append({
                "dimensionId": dim.id if hasattr(dim, 'id') else dim.get("id"),
                "dimensionKey": dim.dimension_key if hasattr(dim, 'dimension_key') else dim.get("dimension_key"),
                "dimensionName": dim.dimension_key if hasattr(dim, 'dimension_key') else dim.get("dimension_key"),
            })
        _api_call("POST", "/dimensions/attach", json={
            "interviewId": interview_id,
            "dimensions": dims,
        }, process_id=process_id, room_id=room_id)
        return
    # ── LOCAL: direct DB ──
    """Attach dimensions to an interview with name/key snapshots.
    `dimensions` is a list of EvaluationDimension objects."""
    for dim in dimensions:
        row = InterviewDimension(
            interview_id=interview_id,
            dimension_id=dim.id,
            dimension_key=dim.dimension_key,
            dimension_name=dim.dimension_name,
        )
        thread_session.add(row)
    thread_session.commit()
    logger.info(
        "📎 Attached %d dimensions to interview %s",
        len(dimensions), interview_id,
    )


def save_raw_evaluation_report(thread_session, interview_id, report_json, process_id: str = None, room_id: str = None):
    if not _IS_LOCAL:
        # Combined with save_dimension_evaluations via /dimensions/evaluate
        # But also support standalone call
        _api_call("POST", "/dimensions/evaluate", json={
            "interviewId": interview_id,
            "reportJson": report_json,
            "dimensionScores": [],
        }, process_id=process_id, room_id=room_id)
        return
    # ── LOCAL: direct DB ──
    """Store raw LLM evaluation JSON for debugging."""
    row = InterviewEvaluationReport(
        interview_id=interview_id,
        report_json=report_json,
    )
    thread_session.add(row)
    thread_session.commit()
    logger.info("💾 Saved raw evaluation report for interview %s", interview_id)
    return row.id


def save_dimension_evaluations(thread_session, interview_id, dimension_scores, process_id: str = None, room_id: str = None):
    if not _IS_LOCAL:
        scores = []
        for ds in dimension_scores:
            scores.append({
                "dimensionId": ds.get("dimension_id"),
                "score": ds.get("score"),
                "justification": ds.get("justification"),
            })
        _api_call("POST", "/dimensions/evaluate", json={
            "interviewId": interview_id,
            "dimensionScores": scores,
        }, process_id=process_id, room_id=room_id)
        return
    # ── LOCAL: direct DB ──
    """Save per-dimension scores. `dimension_scores` is a list of
    dicts: [{"dimension_id": int, "score": float, "justification": str}]."""
    for ds in dimension_scores:
        row = InterviewEvaluationResult(
            interview_id=interview_id,
            dimension_id=ds["dimension_id"],
            score=ds["score"],
            justification=ds.get("justification", ""),
        )
        thread_session.add(row)
    thread_session.commit()
    logger.info(
        "📊 Saved %d dimension evaluations for interview %s",
        len(dimension_scores), interview_id,
    )


def load_interview_evaluations(thread_session, interview_id, process_id: str = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("GET", f"/evaluations/{interview_id}", process_id=process_id, room_id=room_id)
        return result if isinstance(result, list) else []
    # ── LOCAL: direct DB ──
    """Load all dimension evaluation results for an interview."""
    rows = (
        thread_session.query(InterviewEvaluationResult)
        .filter(InterviewEvaluationResult.interview_id == interview_id)
        .all()
    )
    return [
        {
            "dimension_id": r.dimension_id,
            "score": r.score,
            "justification": r.justification,
        }
        for r in rows
    ]


def insert_or_get_recruiter(recruiter_data, process_id: int = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("POST", "/recruiter/upsert", json={
            "recruiterJdId": recruiter_data.get("recruiter_jd_id"),
            "recruiterName": recruiter_data.get("recruiter_name"),
            "recruiterEmail": recruiter_data.get("recruiter_email"),
            "isActive": recruiter_data.get("is_active", True),
        }, process_id=process_id, room_id=room_id)
        return result.get("id")
    # ── LOCAL: direct DB ──
    """
    Insert a new recruiter or get the existing recruiter by email.
    Prevents duplicate recruiter entries based on recruiter_email.
    """
    existing = session.query(Recruiter).filter_by(
        recruiter_email=recruiter_data['recruiter_email']
    ).first()
    if existing:
        logger.info(
            "Duplicate recruiter found for email: %s",
            recruiter_data['recruiter_email']
        )
        return existing.id
    try:
        recruiter = Recruiter(**recruiter_data)
        session.add(recruiter)
        session.commit()
        logger.info("Inserted new recruiter.")
        return recruiter.id
    except IntegrityError:
        session.rollback()
        logger.warning(
            "IntegrityError occurred while inserting recruiter with email: %s",
            recruiter_data['recruiter_email']
        )
        return None
 

def generate_authorship_summary(interview_id: int, process_id: int = 0, room_id: str = "") -> dict:
    """
    Generate authorship confidence summary from session signals.
    
    SCORING INTERPRETATION:
    - HIGH score (65-100%) = Clearly human (authentic, has stuttering/filler words)
    - MEDIUM score (40-64%) = Mixed signals
    - LOW score (0-39%) = Suspicious, likely AI-generated (too polished)
    
    Args:
        interview_id: Interview ID to generate summary for
        process_id: Process ID for logging
        room_id: Room ID for logging
    
    Returns:
        Dictionary with authorship analysis summary
    """
    import json
    from pathlib import Path
    from statistics import mean
    
    session_file = Path(f"sessions/session_interview_{interview_id}.json")
    
    if not session_file.exists():
        logger.warning(
            "[process_id:%s][room_id:%s] Session file not found for interview %s",
            process_id, room_id, interview_id
        )
        return {"error": "session_not_found", "score": None}
    
    try:
        with open(session_file, 'r') as f:
            session_data = json.load(f)
        
        signals = session_data.get("signals", [])
        answers = session_data.get("answers", [])
        questions = session_data.get("questions", [])
        
        if not signals:
            return {"error": "no_signals", "score": None}
        
        # Extract answer signals
        # Skip index 0 (Q2 = first substantive answer) - always 0% due to "no baseline"
        # This avoids penalizing overall score for the background question
        answer_signals = [s.get("answer_signal", 0) for s in signals]
        # Only include signals from Q3+ for overall score (skip Q2 at index 0)
        analyzed_signals = [s for i, s in enumerate(answer_signals) if s > 0 and i > 0]
        
        # Helper to clean answer text
        def clean_answer(text):
            if not text:
                return ""
            return text.replace('\n', ' ').strip()
        
        # =====================================================
        # MUTE STREAK DETECTION
        # Use actual mute_events stored during interview
        # Plus fallback to short answers (< 10 words)
        # =====================================================
        mute_events = session_data.get("mute_events", [])
        mute_questions = []  # Questions where mute/exit occurred
        
        # Get questions with actual mute/exit events from interview
        mute_event_questions = {e.get("question_number", 0) for e in mute_events}
        
        # Also detect short answers (< 10 words) as silent
        short_answer_streak = 0
        streak_start = None
        
        for i, sig in enumerate(signals):
            question_num = i + 1
            answer = answers[i] if i < len(answers) else ""
            word_count = len(answer.split()) if answer else 0
            is_short_or_mute = word_count < 10 or question_num in mute_event_questions
            
            if is_short_or_mute:
                short_answer_streak += 1
                if short_answer_streak == 1:
                    streak_start = question_num
            else:
                # If we had a streak of 3+, flag from 3rd question onwards
                if short_answer_streak >= 3:
                    for q in range(streak_start + 2, question_num):
                        mute_questions.append(q)
                short_answer_streak = 0
                streak_start = None
        
        # Check if we ended on a streak
        if short_answer_streak >= 3:
            for q in range(streak_start + 2, len(signals) + 1):
                mute_questions.append(q)
        
        # Convert to 0-indexed for internal use
        mute_question_indices = [q - 1 for q in mute_questions]
        mute_streak_detected = len(mute_questions) > 0 or len(mute_events) >= 3
        
        # =====================================================
        # SCORING: HIGH = Human, LOW = AI (inverted from old logic)
        # Now signal directly represents authenticity
        # =====================================================
        base_score = 50.0  # Default
        suspicious_count = 0
        
        if analyzed_signals:
            # Average authenticity score (higher = more human)
            avg_signal = mean(analyzed_signals)
            base_score = avg_signal * 100  # Convert to percentage
            # Count LOW-scoring (suspicious/AI-like) answers
            suspicious_count = sum(1 for s in analyzed_signals if s < 0.4)
        
        # =====================================================
        # MUTE PENALTY: If score < 50% AND 3+ mutes, apply -20%
        # This ensures combined suspicious + mute = very low score
        # =====================================================
        mute_count = len(mute_events)
        if base_score < 50 and mute_count >= 3:
            base_score = base_score - 20
            logger.info(
                f"[process_id:{process_id}][room_id:{room_id}] "
                f"Applied -20% mute penalty (score < 50% + {mute_count} mutes): {base_score + 20:.1f}% → {base_score:.1f}%"
            )
        
        base_score = max(0, min(100, base_score))
        
        # =====================================================
        # BUILD PER-QUESTION METRICS AND EVIDENCE
        # =====================================================
        question_flags = {}
        flags_list = []
        
        # Find baseline answer for comparison
        baseline_answer = None
        baseline_idx = None
        for idx, sig in enumerate(signals):
            if sig.get("answer_signal", 0) > 0 and idx < len(answers):
                baseline_answer = answers[idx]
                baseline_idx = idx
                break
        
        for i, sig in enumerate(signals):
            q_num = f"Q{i+1}"
            answer_sig = sig.get("answer_signal", 0)
            anomaly = sig.get("anomaly_flags", [])
            current_answer = answers[i] if i < len(answers) else ""
            current_question = questions[i] if i < len(questions) else f"Question {i+1}"
            semantic_result = sig.get("semantic_result", {})
            observations = semantic_result.get("observations", []) if semantic_result else []
            
            # Determine flag (HIGH score = authentic, LOW score = suspicious)
            if answer_sig == 0:
                flag = "skipped"
                interpretation = "No analysis (skipped)"
            elif answer_sig >= 0.65:
                flag = "authentic"
                interpretation = "Clearly human (stuttering, filler words detected)"
            elif answer_sig >= 0.4:
                flag = "moderate"
                interpretation = "Mixed signals"
            else:
                flag = "suspicious"
                interpretation = "Suspicious (too polished, AI-like)"
            
            # Build per-question data
            q_data = {
                "question": current_question[:200] if current_question else "",
                "answer_preview": clean_answer(current_answer)[:300] if current_answer else "",
                "authenticity_score": round(answer_sig * 100, 1),
                "flag": flag,
                "interpretation": interpretation,
                "anomaly_flags": anomaly
            }
            
            # Add mute streak warning if applicable
            if i in mute_question_indices or (i + 1) in [e.get("question_number", 0) for e in mute_events]:
                q_data["mute_streak_warning"] = "⚠️ Part of mute streak (3+ consecutive short/no answers)"
            
            # Add evidence for suspicious answers
            if flag == "suspicious":
                q_data["evidence"] = {
                    "full_answer": clean_answer(current_answer),
                    "reason": observations[0] if observations else "Answer appears too polished for natural speech",
                    "observations": observations[:3]
                }
                # Add comparison if we have baseline
                if baseline_answer and baseline_idx is not None and i != baseline_idx:
                    q_data["evidence"]["compared_to"] = f"Q{baseline_idx + 1}"
                    q_data["evidence"]["baseline_answer"] = clean_answer(baseline_answer)[:300]
                
                # Add mute streak context if applicable
                if i in mute_question_indices or (i + 1) in [e.get("question_number", 0) for e in mute_events]:
                    q_data["evidence"]["mute_streak_context"] = "User was silent/mute for 3+ consecutive questions"
            
            # Add observations even for moderate flags
            if flag == "moderate" and observations:
                q_data["observations"] = observations[:3]
            
            question_flags[q_num] = q_data
        
        # =====================================================
        # FLAGS AND WARNINGS
        # =====================================================
        
        # Mute streak flag
        if mute_streak_detected:
            mute_event_q_nums = [e.get("question_number", 0) for e in mute_events]
            all_mute_qs = sorted(set(mute_questions + mute_event_q_nums))
            flags_list.append(f"⚠️ Mute streak detected: User was silent at questions: {all_mute_qs}")
        
        # Suspicious answer cluster
        if suspicious_count >= 3:
            flags_list.append(f"🚨 {suspicious_count} answers flagged as likely AI-assisted")
        
        # Low authenticity ratio
        if analyzed_signals:
            authentic_count = sum(1 for s in analyzed_signals if s >= 0.65)
            if authentic_count < len(analyzed_signals) * 0.5:
                flags_list.append(f"⚠️ Only {authentic_count}/{len(analyzed_signals)} answers show clear human markers")
        
        # Determine confidence band (HIGH = good/human, LOW = bad/AI)
        if base_score >= 65:
            confidence_band = "High (Authentic)"
        elif base_score >= 40:
            confidence_band = "Medium"
        else:
            confidence_band = "Low (Suspicious)"
        
        # =====================================================
        # BUILD FINAL SUMMARY
        # =====================================================
        summary = {
            "authenticity_score": round(base_score, 1),
            "confidence_band": confidence_band,
            "interpretation": (
                "Clearly human - natural speech patterns detected" if base_score >= 65 else
                "Mixed signals - some answers may need review" if base_score >= 40 else
                "Suspicious - answers appear too polished, possible AI assistance"
            ),
            "total_questions": len(answers),
            "analyzed_questions": len(analyzed_signals),
            "skipped_questions": len(answer_signals) - len(analyzed_signals),
            "suspicious_count": suspicious_count,
            "mute_streak_detected": mute_streak_detected,
            "flags": flags_list,
            "question_flags": question_flags,
            "breakdown": {
                "avg_authenticity": round(mean(analyzed_signals) * 100, 1) if analyzed_signals else 0,
                "suspicious_count": suspicious_count,
                "authentic_count": sum(1 for s in analyzed_signals if s >= 0.65) if analyzed_signals else 0,
                "moderate_count": sum(1 for s in analyzed_signals if 0.4 <= s < 0.65) if analyzed_signals else 0,
                "mute_questions": [i+1 for i in mute_questions]
            },
            "generated_at": datetime.utcnow().isoformat()
        }
        
        logger.info(
            "[process_id:%s][room_id:%s] ✅ Generated authorship summary for interview %s: authenticity=%.1f%% (%s)",
            process_id, room_id, interview_id, base_score, confidence_band
        )
        
        return summary
        
    except Exception as e:
        logger.error(
            "[process_id:%s][room_id:%s] ❌ Error generating authorship summary: %s",
            process_id, room_id, str(e)
        )
        import traceback
        logger.error(traceback.format_exc())
        return {"error": str(e), "score": None}

 
# === Sample usage ===
 
# # Insert Candidate
# candidate_data = {
#     "id": 1001,
#     "reference_number": "C-123",
#     "name": "Alice Johnson",
#     "email": "alice@example.com",
#     "resume_text": "Experienced Python Developer..."
# }
# candidate_id = insert_or_get_candidate(candidate_data)
 
# # Update Candidate
# update_candidate(candidate_id, {"name": "Alice J. Johnson", "resume_text": "Updated Resume Text"})
 
# # Insert Job
# job_data = {
#     "id": 2001,
#     "reference_number": "J-456",
#     "title": "Software Engineer",
#     "posting_title": "Python Backend Developer",
#     "description": "Develop backend services.",
#     "company": "TechCorp",
#     "city": "Hyderabad",
#     "state": "Telangana",
#     "country": "India"
# }
# job_id = insert_or_get_job(job_data)
 
# # Update Job
# update_job(job_id, {"description": "Updated job description", "job_status": JobStatus.Open})
 
# # Insert Interview
# interview_data = {
#     "candidate_id": candidate_id,
#     "job_id": job_id,
#     "interview_time": datetime(2025, 4, 20, 15, 0),
#     "recruiter_name": "Bob Recruiter",
#     "recruiter_email": "bob@example.com",
#     "interview_questions": {"q1": "Tell me about yourself"},
#     "resume_match_score": 85.5,
#     "resume_job_analysis": "Good match",
#     "interview_score": 0.0,
#     "interview_analysis": "",
#     "verdict": "",
#     "interview_status": InterviewStatus.Not_Started
# }
# interview_id = insert_or_get_interview(interview_data)
 
# # Update Interview
# update_interview(interview_id, {
#     "interview_score": 8.7,
#     "interview_analysis": "Strong technical answers",
#     "verdict": "Pass",
#     "interview_status": InterviewStatus.Analyzed
# })
 
 
   
def get_interview_by_candidate_and_job(candidate_id, job_id, process_id: int = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("GET", f"/interview/{candidate_id}/{job_id}", process_id=process_id, room_id=room_id)
        return result if result else None
    # ── LOCAL: direct DB ──
    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor() as cursor:
            query = """
                SELECT
                    i.id,
                    i.candidate_id,
                    c.candidate_name,
                    i.job_id,
                    j.job_title,
                    j.company_name,
                    i.interview_time,
                    i.interview_score,
                    i.interview_verdict,
                    i.interview_status,
                    i.zoom_host_url,
                    i.zoom_join_url,
                    i.recruiter_join_url,
                    i.candidate_join_url,
                    i.bot_join_url,
                    i.is_reminder_email_sent
                FROM interviews i
                LEFT JOIN candidates c ON i.candidate_id = c.id
                LEFT JOIN jobs j ON i.job_id = j.id
                WHERE i.candidate_id = %s AND i.job_id = %s
                ORDER BY i.interview_time DESC
                LIMIT 1
            """
            cursor.execute(query, (candidate_id, job_id))
            return cursor.fetchone()
    finally:
        connection.close()
            
 
# aira_sql.py (with corrected job title mapping and empty candidate location)
 
# aira_sql.py (final version with job_description fix)
 
# aira_sql.py (final version with job_title only)
 
# aira_sql.py (fixed job_title propagation to Job Title column)
 
# aira_sql.py (final Job Title column logic with aligned column names)
 
# aira_sql.py (job_title fix and header row grouping enabled for history)
import pytz
from datetime import datetime, timedelta
import pandas as pd
def get_all_interviews(process_id: int = None, room_id: str = None):
    if not _IS_LOCAL:
        result = _api_call("GET", "/interviews", process_id=process_id, room_id=room_id)
        return result if isinstance(result, list) else []
    # ── LOCAL: direct DB ──
    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT
                    i.id,
                    i.candidate_id,
                    c.candidate_name,
                    i.job_id,
                    j.job_title AS job_title,
                    r.recruiter_name,
                    i.interview_time,
                    i.interview_timezone,
                    i.interview_score,
                    i.interview_verdict,
                    i.interview_status
                FROM interviews i
                LEFT JOIN candidates c ON i.candidate_id = c.id
                LEFT JOIN jobs j ON i.job_id = j.id
                LEFT JOIN recruiters r ON i.recruiter_id = r.id
                ORDER BY i.interview_time DESC
            """)
            return cursor.fetchall()
    finally:
        connection.close()
from datetime import datetime, timedelta
import pytz
import pandas as pd
 
def generate_available_slots(interviews, timezone="Asia/Kolkata", days=14, mode="future", grouped=False):
    local_tz = pytz.timezone(timezone)
    now = datetime.now(local_tz)
    end = now + timedelta(days=days) if mode == "future" else now - timedelta(days=days)
 
    # Filter interviews by mode (past/future)
    filtered = []
    for row in interviews:
        dt = row["interview_time"].astimezone(local_tz)
        if mode == "future" and now <= dt <= end:
            row["interview_dt_local"] = dt
            filtered.append(row)
        elif mode == "past" and end <= dt <= now:
            row["interview_dt_local"] = dt
            filtered.append(row)
 
    # Sort filtered rows by datetime (descending for past)
    filtered.sort(key=lambda x: x["interview_dt_local"], reverse=(mode == "past"))
 
    # Group rows by date string
    grouped_data = {}
    for row in filtered:
        dt = row["interview_dt_local"]
        date_str = dt.strftime("%d-%b-%y")  # e.g. 15-Jul-25
        time_str = dt.strftime("%I:%M %p")  # e.g. 09:30 AM
 
        if date_str not in grouped_data:
            grouped_data[date_str] = []
 
        grouped_data[date_str].append({
            "Interview Time": time_str,
            "Candidate Name": row.get("candidate_name", ""),
            "Job Title": row.get("job_title", ""),
            "Assigned Recruiter": row.get("recruiter_name", ""),
            "Interview Status": row.get("interview_status", ""),
            "Interview Verdict": row.get("interview_verdict", ""),
            "Interview Score": row.get("interview_score", ""),
            "Booking Status": "BOOKED"
        })
 
    # ✅ Fix: sort date strings using actual datetime comparison
    final_rows = []
    sorted_dates = sorted(
        grouped_data.keys(),
        key=lambda d: datetime.strptime(d, "%d-%b-%y"),
        reverse=(mode == "past")
    )
    for date in sorted_dates:
        if grouped:
            final_rows.append({"Interview Date": date})
        final_rows.extend(grouped_data[date])
 
    return final_rows

def fetch_one(query, params=None):
    """Run a query and return the first row as a dict, or None."""
    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params or ())
            return cursor.fetchone()
    finally:
        connection.close()


def execute(query, params=None):
    """Run a modifying query (INSERT/UPDATE/DELETE)."""
    connection = pymysql.connect(**DB_CONFIG)
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params or ())
        connection.commit()
    finally:
        connection.close()
# Companies whose interviews are test data — never archived, only reset to
# 'Not Started' so the same rows are reusable across load-test runs.
_NO_ARCHIVE_COMPANIES = {
    c.strip().upper()
    for c in os.getenv("AIRA_NO_ARCHIVE_COMPANIES", "HIT,abs").split(",")
    if c.strip()
}

def _interview_for_room(room_id):
    """Return {'id', 'company_name'} for an interview's room_id, or None.

    LOCAL only: direct DB join. In API mode this returns None on purpose — the
    hub has no working per-room lookup (GET /interviews 500s), so callers must
    pass company_name explicitly (the orchestrator already holds it)."""
    if not _IS_LOCAL:
        return None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT i.id AS id, j.company_name AS company_name
                FROM interviews i
                JOIN jobs j ON j.id = i.job_id
                WHERE i.room_id = %s
                LIMIT 1
            """, (room_id,))
            row = cursor.fetchone()
        connection.close()
        return dict(row) if row else None
    except Exception:
        return None

def _is_no_archive_company(company_name):
    """True if a company name is in the never-archive test set (HIT/abs)."""
    return (company_name or "").upper() in _NO_ARCHIVE_COMPANIES

def _is_no_archive_interview(room_id, _row=None):
    """True if this interview belongs to a test company (e.g. HIT, abs) that
    must never be archived — only reset to 'Not Started'."""
    row = _row if _row is not None else _interview_for_room(room_id)
    return bool(row) and _is_no_archive_company(row.get("company_name"))

# Backwards-compatible alias (kept for any external callers).
def _is_hit_interview(room_id):
    return _is_no_archive_interview(room_id)

def archive_and_delete_interview(room_id, process_id=0, company_name=None, interview_id=None):
    # ── Test-company guard: never archive HIT/abs interviews, just reset ──
    # company_name (and interview_id) should be passed by the caller — in API
    # mode the hub has no per-room lookup, so we rely on the interview dict the
    # orchestrator already holds. Fall back to a local DB lookup when possible.
    _guard_row = None
    if company_name is None or (not _IS_LOCAL and interview_id is None):
        _guard_row = _interview_for_room(room_id)
    if company_name is None and _guard_row:
        company_name = _guard_row.get("company_name")
    if interview_id is None and _guard_row:
        interview_id = _guard_row.get("id")

    if _is_no_archive_company(company_name):
        logger.info(
            "[process id: %s][room id: %s] 🧪 Test-company interview (%s) — skipping archive, resetting to 'Not Started'.",
            process_id, room_id, company_name
        )
        try:
            if _IS_LOCAL:
                connection = pymysql.connect(**DB_CONFIG)
                with connection.cursor() as cursor:
                    cursor.execute("""
                        UPDATE interviews
                        SET interview_status = 'Not Started',
                            updated_at       = NOW()
                        WHERE room_id = %s
                    """, (room_id,))
                connection.commit()
                connection.close()
            elif interview_id:
                update_interview(interview_id, {"interview_status": "Not Started"},
                                 process_id=process_id, room_id=room_id)
            else:
                logger.warning("[process id: %s][room id: %s] no interview_id — cannot reset test interview in API mode.",
                               process_id, room_id)
        except Exception as e:
            logger.warning("[process id: %s][room id: %s] test-company reset failed: %s", process_id, room_id, e)
        return False, None   # Return False so caller knows it wasn't archived

    if not _IS_LOCAL:
        try:
            result = _api_call("POST", "/interview/archive", json={"roomId": room_id}, process_id=process_id, room_id=room_id)
            if result and result.get("success"):
                return True, result.get("historyId")
            return False, None
        except Exception as e:
            logger.error(f"❌ API archive failed: {e}")
            return False, None
            
    # ── LOCAL: direct DB ──
    """
    Archive interview to interview_history and delete from interviews table.
    Now includes summary_html, detailed_html, sentinel_html, and best_face.
    """
    # Create a new session to avoid state issues
    local_session = Session()
    
    try:
        # Query the interview
        interview = local_session.query(Interview).filter_by(room_id=room_id).first()

        if not interview:
            logger.warning(
                "[process id: %s][room id: %s] No interview found with room_id '%s' to archive.",
                process_id, room_id, room_id
            )
            local_session.close()
            return False, None

        logger.info(
            "[process id: %s][room id: %s] Found interview (ID: %d) to archive",
            process_id, room_id, interview.id
        )

        # Prepare history data
        history_data = {
            'interview_id': interview.id,  # Store original interview ID
            'candidate_id': interview.candidate_id,
            'job_id': interview.job_id,
            'recruiter_id': interview.recruiter_id,
            'interview_time': interview.interview_time,
            'interview_timezone': interview.interview_timezone,
            'room_id': interview.room_id,
            'recruiter_name': interview.recruiter_name,
            'recruiter_email': interview.recruiter_email,
            'interview_mode': interview.interview_mode,
            'zone': interview.zone,
            'resume_text': interview.resume_text,
            'resume_match_score': interview.resume_match_score,
            'resume_job_analysis': interview.resume_job_analysis,
            'interview_score': interview.interview_score,
            'interview_analysis': interview.interview_analysis,
            'interview_verdict': interview.interview_verdict,
            'interview_status': interview.interview_status,
            'is_scheduled': interview.is_scheduled,
            'is_email_sent': interview.is_email_sent,
            'is_jd_interview_updated': interview.is_jd_interview_updated,
            'is_jd_interview_result_updates': interview.is_jd_interview_result_updates,
            'is_reminder_email_sent': interview.is_reminder_email_sent,
            'mandatory_skills': interview.mandatory_skills,
            'optional_skills': interview.optional_skills,
            'has_mandatory_skills': interview.has_mandatory_skills,
            'recording_enabled': interview.recording_enabled,
            'sentinel_enabled': interview.sentinel_enabled,
            'recording_path': interview.recording_path,
            'summary_html': interview.summary_html,
            'detailed_html': interview.detailed_html,
            'sentinel_html': interview.sentinel_html,
            'best_face': interview.best_face,
            'created_at': interview.interview_time,
            'archived_at': datetime.utcnow(),
            'original_resume_text': interview.original_resume_text,
            'authorship_scores_summary': interview.authorship_scores_summary,
            'email_config': interview.email_config,
            'interview_config': interview.interview_config,
            'round_type': interview.round_type,
            'current_round_number': interview.current_round_number,
        }

        # Check if history already exists
        existing_history = local_session.query(InterviewHistory).filter_by(room_id=room_id).first()

        if existing_history:
            logger.info(
                "[process id: %s][room id: %s] Interview already exists in history. Updating...",
                process_id, room_id
            )
            for key, value in history_data.items():
                setattr(existing_history, key, value)
            history_id = existing_history.id
        else:
            history = InterviewHistory(**history_data)
            local_session.add(history)
            local_session.flush()  # Get the ID before commit
            history_id = history.id
            logger.info(
                "[process id: %s][room id: %s] Created new interview history record (ID: %d)",
                process_id, room_id, history_id
            )

        # Store interview_id before deletion
        interview_id = interview.id
        
        # Delete the original interview
        local_session.delete(interview)
        
        # Commit all changes in one transaction
        local_session.commit()

        logger.info(
            "[process id: %s][room id: %s] ✅ Successfully archived interview (ID: %d) → history (ID: %d) and deleted from interviews",
            process_id, room_id, interview_id, history_id
        )

        local_session.close()
        return True, history_id

    except IntegrityError as e:
        local_session.rollback()
        logger.error(
            "[process id: %s][room id: %s] ❌ IntegrityError during archiving: %s",
            process_id, room_id, str(e)
        )
        logger.error(traceback.format_exc())
        local_session.close()
        return False, None
        
    except Exception as e:
        local_session.rollback()
        logger.error(
            "[process id: %s][room id: %s] ❌ Unexpected error during archiving: %s",
            process_id, room_id, str(e)
        )
        logger.error(traceback.format_exc())
        local_session.close()
        return False, None
    finally:
        # Ensure session is closed
        if local_session:
            local_session.close()
            
def archive_and_delete_interview_sql(room_id, process_id=0):
    """
    Archive interview using raw SQL for better reliability.
    """
    connection = None
    try:
        connection = pymysql.connect(**DB_CONFIG)
        
        with connection.cursor() as cursor:
            # First, check if interview exists
            cursor.execute(
                "SELECT * FROM interviews WHERE room_id = %s",
                (room_id,)
            )
            interview = cursor.fetchone()
            
            if not interview:
                logger.warning(
                    "[process id: %s][room id: %s] No interview found with room_id '%s'",
                    process_id, room_id, room_id
                )
                return False, None
            
            logger.info(
                "[process id: %s][room id: %s] Found interview (ID: %d) to archive",
                process_id, room_id, interview['id']
            )
            
            # Check if already archived
            cursor.execute(
                "SELECT id FROM interview_history WHERE room_id = %s",
                (room_id,)
            )
            existing = cursor.fetchone()
            
            if existing:
                # Update existing history
                logger.info(
                    "[process id: %s][room id: %s] Updating existing history record",
                    process_id, room_id
                )
                cursor.execute("""
                    UPDATE interview_history SET
                        interview_id = %s,
                        candidate_id = %s,
                        job_id = %s,
                        recruiter_id = %s,
                        interview_time = %s,
                        interview_timezone = %s,
                        recruiter_name = %s,
                        recruiter_email = %s,
                        interview_mode = %s,
                        zone = %s,
                        resume_text = %s,
                        resume_match_score = %s,
                        resume_job_analysis = %s,
                        interview_score = %s,
                        interview_analysis = %s,
                        interview_verdict = %s,
                        interview_status = %s,
                        is_scheduled = %s,
                        is_email_sent = %s,
                        is_jd_interview_updated = %s,
                        is_jd_interview_result_updates = %s,
                        is_reminder_email_sent = %s,
                        mandatory_skills = %s,
                        optional_skills = %s,
                        has_mandatory_skills = %s,
                        recording_enabled = %s,
                        sentinel_enabled = %s,
                        recording_path = %s,
                        summary_html = %s,
                        detailed_html = %s,
                        sentinel_html = %s,
                        best_face = %s,
                        archived_at = %s,
                        original_resume_text = %s,
                        authorship_scores_summary = %s,
                        interview_config = %s,
                        round_type = %s,
                        current_round_number = %s
                    WHERE room_id = %s
                """, (
                    interview['id'],  # interview_id
                    interview['candidate_id'],
                    interview['job_id'],
                    interview['recruiter_id'],
                    interview['interview_time'],
                    interview['interview_timezone'],
                    interview['recruiter_name'],
                    interview['recruiter_email'],
                    interview['interview_mode'],
                    interview['zone'],
                    interview['resume_text'],
                    interview['resume_match_score'],
                    interview['resume_job_analysis'],
                    interview['interview_score'],
                    interview['interview_analysis'],
                    interview['interview_verdict'],
                    interview['interview_status'],
                    interview['is_scheduled'],
                    interview['is_email_sent'],
                    interview['is_jd_interview_updated'],
                    interview['is_jd_interview_result_updates'],
                    interview['is_reminder_email_sent'],
                    interview.get('mandatory_skills'),
                    interview.get('optional_skills'),
                    interview.get('has_mandatory_skills'),
                    interview['recording_enabled'],
                    interview['sentinel_enabled'],
                    interview.get('recording_path'),
                    interview['summary_html'],
                    interview['detailed_html'],
                    interview['sentinel_html'],
                    interview['best_face'],
                    interview['original_resume_text'],
                    datetime.utcnow(),
                    interview.get('authorship_scores_summary'),
                    interview.get('interview_config'),
                    interview.get('round_type', 'technical'),
                    interview.get('current_round_number', 1),
                    room_id
                ))
                history_id = existing['id']
            else:
                # Insert new history record
                logger.info(
                    "[process id: %s][room id: %s] Creating new history record",
                    process_id, room_id
                )
                import json
                cursor.execute("""
                    INSERT INTO interview_history (
                        interview_id, candidate_id, job_id, recruiter_id, interview_time,
                        interview_timezone, room_id, recruiter_name, recruiter_email,
                        interview_mode, zone, resume_text, resume_match_score,
                        resume_job_analysis, interview_score, interview_analysis,
                        interview_verdict, interview_status, is_scheduled,
                        is_email_sent, is_jd_interview_updated, is_jd_interview_result_updates,
                        is_reminder_email_sent, mandatory_skills, optional_skills, has_mandatory_skills,
                        recording_enabled, sentinel_enabled, recording_path, summary_html, detailed_html,
                        sentinel_html, best_face, created_at, original_resume_text, archived_at,
                        authorship_scores_summary, interview_config, round_type, current_round_number
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                """, (
                    interview['id'],  # interview_id
                    interview['candidate_id'],
                    interview['job_id'],
                    interview['recruiter_id'],
                    interview['interview_time'],
                    interview['interview_timezone'],
                    interview['room_id'],
                    interview['recruiter_name'],
                    interview['recruiter_email'],
                    interview['interview_mode'],
                    interview['zone'],
                    interview['resume_text'],
                    interview['resume_match_score'],
                    interview['resume_job_analysis'],
                    interview['interview_score'],
                    interview['interview_analysis'],
                    interview['interview_verdict'],
                    interview['interview_status'],
                    interview['is_scheduled'],
                    interview['is_email_sent'],
                    interview['is_jd_interview_updated'],
                    interview['is_jd_interview_result_updates'],
                    interview['is_reminder_email_sent'],
                    interview.get('mandatory_skills'),
                    interview.get('optional_skills'),
                    interview.get('has_mandatory_skills'),
                    interview['recording_enabled'],
                    interview['sentinel_enabled'],
                    interview.get('recording_path'),
                    interview['summary_html'],
                    interview['detailed_html'],
                    interview['sentinel_html'],
                    interview['best_face'],
                    interview['interview_time'],
                    interview['original_resume_text'],
                    datetime.utcnow(),
                    json.dumps(interview.get('authorship_scores_summary')) if interview.get('authorship_scores_summary') else None,
                    json.dumps(interview.get('interview_config')) if interview.get('interview_config') else None,
                    interview.get('round_type', 'technical'),
                    interview.get('current_round_number', 1),
                ))
                history_id = cursor.lastrowid
            
            # Delete from interviews table
            cursor.execute(
                "DELETE FROM interviews WHERE room_id = %s",
                (room_id,)
            )
            deleted_count = cursor.rowcount
            
            # Commit transaction
            connection.commit()
            
            logger.info(
                "[process id: %s][room id: %s] ✅ Successfully archived interview (ID: %d) → history (ID: %d), deleted %d row(s)",
                process_id, room_id, interview['id'], history_id, deleted_count
            )
            
            return True, history_id
            
    except Exception as e:
        if connection:
            connection.rollback()
        logger.error(
            "[process id: %s][room id: %s] ❌ Error during archiving: %s",
            process_id, room_id, str(e)
        )
        logger.error(traceback.format_exc())
        return False, None
    finally:
        if connection:
            connection.close()


def get_interview_history_by_room_id(room_id, process_id=None):
    if not _IS_LOCAL:
        result = _api_call("GET", f"/interview/history/{room_id}", process_id=process_id, room_id=room_id)
        return result if result else None
    # ── LOCAL: direct DB ──
    """
    Retrieve archived interview from interview_history by room_id.
    
    Args:
        room_id: Room ID to search for
        process_id: Process ID for logging
    
    Returns:
        InterviewHistory object or None if not found
    """
    try:
        history = session.query(InterviewHistory).filter_by(room_id=room_id).first()
        
        if history:
            logger.info(
                "[process id: %s][room id: %s] Found interview history record (ID: %d)",
                process_id, room_id, history.id
            )
        else:
            logger.warning(
                "[process id: %s][room id: %s] No interview history found for room_id '%s'",
                process_id, room_id, room_id
            )
        
        return history
        
    except Exception as e:
        logger.error(
            "[process id: %s][room id: %s] Error retrieving interview history: %s",
            process_id, room_id, str(e)
        )
        return None


# Remove the old insert_or_update_interview_history and delete_from_interview_table functions
# They are replaced by archive_and_delete_interview above