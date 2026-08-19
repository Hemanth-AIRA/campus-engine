"""
Template Handler for Interview Reports
Handles HTML template rendering and file generation for interview reports
"""

import os
import base64
import logging
from datetime import datetime
from jinja2 import Environment, FileSystemLoader
from PIL import Image
import io
from logger_config import setup_logging
from config_loader import INTERVIEW_PASS_THRESHOLD_PERCENTAGE
import pymysql
import sentinel_report_data_processor
import requests
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

setup_logging()
logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

class InterviewReportTemplate:
    def __init__(self):
        # Set up Jinja2 environment
        current_dir = os.path.dirname(os.path.abspath(__file__))
        template_dir = os.path.join(current_dir, "templates")
        self.env = Environment(loader=FileSystemLoader(template_dir))

        # Add custom filter for color to CSS class conversion
        self.env.filters['color_to_class'] = self.color_to_class

        # Load templates - use detailed template for chronological Q&A display
        self.template = self.env.get_template('aira_detailed_report_template.html.j2')
        # Keep summary template for future use
        self.summary_template = self.env.get_template('aira_summary_report_template.html.j2')

    # === Color helpers (CSS class + hex) ===
    def color_to_class(self, hex_color: str) -> str:
        """Convert hex color to CSS class name for PDF-friendly styling."""
        if not hex_color:
            return 'color-blue'
        color_map = {
            '#4CAF50': 'color-green',
            '#2196F3': 'color-blue',
            '#FFC107': 'color-orange',
            '#9C27B0': 'color-purple',
            '#FF5722': 'color-red',
            '#04AAAA': 'color-grey',   # align spelling with CSS
            '#4caf50': 'color-green',
            '#2196f3': 'color-blue',
            '#ffc107': 'color-orange',
            '#9c27b0': 'color-purple',
            '#ff5722': 'color-red',
            '#04aaaa': 'color-grey',
        }
        return color_map.get(hex_color.lower(), 'color-blue')

    def get_metric_color_class(self, score: float) -> str:
        """Return class: color-green | color-orange | color-red based on score (0-5 scale)."""
        try:
            s = float(score)
        except Exception:
            s = 0.0
        if s >= 3.5:
            return 'color-green'
        if s >= 2:
            return 'color-orange'
        return 'color-red'

    def get_metric_color_hex(self, score: float) -> str:
        """Return hex color fallback (0-5 scale)."""
        try:
            s = float(score)
        except Exception:
            s = 0.0
        if s >= 3.5:
            return '#4CAF50'  # green
        if s >= 2:
            return '#FFC107'  # amber
        return '#FF5722'     # red

    def _extract_mandatory_skills(self, job_description):
        """Extract mandatory skills section from job description if it exists"""
        import re
        mandatory_skills = ""

        # Look for various patterns of mandatory skills sections
        patterns = [
            r"mandatory\s+skills?[:\-\s]*(.*?)(?=\n\s*\n|\n\s*[a-z]+\s*skills?[:\-]|\n\s*requirements?[:\-]|\Z)",
            r"required\s+skills?[:\-\s]*(.*?)(?=\n\s*\n|\n\s*[a-z]+\s*skills?[:\-]|\n\s*requirements?[:\-]|\Z)",
            r"must\s+have[:\-\s]*(.*?)(?=\n\s*\n|\n\s*[a-z]+\s*skills?[:\-]|\n\s*requirements?[:\-]|\Z)",
            r"essential\s+skills?[:\-\s]*(.*?)(?=\n\s*\n|\n\s*[a-z]+\s*skills?[:\-]|\n\s*requirements?[:\-]|\Z)"
        ]

        for pattern in patterns:
            match = re.search(pattern, job_description, re.IGNORECASE | re.DOTALL)
            if match:
                mandatory_skills = match.group(1).strip()
                break

        return mandatory_skills
    

    def evaluate_mandatory_skills(self, analysis_blocks, mandatory_skills_text, job_description, room_id, process_id, openai_client):
        """
        Extract mandatory skills from questions asked during the interview and evaluate 
        candidate's performance on each skill based on their responses.
        
        PASSING THRESHOLD: 2.5/5.0 (50%)
        """
        
        MANDATORY_SKILLS_EVALUATION_PROMPT = """You are an expert technical recruiter analyzing a candidate's interview performance.

    Your task is to:
    1. Identify ALL technical/professional skills that were ACTUALLY TESTED in the interview questions
    2. Evaluate the candidate's demonstrated proficiency in each skill based on their responses
    3. Provide evidence-based scoring with specific examples

    IMPORTANT: Only evaluate skills that were EXPLICITLY tested through interview questions. 
    Do not evaluate skills mentioned in the job description but not asked about in the interview.

    Given:
    - Complete interview transcript with questions, answers, and analysis

    Provide a JSON response with this EXACT structure:

    {
    "mandatory_skills": [
        {
        "skill_name": "specific skill tested (e.g., 'Python Programming', 'SQL Database Design', 'REST API Development')",
        "score": 0-5 (float, one decimal place),
        "proficiency_level": "None|Very Basic|Basic|Intermediate|Advanced|Expert",
        "evidence": "specific quotes or examples from candidate's answers demonstrating this skill",
        "assessment": "2-3 sentence detailed evaluation of proficiency based on responses",
        "demonstrated": true|false,
        "questions_asked": ["question 1 that tested this skill", "question 2 that tested this skill"]
        }
    ],
    "overall_mandatory_score": 0-5 (float - weighted average of all skills tested),
    "skills_met": number of skills where score >= 2.5,
    "total_skills": total number of skills tested in interview,
    "skills_gap_summary": "2-3 sentence summary identifying weak areas among tested skills"
    }

    SCORING GUIDELINES (0-5 SCALE):

    0.0: NONE
    - Absolutely no knowledge demonstrated
    - Admits complete lack of experience ("I have never used this")
    - Did not attempt to answer or provided completely irrelevant response
    - No awareness of the skill whatsoever

    0.1 - 1.0: VERY BASIC
    - Minimal awareness or very surface-level familiarity
    - Fundamental misunderstanding of core concepts
    - Cannot answer basic questions about the skill
    - May have heard of the technology but has no practical understanding
    - Provides mostly incorrect or vague information

    1.1 - 2.4: BASIC/INSUFFICIENT  
    - Limited theoretical knowledge only
    - Cannot provide practical examples
    - Significant gaps in understanding
    - Struggles with basic concepts
    - DOES NOT MEET MINIMUM REQUIREMENTS

    2.5 - 3.4: INTERMEDIATE/ACCEPTABLE ⭐ PASSING THRESHOLD
    - Basic practical knowledge demonstrated
    - Can discuss concepts with some depth
    - Shows foundational hands-on experience
    - Provides concrete examples
    - MEETS MINIMUM REQUIREMENTS

    3.5 - 4.4: ADVANCED/PROFICIENT
    - Strong practical knowledge with specific examples
    - Demonstrates solid hands-on experience
    - Can discuss advanced concepts
    - Shows problem-solving ability

    4.5 - 5.0: EXPERT/EXCEPTIONAL
    - Deep expertise clearly demonstrated
    - Multiple detailed real-world examples
    - Can explain complex scenarios
    - Shows mastery and best practices

    CRITICAL RULES:
    1. ONLY evaluate skills that were directly tested in interview questions
    2. Base scores ONLY on actual evidence from the interview responses
    3. Provide SPECIFIC evidence - quote actual responses
    4. Skills with score >= 2.5 are considered PASSED
    5. If candidate provides no concrete examples for a tested skill, maximum score = 2.0
    6. If candidate admits complete lack of experience in a tested skill, score = 0.0 (None)
    6b. If candidate shows minimal/surface-level awareness but no real understanding, maximum score = 1.0 (Very Basic)
    7. Group related questions under the same skill (e.g., multiple SQL questions = one "SQL" skill)
    8. Overall score reflects performance on skills that were actually tested
    9. Set "demonstrated": true only if score >= 2.5

    EXAMPLES OF SKILL IDENTIFICATION:
    - Question: "Explain how you would optimize a slow SQL query" → Skill: "SQL Query Optimization"
    - Question: "Describe your experience with React hooks" → Skill: "React.js Development"
    - Question: "How do you handle API rate limiting?" → Skill: "API Design & Best Practices"

    Return ONLY valid JSON, no additional text or markdown formatting."""

        try:
            logger.info(f"[process id: {process_id}][room id: {room_id}] Starting mandatory skills evaluation from interview questions")
            
            # Guard: Check if there are any valid questions to evaluate
            valid_questions = [
                qa for qa in analysis_blocks 
                if isinstance(qa, dict) 
                and qa.get('question') 
                and qa.get('answer') 
                and str(qa.get('answer', '')).strip() not in ['', '.', 'No answer provided', 'N/A']
            ]
            
            if not valid_questions:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] No valid Q&A pairs found - skipping mandatory skills evaluation")
                return {
                    "mandatory_skills": [],
                    "overall_mandatory_score": 0,
                    "skills_met": 0,
                    "total_skills": 0,
                    "skills_gap_summary": "No interview questions were answered. Unable to evaluate skills."
                }
            
            logger.info(f"[process id: {process_id}][room id: {room_id}] Found {len(valid_questions)} valid Q&A pairs for skills evaluation")
            
            # Build comprehensive interview context
            interview_context = ""
            for i, qa in enumerate(valid_questions, 1):
                if isinstance(qa, dict):
                    interview_context += f"\n{'='*60}\n"
                    interview_context += f"QUESTION {i}: {qa.get('question', 'N/A')}\n"
                    interview_context += f"CANDIDATE ANSWER: {qa.get('answer', 'N/A')}\n"
                    interview_context += f"SCORE: {qa.get('overall_score', 0)}/100\n"
                    interview_context += f"ANALYSIS: {qa.get('analysis', 'N/A')}\n"
                    
                    if qa.get('strengths'):
                        interview_context += f"STRENGTHS: {', '.join(qa['strengths'])}\n"
                    if qa.get('weaknesses'):
                        interview_context += f"WEAKNESSES: {', '.join(qa['weaknesses'])}\n"
            
            # Prepare the evaluation request
            user_prompt = f"""COMPLETE INTERVIEW TRANSCRIPT AND ANALYSIS:
    {interview_context}

    TASK: 
    1. Identify all technical/professional skills that were tested through the questions asked
    2. Evaluate the candidate's proficiency in each skill based on their responses
    3. Provide specific evidence from their answers
    4. Mark skill as "demonstrated": true if score >= 2.5 (passing threshold)

    Focus ONLY on skills that were actually tested in the interview questions above."""

            # Call OpenAI API
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": MANDATORY_SKILLS_EVALUATION_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
                response_format={"type": "json_object"}
            )
            
            raw_content = response.choices[0].message.content
            logger.info(f"[process id: {process_id}][room id: {room_id}] Received mandatory skills evaluation response")
            
            try:
                # Parse JSON response
                import json
                skills_evaluation = json.loads(raw_content)
                
                # Validate and ensure all required fields exist
                if "mandatory_skills" not in skills_evaluation:
                    skills_evaluation["mandatory_skills"] = []
                
                # PASSING_THRESHOLD constant
                PASSING_THRESHOLD = 2.5
                
                # Ensure each skill has all required fields with CONSISTENT 2.5 threshold
                for skill in skills_evaluation["mandatory_skills"]:
                    skill.setdefault("skill_name", "Unknown Skill")
                    skill.setdefault("score", 0)
                    skill.setdefault("proficiency_level", "None")
                    skill.setdefault("evidence", "No evidence found in interview")
                    skill.setdefault("assessment", "Skill not adequately assessed during interview")
                    skill.setdefault("questions_asked", [])
                    
                    # Normalize score to 0-5 scale
                    raw_skill_score = float(skill.get("score", 0))
                    if raw_skill_score > 5:
                        raw_skill_score = raw_skill_score / 20.0  # Convert 0-100 to 0-5
                    skill["score"] = round(max(0.0, min(5.0, raw_skill_score)), 1)
                    
                    # ⭐ FIX: Enforce proficiency-to-score alignment
                    # LLM sometimes returns "Intermediate" with score 0.4 - this is inconsistent
                    # Intermediate should be 2.5-3.4 per scoring guidelines
                    PROFICIENCY_FLOOR = {
                        'None': 0.0,
                        'Very Basic': 0.1,
                        'Basic': 1.1,
                        'Intermediate': 2.5,
                        'Advanced': 3.5,
                        'Expert': 4.5
                    }
                    proficiency = skill.get("proficiency_level", "None")
                    min_score = PROFICIENCY_FLOOR.get(proficiency, 0.0)
                    
                    if skill["score"] < min_score:
                        logger.warning(
                            f"[process id: {process_id}][room id: {room_id}] "
                            f"Score correction: {skill['skill_name']} scored {skill['score']} "
                            f"with {proficiency} proficiency (floor: {min_score}). Correcting to {min_score}."
                        )
                        skill["score"] = min_score
                    
                    # ⭐ CRITICAL FIX: Use consistent 2.5 threshold for demonstrated flag
                    skill["demonstrated"] = skill["score"] >= PASSING_THRESHOLD
                    
                    logger.debug(
                        f"[process id: {process_id}][room id: {room_id}] "
                        f"Skill: {skill['skill_name']}, Score: {skill['score']}, "
                        f"Demonstrated: {skill['demonstrated']} (threshold: {PASSING_THRESHOLD})"
                    )
                
                # Calculate overall metrics if missing - USE 2.5 THRESHOLD
                if "total_skills" not in skills_evaluation:
                    skills_evaluation["total_skills"] = len(skills_evaluation["mandatory_skills"])
                
                # ⭐ CRITICAL FIX: Use 2.5 for skills_met count
                if "skills_met" not in skills_evaluation:
                    skills_evaluation["skills_met"] = sum(
                        1 for s in skills_evaluation["mandatory_skills"] 
                        if s.get("score", 0) >= PASSING_THRESHOLD
                    )
                else:
                    # Recalculate to ensure consistency even if AI provided it
                    skills_evaluation["skills_met"] = sum(
                        1 for s in skills_evaluation["mandatory_skills"] 
                        if s.get("score", 0) >= PASSING_THRESHOLD
                    )
                
                if "overall_mandatory_score" not in skills_evaluation:
                    if skills_evaluation["mandatory_skills"]:
                        avg_score = sum(s.get("score", 0) for s in skills_evaluation["mandatory_skills"]) / len(skills_evaluation["mandatory_skills"])
                        skills_evaluation["overall_mandatory_score"] = round(avg_score, 1)
                    else:
                        skills_evaluation["overall_mandatory_score"] = 0
                else:
                    # Normalize if it came back on 0-100 scale
                    raw_overall = float(skills_evaluation.get("overall_mandatory_score", 0))
                    if raw_overall > 5:
                        skills_evaluation["overall_mandatory_score"] = round(raw_overall / 20.0, 1)
                
                if "skills_gap_summary" not in skills_evaluation:
                    unmet_skills = [
                        s["skill_name"] for s in skills_evaluation["mandatory_skills"] 
                        if s.get("score", 0) < PASSING_THRESHOLD
                    ]
                    if unmet_skills:
                        skills_evaluation["skills_gap_summary"] = (
                            f"Skills below passing threshold (2.5): {', '.join(unmet_skills)}. "
                            f"These areas need improvement."
                        )
                    else:
                        skills_evaluation["skills_gap_summary"] = (
                            "Candidate demonstrated proficiency in all tested skills "
                            "at or above the passing threshold (2.5/5.0)."
                        )
                
                logger.info(
                    f"[process id: {process_id}][room id: {room_id}] "
                    f"Mandatory skills evaluation complete: {skills_evaluation['skills_met']}/{skills_evaluation['total_skills']} skills passed (>= 2.5), "
                    f"overall score: {skills_evaluation['overall_mandatory_score']}/5"
                )
                
                # Log each skill for debugging
                for skill in skills_evaluation["mandatory_skills"]:
                    status = "✓ PASS" if skill["demonstrated"] else "✗ FAIL"
                    logger.info(
                        f"[process id: {process_id}][room id: {room_id}] "
                        f"{status} | {skill['skill_name']}: {skill['score']}/5.0"
                    )
                
                return skills_evaluation
                
            except json.JSONDecodeError as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] JSON decode error in mandatory skills evaluation: {e}")
                raise
                
        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] Error evaluating mandatory skills: {e}")
            
            # Return fallback structure
            return {
                "mandatory_skills": [],
                "overall_mandatory_score": 0,
                "skills_met": 0,
                "total_skills": 0,
                "skills_gap_summary": "Technical error prevented mandatory skills evaluation. Manual review required."
            }
        
    def generate_template_data(self, interview, json_final_analysis, analysis_blocks, process_id, room_id):
        """
        Generate template data from interview results
        """
        logger.info(f"[process id: {process_id}][room id: {room_id}] Generating template data for interview report")
        interview_id = interview.get('id', 'unknown')

        # ── Extract pass threshold from DB interview_config (fallback to static config) ──
        import json as _json
        _raw_ic = interview.get("interview_config")
        if _raw_ic:
            _ic = _json.loads(_raw_ic) if isinstance(_raw_ic, str) else _raw_ic
        else:
            _ic = {}
        db_pass_threshold = int(_ic.get("pass_threshold_pct", INTERVIEW_PASS_THRESHOLD_PERCENTAGE))
        logger.info(
            f"[process id: {process_id}][room id: {room_id}] "
            f"📋 Pass threshold from interview_config: {db_pass_threshold}% "
            f"(fallback={INTERVIEW_PASS_THRESHOLD_PERCENTAGE}%)"
        )

        # Initialize OpenAI client at the start
        openai_client = None
        try:
            from openai import OpenAI
            import os
            
            api_key = os.getenv('OPENAI_API_KEY')
            if api_key:
                openai_client = OpenAI(api_key=api_key)
                logger.info(f"[process id: {process_id}][room id: {room_id}] OpenAI client initialized successfully")
            else:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] OPENAI_API_KEY not found - mandatory skills evaluation will be skipped")
        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] Failed to initialize OpenAI client: {e}")
            openai_client = None

        # NEW: Handle both old format (list) and new format (dict with timeline)
        if isinstance(analysis_blocks, dict):
            analyzed_questions = analysis_blocks.get("analyzed_questions", [])
            conversation_timeline = analysis_blocks.get("conversation_timeline", [])
            logger.info(f"[process id: {process_id}][room id: {room_id}] conversational timeline for debugging {conversation_timeline}")
            if conversation_timeline:
                conversation_timeline = self._format_timeline_timestamps(conversation_timeline)
            logger.info(f"[process id: {process_id}][room id: {room_id}] Processing new format: {len(analyzed_questions)} analyzed, {len(conversation_timeline)} timeline entries")
        else:
            analyzed_questions = analysis_blocks if isinstance(analysis_blocks, list) else []
            conversation_timeline = []
            logger.info(f"[process id: {process_id}][room id: {room_id}] Processing old format: {len(analyzed_questions)} analyzed blocks")

        # --- Candidate photo via API ---
        try:
            import integrations.aira_sql as airadb
            logger.info(f"[process id: {process_id}][room id: {room_id}] 🔎 INSPECTING BYTEA DATA")
            self.inspect_bytea(interview_id, process_id, room_id)
            logger.info(f"[process id: {process_id}][room id: {room_id}] 💾 ATTEMPTING TO SAVE IMAGE")
            saved_path = self.save_image_to_png(interview_id, process_id=process_id, room_id=room_id)
            if saved_path:
                logger.info(f"[process id: {process_id}][room id: {room_id}] Successfully saved candidate photo via API: {saved_path}")
            else:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] Could not save image for interview {interview_id}")
        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] Error saving candidate photo: {e}")

        # Load the candidate photo (either newly saved or previously existing)
        candidate_photo_base64 = self._get_candidate_photo(interview_id, process_id, room_id)

        # Mandatory skills extraction (fallback)
        has_mandatory_skills = interview.get('has_mandatory_skills', False)
        mandatory_skills = interview.get('mandatory_skills', '')
        if not has_mandatory_skills and not mandatory_skills:
            job_description = interview.get('job_description', '')
            mandatory_skills = self._extract_mandatory_skills(job_description)
            has_mandatory_skills = bool(mandatory_skills.strip())
        
        # Evaluate mandatory skills ONLY if OpenAI client is available
        mandatory_skills_evaluation = None
        if openai_client and (has_mandatory_skills or mandatory_skills):
            try:
                logger.info(f"[process id: {process_id}][room id: {room_id}] Starting mandatory skills evaluation")
                mandatory_skills_evaluation = self.evaluate_mandatory_skills(
                    analysis_blocks=analyzed_questions,
                    mandatory_skills_text=mandatory_skills,
                    job_description=interview.get('job_description', ''),
                    room_id=room_id,
                    process_id=process_id,
                    openai_client=openai_client
                )
                logger.info("---"*1000)
                logger.info(f"[process id: {process_id}][room id: {room_id}] Mandatory skills evaluation completed successfully: {mandatory_skills_evaluation}")
                logger.info("---"*1000)
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Error during mandatory skills evaluation: {e}", exc_info=True)
                mandatory_skills_evaluation = None
        else:
            if not openai_client:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] Skipping mandatory skills evaluation - OpenAI client not available")
            elif not (has_mandatory_skills or mandatory_skills):
                logger.info(f"[process id: {process_id}][room id: {room_id}] Skipping mandatory skills evaluation - No mandatory skills defined")
        
        # NOTE: final_score and verdict will be calculated AFTER Skills metric override
        # This ensures the exclusive skill score is included in the final calculation
        # See the section after "ALIGN Skills metric with Mandatory Skills evaluation score"

        # Aggregate strengths / weaknesses
        strengths, areas_for_development = [], []
        for block in analyzed_questions:
            if isinstance(block, dict):
                if block.get('strengths'):
                    strengths.extend(block['strengths'])
                if block.get('weaknesses'):
                    areas_for_development.extend(block['weaknesses'])
        strengths = list(dict.fromkeys(strengths))
        areas_for_development = list(dict.fromkeys(areas_for_development))

        # NOTE: Comprehensive improvements call moved AFTER final_score calculation (around line 878)
        # because _generate_comprehensive_improvements needs final_score as input

        # =====================================================
        # AUTHORSHIP/AI DETECTION - Load per-question metrics
        # =====================================================
        authorship_summary = None
        question_authorship = {}  # Map Q1, Q2, etc. to authorship data
        try:
            import integrations.aira_sql
            authorship_summary = aira_sql.generate_authorship_summary(interview_id, process_id, room_id)
            if authorship_summary and not authorship_summary.get("error"):
                question_authorship = authorship_summary.get("question_flags", {})
                logger.info(
                    f"[process id: {process_id}][room id: {room_id}] "
                    f"Loaded authorship summary: score={authorship_summary.get('authenticity_score', 'N/A')}%, "
                    f"band={authorship_summary.get('confidence_band', 'N/A')}"
                )
            else:
                logger.warning(
                    f"[process id: {process_id}][room id: {room_id}] "
                    f"No authorship data available: {authorship_summary.get('error', 'unknown')}"
                )
        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] Error loading authorship summary: {e}")

        # Build questions list with robust metrics + color mapping
        questions_list = []
        for i, block in enumerate(analyzed_questions, 1):
            if isinstance(block, dict):
                answer = block.get('answer', 'No answer provided')
                is_flagged = block.get('flag', False)

                # Heuristic auto-flag for non-analyzed convo items
                if not is_flagged and answer:
                    a_low = answer.lower()
                    if a_low.startswith("bot note:") or "what do you think" in a_low or "can you give" in a_low or "i want to create" in a_low:
                        is_flagged = True

                raw_overall_score = float(block.get('overall_score', 0) or 0)
                # Normalize overall_score to 0-5 scale
                if raw_overall_score > 5:
                    original_overall_score = raw_overall_score / 20.0
                else:
                    original_overall_score = raw_overall_score
                original_overall_score = min(5.0, max(0.0, original_overall_score))

                metrics = block.get('metrics', []) or []

                # Normalize existing metrics to 0-5 scale
                for m in metrics:
                    m_score = float(m.get('score', 0) or 0)
                    if m_score > 5:
                        m['score'] = round(m_score / 20.0, 2)  # Convert 0-100 to 0-5
                    m['score'] = min(5.0, max(0.0, float(m['score'])))

                # If no metrics, synthesize on 0-5 scale
                if not metrics:
                    base_score = original_overall_score  # Already on 0-5 scale
                    if has_mandatory_skills:
                        metrics = [
                            {"name": "Communication",          "score": round(base_score, 2)},
                            {"name": "Experience",             "score": round(max(0, base_score - 0.5), 2)},
                            {"name": "Mandatory Skills",       "score": round(max(0, base_score - 1.0), 2)},
                            {"name": "Domain Knowledge",       "score": round(max(0, base_score - 0.25), 2)},
                            {"name": "Technical Competency",   "score": round(max(0, base_score - 0.75), 2)},
                            {"name": "Problem Solving",        "score": round(max(0, base_score - 0.4), 2)},
                        ]
                    else:
                        metrics = [
                            {"name": "Communication",          "score": round(base_score, 2)},
                            {"name": "Experience",             "score": round(max(0, base_score - 0.5), 2)},
                            {"name": "Confidence",             "score": round(min(5, base_score + 0.5), 2)},
                            {"name": "Domain Knowledge",       "score": round(max(0, base_score - 0.25), 2)},
                            {"name": "CV Justification",   "score": round(max(0, base_score - 0.75), 2)},
                            {"name": "Skills",                 "score": round(base_score, 2)},
                        ]
                # Normalize metric colors (add class + hex; keep 'color' for legacy filter path)
                for m in metrics:
                    s_val = m.get('score', 0)
                    m['color_class'] = self.get_metric_color_class(s_val)
                    m['color_hex'] = self.get_metric_color_hex(s_val)
                    # legacy: if template still uses color_to_class on m['color']
                    m['color'] = m.get('color', m['color_hex'])

                # Corrected overall score logic (all on 0-5 scale now)
                if metrics:
                    metric_scores = [float(m.get('score', 0) or 0) for m in metrics if isinstance(m, dict)]
                    if metric_scores:
                        avg_metric_score = sum(metric_scores) / len(metric_scores)
                        corrected_overall_score = min(original_overall_score, avg_metric_score)
                    else:
                        corrected_overall_score = original_overall_score
                else:
                    corrected_overall_score = original_overall_score
                corrected_overall_score = min(5.0, max(0.0, corrected_overall_score))

                question_data = {
                    "id": block.get('id', i),
                    "question": block.get('question', f'Question {i}'),
                    "answer": answer,
                    "analysis": block.get('analysis', ''),
                    "overall_score": round(min(5.0, max(0.0, corrected_overall_score)), 2),
                    "metrics": metrics,
                    "strengths": block.get('strengths', []),
                    "weaknesses": block.get('weaknesses', []),
                    "flag": is_flagged
                }
                
                # =====================================================
                # ADD AUTHENTICITY AS A METRIC (like Communication, etc.)
                # =====================================================
                q_key = f"Q{i}"
                q_auth = question_authorship.get(q_key, {})
                
                if q_auth and q_auth.get("authenticity_score") is not None:
                    # Skip Q2 (background question "To begin...") - always 0% due to no baseline
                    current_question_text = block.get('question', '').lower()
                    is_background_question = (i == 2) or (
                        "background" in current_question_text and 
                        "professional" in current_question_text
                    )
                    
                    if not is_background_question:
                        # Convert 0-100% to 0-5 scale for consistency with other metrics
                        raw_auth_score = float(q_auth.get("authenticity_score", 50))
                        
                        # Skip displaying metric if score is 0 (no useful data)
                        if raw_auth_score > 0:
                            auth_score_0_5 = round((raw_auth_score / 100.0) * 5.0, 2)
                            auth_score_0_5 = min(5.0, max(0.0, auth_score_0_5))
                            
                            # Add Authenticity as a metric in the metrics array
                            auth_metric = {
                                "name": "Authenticity",
                                "score": auth_score_0_5
                            }
                            auth_metric['color_class'] = self.get_metric_color_class(auth_score_0_5)
                            auth_metric['color_hex'] = self.get_metric_color_hex(auth_score_0_5)
                            auth_metric['color'] = auth_metric['color_hex']
                            
                            # Insert Authenticity metric at the beginning
                            question_data["metrics"].insert(0, auth_metric)
                    
                    # Store flag for potential future use (data still stored in DB)
                    auth_flag = q_auth.get("flag", "unknown")
                    question_data["authenticity_flag"] = auth_flag
                    # Note: Do NOT set question_data["flag"] = True here
                    # The "flag" property is used by the template to HIDE questions
            else:
                # Fallback for string-based analysis blocks
                if has_mandatory_skills:
                    fallback_metrics = [
                        {"name": "Communication", "score": 0},
                        {"name": "Experience", "score": 0},
                        {"name": "Mandatory Skills", "score": 0},
                        {"name": "Domain Knowledge", "score": 0},
                        {"name": "Technical Competency", "score": 0},
                        {"name": "Problem Solving", "score": 0},
                    ]
                else:
                    fallback_metrics = [
                        {"name": "Communication", "score": 0},
                        {"name": "Experience", "score": 0},
                        {"name": "Confidence", "score": 0},
                        {"name": "Domain Knowledge", "score": 0},
                        {"name": "CV Justification", "score": 0},
                        {"name": "Skills", "score": 0},
                    ]
                for m in fallback_metrics:
                    m['color_class'] = self.get_metric_color_class(0)
                    m['color_hex'] = self.get_metric_color_hex(0)
                    m['color'] = m['color_hex']

                question_data = {
                    "id": i,
                    "question": "Question details not available",
                    "answer": "Answer details not available",
                    "analysis": str(block) if block else "No analysis available",
                    "overall_score": 0,
                    "metrics": fallback_metrics,
                    "strengths": [],
                    "weaknesses": [],
                    "flag": False
                }
            questions_list.append(question_data)

        # Calculate average metrics across questions (with colors)
        all_metrics, question_count = {}, 0
        for q in questions_list:
            if q.get('metrics'):
                question_count += 1
                for m in q['metrics']:
                    name = m['name']
                    # Skip Authenticity - it should NOT affect final score
                    if name == "Authenticity":
                        continue
                    score_val = float(m.get('score', 0))
                    agg = all_metrics.get(name, 0.0)
                    all_metrics[name] = agg + score_val

        avg_metrics = []
        if question_count > 0:
            for name, total in all_metrics.items():
                avg_score = round(total / question_count, 1)
                avg_metrics.append({
                    "name": name,
                    "score": avg_score,
                    "color_hex": self.get_metric_color_hex(avg_score),
                    "color_class": self.get_metric_color_class(avg_score),
                    "color": self.get_metric_color_hex(avg_score),  # legacy path
                })

        # ALIGN Skills metric with Mandatory Skills evaluation score
        if mandatory_skills_evaluation and mandatory_skills_evaluation.get("overall_mandatory_score") is not None:
            mandatory_overall_score = float(mandatory_skills_evaluation.get("overall_mandatory_score", 0))
            # Ensure it's on 0-5 scale
            if mandatory_overall_score > 5:
                mandatory_overall_score = mandatory_overall_score / 20.0
            mandatory_overall_score = round(min(5.0, max(0.0, mandatory_overall_score)), 1)
            
            # Find and update the "Skills" metric to match mandatory skills score
            for metric in avg_metrics:
                if metric["name"] == "Skills":
                    logger.info(
                        f"[process id: {process_id}][room id: {room_id}] "
                        f"Aligning Skills metric: {metric['score']} -> {mandatory_overall_score} (from mandatory skills evaluation)"
                    )
                    metric["score"] = mandatory_overall_score
                    metric["color_hex"] = self.get_metric_color_hex(mandatory_overall_score)
                    metric["color_class"] = self.get_metric_color_class(mandatory_overall_score)
                    metric["color"] = metric["color_hex"]
                    break
            else:
                # If "Skills" metric doesn't exist, add it
                logger.info(
                    f"[process id: {process_id}][room id: {room_id}] "
                    f"Adding Skills metric with mandatory skills score: {mandatory_overall_score}"
                )
                avg_metrics.append({
                    "name": "Skills",
                    "score": mandatory_overall_score,
                    "color_hex": self.get_metric_color_hex(mandatory_overall_score),
                    "color_class": self.get_metric_color_class(mandatory_overall_score),
                    "color": self.get_metric_color_hex(mandatory_overall_score),
                })

        # =====================================================
        # CALCULATE FINAL SCORE AND VERDICT (after Skills override)
        # =====================================================
        # Initialize final_score with default
        final_score = 0.0
        
        # Calculate final_score from avg_metrics (excluding Authenticity)
        non_auth_metrics = [m for m in avg_metrics if m["name"] != "Authenticity"]
        if non_auth_metrics:
            final_score = sum(m["score"] for m in non_auth_metrics) / len(non_auth_metrics)
            final_score = round(min(5.0, max(0.0, final_score)), 1)
            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"Final score calculated from {len(non_auth_metrics)} metrics (excl. Authenticity): {final_score}/5"
            )
        else:
            # Fallback to GPT score if no metrics available
            raw_final_score = float(json_final_analysis.get('final_score', 0) or 0)
            if raw_final_score > 5:
                final_score = raw_final_score / 20.0
            else:
                final_score = raw_final_score
            final_score = round(min(5.0, max(0.0, final_score)), 1)
            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"Final score from GPT fallback: {final_score}/5"
            )
        
        # Calculate verdict (threshold on 0-5 scale, sourced from DB interview_config)
        pass_threshold_on_5_scale = (db_pass_threshold / 100.0) * 5.0
        total_questions = self._get_total_questions(interview)
        min_questions_required = max(int(total_questions * 0.75), 3)  # 75% of total, min 3
        enough_questions = len(analyzed_questions) >= min_questions_required
        passed = bool(final_score >= pass_threshold_on_5_scale and enough_questions)
        logger.info(
            f"[process id: {process_id}][room id: {room_id}] "
            f"Verdict check: score={final_score}/{pass_threshold_on_5_scale} (threshold={db_pass_threshold}%) "
            f"questions={len(analyzed_questions)}/{min_questions_required} (75% of {total_questions}) "
            f"passed={passed}"
        )
        
        # Enhanced pass criteria with mandatory skills if evaluation is available
        if mandatory_skills_evaluation:
            mandatory_threshold = 0.4  # 40% of mandatory skills must be met
            total_skills = mandatory_skills_evaluation.get("total_skills", 0)
            skills_met = mandatory_skills_evaluation.get("skills_met", 0)
            
            if total_skills > 0:
                required_skills = int(total_skills * mandatory_threshold)  # Floor to nearest integer
                mandatory_skills_passed = skills_met >= required_skills
                passed = passed and mandatory_skills_passed
                logger.info(
                    f"[process id: {process_id}][room id: {room_id}] "
                    f"Mandatory skills check: {skills_met}/{total_skills} met (need {required_skills}), "
                    f"passed={mandatory_skills_passed}"
                )
        
        verdict_text = "PASSED INTERVIEW ROUND" if passed else "FAILED INTERVIEW ROUND"
        logger.info(f"[process id: {process_id}][room id: {room_id}] Verdict: {verdict_text}")
        
        # Save the calculated final_score and verdict to database
        interview_id = interview.get('id', interview.get('interview_id'))
        if interview_id:
            try:
                import integrations.aira_sql as airadb
                airadb.update_interview(interview_id, {
                    "interview_score": round(final_score * 20, 1),  # Convert back to 0-100 for DB
                    "interview_verdict": "Pass" if passed else "Fail"
                }, process_id=process_id, room_id=room_id)
                logger.info(
                    f"[process id: {process_id}][room id: {room_id}] "
                    f"✅ Updated DB with calculated score={round(final_score * 20, 1)}/100, verdict={'Pass' if passed else 'Fail'}"
                )
            except Exception as e:
                logger.error(f"[process id: {process_id}][room id: {room_id}] ❌ Failed to update verdict in DB: {e}")

        # Comprehensive improvements (moved here after final_score is calculated)
        comprehensive_improvements = self._generate_comprehensive_improvements(
            analyzed_questions, final_score, areas_for_development
        )
        all_improvements = comprehensive_improvements + areas_for_development
        areas_for_development = list(dict.fromkeys(all_improvements))[:5]

        # Prepare template data matching expected JSON structure
        q_count = (
            len(analysis_blocks.get("analyzed_questions", []))
            if isinstance(analysis_blocks, dict)
            else len(analysis_blocks or [])
        )

        raw_resume_score = float(interview.get('resume_match_score', 0) or 0)
        if raw_resume_score > 5:
            resume_score = raw_resume_score / 20.0
        else:
            resume_score = raw_resume_score
        resume_score = min(5.0, max(0.0, resume_score))

        template_data = {
            "header": {
                "title": "AIRA",
                "subtitle": "Your AI Hiring Agent<br>Autonomous Interview & Resume Analysis"
            },
            "candidate": {
                "name": interview.get('candidate_name', 'Unknown'),
                "email": interview.get('candidate_email', ''),
                "phone": interview.get('candidate_phone', 'None'),
                "photo_url": candidate_photo_base64
            },
            "client": {
                "company": interview.get('company_name', 'Unknown Company'),
                "position": interview.get('job_title', 'Unknown Position')
            },
            "interview": {
                "date": interview.get('interview_time', datetime.now()).strftime("%Y-%m-%d") if interview.get('interview_time') else datetime.now().strftime("%Y-%m-%d"),
                "time": interview.get('interview_time', datetime.now()).strftime("%I:%M %p") if interview.get('interview_time') else datetime.now().strftime("%I:%M %p"),
                "timezone": interview.get('interview_timezone', 'UTC'),
                "duration": interview.get('actual_duration', '25 mins'),
                "questions_count": q_count,
                "total_questions": self._get_total_questions(interview),
                "mode": interview.get("interview_mode"),
                "room_id": interview.get('room_id', '')
            },
            "resume_analysis": {
                "score": round(resume_score, 1),
                "recommendation": interview.get('resume_job_analysis', 'Resume analysis not available.')
            },
            "interview_analysis": {
                "score": round(final_score, 1),
                "recommendation": json_final_analysis.get("final_analysis", "Interview analysis not available.")
            },
            "verdict": {
                "passed": passed,
                "icon": "✓" if passed else "✗",
                "status": verdict_text,
                "subtitle": "Recommend proceeding to the next stage of the interview process." if passed else "Candidate requires further evaluation or training before proceeding.",
                "strengths": {
                    "title": "✓ Key Strengths",
                    "items": strengths[:5] if strengths else [item for item in ["Technical Knowledge", "Experience", "Domain Knowledge"] if item not in areas_for_development][:2]
                },
                "areas": {
                    "title": "⚠ Areas for Development",
                    "items": areas_for_development[:3] if areas_for_development else [item for item in ["Communication", "Confidence", "Experience"] if item not in strengths][:2]
                },
                "overall_assessment": (
                    f"The candidate achieved a score of {final_score:.1f}/5 ({verdict_text.lower()}). "
                    f"{json_final_analysis.get('final_analysis', 'Analysis not available.')} "
                    f"Based on the comprehensive evaluation across {len(questions_list)} interview questions, "
                    f"{'the candidate demonstrates strong potential for the role' if passed else 'further development is recommended'}."
                )
            },
            "overview": {
                "summary": json_final_analysis.get("performance_overview", "Performance overview not available."),
                "metrics": avg_metrics
            },
            "mandatory": {
                "strengths": strengths[:3] if strengths else ["Technical Skills"],
                "improvements": areas_for_development[:3] if areas_for_development else ["Experience"],
                "has_mandatory_skills": has_mandatory_skills,
                "mandatory_skills": mandatory_skills if has_mandatory_skills else None,
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
            "aira_logo_url": self._get_aira_logo()
        }
        
        # Add mandatory skills evaluation to template data if available
        if mandatory_skills_evaluation:
            template_data["mandatory_skills_evaluation"] = mandatory_skills_evaluation
            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"Added mandatory skills evaluation to template: "
                f"{mandatory_skills_evaluation['skills_met']}/{mandatory_skills_evaluation['total_skills']} skills met"
            )
        
        # =====================================================
        # ADD AUTHORSHIP/AI DETECTION SUMMARY TO TEMPLATE
        # =====================================================
        if authorship_summary and not authorship_summary.get("error"):
            template_data["authorship_analysis"] = {
                "authenticity_score": authorship_summary.get("authenticity_score", None),
                "confidence_band": authorship_summary.get("confidence_band", "N/A"),
                "interpretation": authorship_summary.get("interpretation", ""),
                "mute_streak_detected": authorship_summary.get("mute_streak_detected", False),
                "mute_event_count": len(authorship_summary.get("breakdown", {}).get("mute_questions", [])),
                "suspicious_count": authorship_summary.get("suspicious_count", 0),
                "analyzed_questions": authorship_summary.get("analyzed_questions", 0),
                "flags": authorship_summary.get("flags", []),
                "breakdown": authorship_summary.get("breakdown", {})
            }
            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"Added authorship analysis to template: "
                f"score={authorship_summary.get('authenticity_score', 'N/A')}%, "
                f"band={authorship_summary.get('confidence_band', 'N/A')}, "
                f"suspicious={authorship_summary.get('suspicious_count', 0)}"
            )

        logger.info(f"[process id: {process_id}][room id: {room_id}] Successfully generated template data")
        return template_data

    def extract_report_data_for_storage(self, template_data, json_final_analysis, analysis_blocks, process_id=0, room_id=""):
        """
        Extract all report-generated data from template_data into a flat dict
        ready for DB insertion into interview_report_data table.
        
        Only includes data that is GENERATED during report creation —
        no candidate/job/recruiter info (those are FK-joined).
        
        Args:
            template_data: Full template data dict from generate_template_data()
            json_final_analysis: Raw GPT final analysis dict
            analysis_blocks: Raw analysis blocks (before template processing)
            process_id: Process ID for logging
            room_id: Room ID for logging
        
        Returns:
            Dict ready for save_interview_report_data()
        """
        try:
            logger.info(f"[process id: {process_id}][room id: {room_id}] 📊 extract_report_data_for_storage() called — extracting structured data from template...")
            # Strip color fields from metrics to keep JSON clean for ETL
            def clean_metrics(metrics_list):
                if not metrics_list:
                    return []
                cleaned = []
                for m in metrics_list:
                    cleaned.append({
                        "name": m.get("name"),
                        "score": m.get("score", 0),
                    })
                return cleaned

            # Strip color fields from per-question metrics too
            def clean_questions(questions_list):
                if not questions_list:
                    return []
                cleaned = []
                for q in questions_list:
                    clean_q = {
                        "id": q.get("id"),
                        "question": q.get("question"),
                        "answer": q.get("answer"),
                        "analysis": q.get("analysis"),
                        "overall_score": q.get("overall_score"),
                        "metrics": clean_metrics(q.get("metrics", [])),
                        "strengths": q.get("strengths", []),
                        "weaknesses": q.get("weaknesses", []),
                        "flag": q.get("flag", False),
                    }
                    # Include authenticity flag if present
                    if "authenticity_flag" in q:
                        clean_q["authenticity_flag"] = q["authenticity_flag"]
                    cleaned.append(clean_q)
                return cleaned

            report_data = {
                # Interview stats
                "interview_duration": template_data.get("interview", {}).get("duration"),
                "questions_asked_count": template_data.get("interview", {}).get("questions_count", 0),
                "total_questions_configured": template_data.get("interview", {}).get("total_questions", 15),

                # Scores
                "resume_analysis_score": template_data.get("resume_analysis", {}).get("score", 0.0),
                "resume_analysis_text": template_data.get("resume_analysis", {}).get("recommendation"),
                "interview_final_score": template_data.get("interview_analysis", {}).get("score", 0.0),
                "interview_analysis_text": template_data.get("interview_analysis", {}).get("recommendation"),

                # Verdict
                "verdict_passed": template_data.get("verdict", {}).get("passed", False),
                "verdict_status": template_data.get("verdict", {}).get("status"),
                "overall_assessment": template_data.get("verdict", {}).get("overall_assessment"),
                "strengths": template_data.get("verdict", {}).get("strengths", {}).get("items", []),
                "weaknesses": template_data.get("verdict", {}).get("areas", {}).get("items", []),

                # Performance overview
                "performance_summary": template_data.get("overview", {}).get("summary"),
                "avg_metrics": clean_metrics(template_data.get("overview", {}).get("metrics", [])),

                # Per-question detail
                "questions_data": clean_questions(template_data.get("questions", {}).get("list", [])),
                "conversation_timeline": template_data.get("questions", {}).get("conversation_timeline", []),

                # Mandatory skills
                "has_mandatory_skills": template_data.get("mandatory", {}).get("has_mandatory_skills", False),
                "mandatory_skills_text": template_data.get("mandatory", {}).get("mandatory_skills"),
                "mandatory_skills_evaluation": template_data.get("mandatory_skills_evaluation"),

                # Authorship / AI detection
                "authorship_analysis": template_data.get("authorship_analysis"),

                # Behavioral metrics
                "behavioral_metrics": template_data.get("behavioral_metrics"),

                # Raw analysis inputs (for full regeneration)
                "json_final_analysis": json_final_analysis if isinstance(json_final_analysis, dict) else None,
                "analysis_blocks_raw": analysis_blocks if isinstance(analysis_blocks, (list, dict)) else None,
            }

            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"✅ Extracted report data for storage: "
                f"score={report_data['interview_final_score']}, "
                f"verdict={report_data['verdict_status']}, "
                f"questions={report_data['questions_asked_count']}"
            )
            return report_data

        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] ❌ Error extracting report data for storage: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {}

    def _generate_comprehensive_improvements(self, analysis_blocks, final_score, individual_weaknesses):
        """
        Generate comprehensive improvement recommendations based on overall performance patterns
        """
        comprehensive_improvements = []
        def normalize_score(score):
            s = float(score) if score else 0
            if s > 5:
                s = s / 20.0
            return min(5.0, max(0.0, s))
        
        poor_questions = [b for b in analysis_blocks if isinstance(b, dict) and normalize_score(b.get('overall_score', 0)) < 1.5]
        very_poor_questions = [b for b in analysis_blocks if isinstance(b, dict) and normalize_score(b.get('overall_score', 0)) < 0.75]
        
        if len(very_poor_questions) >= 3:
            comprehensive_improvements.append("Requires fundamental training in core job requirements and domain knowledge")
        if len(poor_questions) >= 4:
            comprehensive_improvements.append("Needs significant improvement in interview preparation and question comprehension")

        irrelevant_responses = 0
        confrontational_responses = 0
        minimal_responses = 0
        technical_confusion = 0

        for block in analysis_blocks:
            if isinstance(block, dict):
                answer = (block.get('answer') or '').lower()
                analysis = (block.get('analysis') or '').lower()

                if 'unrelated' in analysis or 'irrelevant' in analysis:
                    irrelevant_responses += 1
                if 'confrontational' in analysis or 'bad is right' in answer or 'baddies' in answer:
                    confrontational_responses += 1
                if len(answer.strip()) <= 3 or answer.strip() == '.':
                    minimal_responses += 1
                if 'confused' in analysis or 'misunderstood' in analysis or 'badis' in answer:
                    technical_confusion += 1

        if irrelevant_responses >= 2:
            comprehensive_improvements.append("Must improve listening skills and ability to understand question context")
        if confrontational_responses >= 1:
            comprehensive_improvements.append("Needs training in professional communication and handling unfamiliar terminology")
        if minimal_responses >= 2:
            comprehensive_improvements.append("Requires development of communication skills and interview engagement")
        if technical_confusion >= 3:
            comprehensive_improvements.append("Lacks fundamental knowledge in core technical concepts for this role")

        if final_score < 3.0:
            comprehensive_improvements.append("Candidate requires comprehensive training before being suitable for this position")
        elif final_score < 4.0:
            comprehensive_improvements.append("Significant skill development needed in multiple competency areas")

        return comprehensive_improvements[:3]

    @staticmethod
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

    def save_image_to_png(self, id, output_dir='images', process_id=0, room_id=""):
        """
        Retrieve best_face bytea from interviews via API and save as PNG
        """
        try:
            import integrations.aira_sql as airadb
            result = airadb._api_call("GET", f"/interview/best-face/{id}")
            face_b64 = result.get("bestFace")
            if not face_b64:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] ⚠️ No image data from API for interview id: {id}")
                return None
            bytea_data = self._normalize_bytea(base64.b64decode(face_b64))
            if not bytea_data:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] ⚠️ Could not normalize image data for interview id: {id}")
                return None

            logger.info(f"[process id: {process_id}][room id: {room_id}] 📊 Total bytea size: {len(bytea_data)} bytes")

            os.makedirs(output_dir, exist_ok=True)
            filepath = os.path.join(output_dir, f"interview_{id}_best_face.png")

            image = Image.open(io.BytesIO(bytea_data))
            logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Image format detected: {image.format}, Size: {image.size}, Mode: {image.mode}")

            if image.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', image.size, (255, 255, 255))
                if image.mode == 'P':
                    image = image.convert('RGBA')
                background.paste(image, mask=image.split()[-1] if image.mode in ('RGBA', 'LA') else None)
                image = background
            elif image.mode != 'RGB':
                image = image.convert('RGB')

            image.save(filepath, 'PNG')
            logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Image saved successfully via API: {filepath}")
            return filepath

        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] ❌ Error saving image via API: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def inspect_bytea(self, id, process_id, room_id):
        """
        Inspect the bytea data to understand its format via API
        """
        try:
            import integrations.aira_sql as airadb
            result = airadb._api_call("GET", f"/interview/best-face/{id}")
            face_b64 = result.get("bestFace")
            if not face_b64:
                logger.error(f"[process id: {process_id}][room id: {room_id}] No image data found via API")
                return None
            bytea_data = self._normalize_bytea(base64.b64decode(face_b64))
            if not bytea_data:
                logger.error(f"[process id: {process_id}][room id: {room_id}] Could not normalize bytea data")
                return None

            logger.info(f"[process id: {process_id}][room id: {room_id}] 📊 Bytea size: {len(bytea_data)} bytes (via API)")

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
                    logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Detected format: {format_name}")
                    return format_name

            logger.warning(f"[process id: {process_id}][room id: {room_id}] ⚠️ Unknown format - not a standard image format")
            return None

        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] ❌ Error inspecting bytea via API: {e}")
            return None

    def _get_candidate_photo(self, candidate_id, process_id, room_id):
        """
        Try to load the best face image for the candidate from database/disk,
        fall back to placeholder if not available
        """
        try:
            # Prefer the face the engine captured LIVE during this interview
            # (images/<room_id>_best_face.png), then the DB-download path used by
            # report regeneration (images/interview_<id>_best_face.png).
            candidate_paths = []
            if room_id:
                candidate_paths.append(f"images/{room_id}_best_face.png")
            candidate_paths.append(f"images/interview_{candidate_id}_best_face.png")
            for image_path in candidate_paths:
                if os.path.exists(image_path):
                    logger.info(f"[process id: {process_id}][room id: {room_id}] Found candidate photo: {image_path}")
                    with open(image_path, "rb") as f:
                        encoded = base64.b64encode(f.read()).decode('utf-8')
                        return f"data:image/png;base64,{encoded}"
            logger.info(f"[process id: {process_id}][room id: {room_id}] No face image found for candidate {candidate_id}, using placeholder")
        except Exception as e:
            logger.warning(f"[process id: {process_id}][room id: {room_id}] Error loading candidate photo for {candidate_id}: {e}")

        return (
            "data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNjQiIGhlaWdodD0iNjQiIHZpZXdCb3g9IjAgMCA2NCA2NCIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPGNpcmNsZSBjeD0iMzIiIGN5PSIzMiIgcj0iMzIiIGZpbGw9IiNEREREREQiLz4KPHN2ZyB4PSIxNiIgeT0iMTYiIHdpZHRoPSIzMiIgaGVpZ2h0PSIzMiIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJub25lIiB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciPgo8cGF0aCBkPSJNMTIgMTJDMTQuNDg1MyAxMiAxNi41IDkuOTg1MjggMTYuNSA3LjVDMTYuNSA1LjAxNDcyIDE0LjQ4NTMgMyAxMiAzQzkuNTE0NzIgMyA3LjUgNS4wMTQ3MiA3LjUgNy41QzcuNSA5Ljk4NTI4IDkuNTE0NzIgMTIgMTIgMTJaIiBmaWxsPSIjNjY2NjY2Ii8+CjxwYXRoIGQ9Ik0yMC4xMTggMTkuODg1QzE4LjI3MSAxOC4wMzggMTUuMjYxIDE3IDEyIDE3SDZDNC43MzkgMTcgMS43MjkgMTguMDM4IDUuODgyIDE5Ljg4NUMyLjA2NSAyMS43NTIgMi4wNjUgMjIuMjQ4IDUuODgyIDI0LjExNUM3LjcyOSAyNS45NjIgMTAuNzM5IDI3IDEyIDI3QzEzLjI2MSAyNyAxOC4yNzEgMjUuOTYyIDIwLjExOCAyNC4xMTVDMjEuOTM1IDIyLjI0OCAyMS45MzUgMjEuNzUyIDIwLjExOCAxOS44ODVaIiBmaWxsPSIjNjY2NjY2Ii8+Cjwvc3ZnPgo8L3N2Zz4K"
        )

    def render_html(self, template_data):
        """
        Render the HTML template with the provided data
        """
        try:
            html_content = self.template.render(**template_data)
            logger.info("Successfully rendered Jinja2 template for interview report")
            return html_content
        except Exception as e:
            logger.error(f"Failed to render template: {e}")
            raise

    def save_html_file(self, html_content, candidate_id, timestamp=None):
        """
        Save HTML content to a file with candidate ID and timestamp
        Returns the file path
        """
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        reports_dir = "interview_reports"
        os.makedirs(reports_dir, exist_ok=True)

        filename = f"interview_report_{candidate_id}_{timestamp}.html"
        file_path = os.path.join(reports_dir, filename)

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            logger.info(f"Successfully saved HTML report to {file_path}")
            return file_path
        except Exception as e:
            logger.error(f"Failed to save HTML file: {e}")
            raise

    def generate_report(self, interview, json_final_analysis, analysis_blocks, candidate_id, timestamp=None, process_id=0, room_id=""):
        """
        Complete workflow: generate template data, render HTML, and save file
        Returns (html_content, file_path)
        """
        template_data = self.generate_template_data(interview, json_final_analysis, analysis_blocks, process_id, room_id)
        html_content = self.render_html(template_data)
        file_path = self.save_html_file(html_content, candidate_id, timestamp)
        return html_content, file_path

    def generate_both_reports(self, interview, json_final_analysis, analysis_blocks, candidate_id, timestamp, process_id, room_id):
        """
        Generate both Summary and Detailed reports
        Returns: (summary_html, summary_path, detailed_html, detailed_path)
        """
        try:
            logger.info(f"[process id: {process_id}][room id: {room_id}] Generating both Summary and Detailed reports...")

            template_data = self.generate_template_data(interview, json_final_analysis, analysis_blocks, process_id, room_id)

            # =====================================================
            # GATHER BEHAVIORAL METRICS FOR SUMMARY REPORT
            # =====================================================
            behavioral_metrics = {
                "authenticity_score": None,
                "tabs_switched": 0,
                "face_swaps_detected": 0,
                "multiple_faces_detected": 0,
                "sentinel_score": None,
            }

            # 1. Authenticity score (from authorship analysis already in template_data)
            auth_analysis = template_data.get("authorship_analysis")
            if auth_analysis:
                behavioral_metrics["authenticity_score"] = auth_analysis.get("authenticity_score")
                behavioral_metrics["suspicious_answers"] = auth_analysis.get("suspicious_count", 0)

            # 2. Tabs switched (from Proctor API, fallback to interviews table)
            try:
                from env_config import CONNECT_BASE_URL
                proctor_base_url = CONNECT_BASE_URL
                proctor_url = f"{proctor_base_url}/api/proctor-log/{room_id}"
                proctor_resp = requests.get(proctor_url, timeout=5)
                if proctor_resp.status_code == 200:
                    proctor_data = proctor_resp.json()
                    behavioral_metrics["tabs_switched"] = proctor_data.get("count", 0)
                else:
                    logger.warning(f"[process id: {process_id}][room id: {room_id}] Proctor API returned {proctor_resp.status_code}, falling back to DB.")
                    behavioral_metrics["tabs_switched"] = interview.get("number_of_tabs_switched", 0) or 0
            except Exception as e:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] Proctor API failed: {e}. Falling back to DB.")
                behavioral_metrics["tabs_switched"] = interview.get("number_of_tabs_switched", 0) or 0

            # 3. Sentinel metrics (face swaps, multi-person, overall score)
            try:
                processor = sentinel_report_data_processor.SentinelDataProcessor()
                processor.connect()
                sentinel_events = processor.get_interview_data(room_id)
                if sentinel_events:
                    identity = processor.calculate_identity_metrics(sentinel_events)
                    multiperson = processor.calculate_multiperson_metrics(sentinel_events)
                    violations = processor.extract_violations(sentinel_events)
                    overall = processor.calculate_overall_score(sentinel_events, violations)
                    behavioral_metrics["face_swaps_detected"] = identity.get("face_changes", 0)
                    behavioral_metrics["multiple_faces_detected"] = multiperson.get("event_count", 0)
                    behavioral_metrics["sentinel_score"] = round(100 - overall.get("violation_percentage", 0))
                processor.disconnect()
            except Exception as e:
                logger.warning(f"[process id: {process_id}][room id: {room_id}] Sentinel metrics extraction failed (non-critical): {e}")

            template_data["behavioral_metrics"] = behavioral_metrics
            logger.info(
                f"[process id: {process_id}][room id: {room_id}] "
                f"Behavioral metrics: auth={behavioral_metrics['authenticity_score']}%, "
                f"tabs={behavioral_metrics['tabs_switched']}, swaps={behavioral_metrics['face_swaps_detected']}, "
                f"multi_face={behavioral_metrics['multiple_faces_detected']}"
            )

            summary_html = self.summary_template.render(**template_data)
            detailed_html = self.template.render(**template_data)

            os.makedirs("reports", exist_ok=True)

            summary_path = f"reports/{candidate_id}_summary_report_{timestamp}.html"
            detailed_path = f"reports/{candidate_id}_detailed_report_{timestamp}.html"

            with open(summary_path, 'w', encoding='utf-8') as f:
                f.write(summary_html)
            with open(detailed_path, 'w', encoding='utf-8') as f:
                f.write(detailed_html)

            logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Summary report saved: {summary_path}")
            logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Detailed report saved: {detailed_path}")
            sentinel_report_generates=False
            try:
                sentinel_html = sentinel_report_data_processor.start_generation(room_id)
                sentinel_path = f"reports/{candidate_id}_sentinel_report_{timestamp}.html"
                with open(sentinel_path, 'w', encoding='utf-8') as f:
                    f.write(sentinel_html)
                logger.info(f"[process id: {process_id}][room id: {room_id}] ✅ Sentinel report saved: {sentinel_path}")
                sentinel_report_generates=True
            except:
                sentinel_report_generates=False
                logger.info(f"[process id: {process_id}][room id: {room_id}] failed to generate sentinel reports...")
            # =====================================================
            # EXTRACT STRUCTURED REPORT DATA FOR DB STORAGE
            # =====================================================
            report_data_dict = self.extract_report_data_for_storage(
                template_data=template_data,
                json_final_analysis=json_final_analysis,
                analysis_blocks=analysis_blocks,
                process_id=process_id,
                room_id=room_id
            )

            all_files={}
            if sentinel_report_generates:
                all_files={
                    "summary_html":summary_html,
                    "summary_path":summary_path,
                    "detailed_html":detailed_html,
                    "detailed_path":detailed_path,
                    "sentinel_html":sentinel_html,
                    "sentinel_path":sentinel_path,
                    "report_data_dict": report_data_dict
                }
            else:
                all_files={
                    "summary_html":summary_html,
                    "summary_path":summary_path,
                    "detailed_html":detailed_html,
                    "detailed_path":detailed_path,
                    "report_data_dict": report_data_dict
                }
            return all_files

        except Exception as e:
            logger.error(f"[process id: {process_id}][room id: {room_id}] Error generating both reports: {e}")
            raise

    def _get_aira_logo(self):
        """
        Load AIRA logo and convert to base64
        Returns base64 encoded logo or None for emoji fallback
        """
        try:
            logo_path = "assets/aira_logo.png"
            if os.path.exists(logo_path):
                with open(logo_path, "rb") as f:
                    logo_data = f.read()
                    encoded = base64.b64encode(logo_data).decode('utf-8')
                    return f"data:image/png;base64,{encoded}"
            else:
                return None
        except Exception as e:
            logger.warning(f"Error loading AIRA logo: {e}")
            return None

    def _format_timeline_timestamps(self, conversation_timeline):
        """Format timestamps to keep 24-hour HH:MM:SS format"""
        from datetime import datetime
        for item in conversation_timeline:
            timestamp = item.get('timestamp', '')
            if not timestamp:
                continue
            try:
                dt = None
                # Try common timestamp formats
                try:
                    dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    try:
                        dt = datetime.strptime(timestamp, "%Y-%m-%d %H%M%S")
                    except ValueError:
                        try:
                            timestamp_clean = timestamp.replace('-', ':', 2)
                            dt = datetime.strptime(timestamp_clean, "%Y:%m:%d %H:%M:%S")
                        except ValueError:
                            pass

                # If parsed successfully, reformat in 24-hour HH:MM:SS
                if dt:
                    item['timestamp'] = dt.strftime("%H:%M:%S")

            except Exception as e:
                logger.warning(f"Could not parse timestamp {timestamp}: {e}")
        return conversation_timeline

    def _get_total_questions(self, interview):
        """
        Calculate total questions from interview_config JSON field in DB.
        Returns: questions_per_round (the interview loop runs 1 round only).
        """
        try:
            import json
            raw_config = interview.get("interview_config")
            if not raw_config:
                return 15
            
            
            if isinstance(raw_config, str):
                config_dict = json.loads(raw_config)
            else:
                config_dict = raw_config
            
            company_name = interview.get('company_name', 'Unknown Company')
            if company_name == "DemoCompany":
                return 5
            
            q_per_round = config_dict.get("questions_per_round", 15)
            
            return q_per_round
        except Exception as e:
            logger.warning(f"Error calculating total questions: {e}")
            return 15



def sanitize_filename(filename):
    """Sanitize filename to remove invalid characters"""
    import re
    return re.sub(r'[<>:\"/\\|?*]', '_', filename)


# Backward compatibility functions
def generate_template_data(interview, json_final_analysis, analysis_blocks, process_id, room_id):
    handler = InterviewReportTemplate()
    return handler.generate_template_data(interview, json_final_analysis, analysis_blocks, process_id, room_id)

def render_interview_template(interview, json_final_analysis, analysis_blocks, process_id, room_id):
    handler = InterviewReportTemplate()
    template_data = handler.generate_template_data(interview, json_final_analysis, analysis_blocks, process_id, room_id)
    return handler.render_html(template_data)