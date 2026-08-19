"""
Interview Simulation Script — Phase 3B (Full Pipeline)

Mirrors the production webrtc_interview.py flow WITHOUT browser/Selenium/video.
Uses GPT-4o-mini as the simulated candidate (150-word max answers).

Includes:
- Full ResponsesClient Q/A loop
- Phase 3A async evaluation pipeline (DB inserts + background evaluation)
- Phase 3B post-interview baseline smoothing
- Role-calibrated candidate personas (junior/mid/senior)
- Difficulty escalation with constraints
- All 27 permutations: 3 Roles × 3 Difficulties × 3 Rounds
- Enhanced HTML report (v5) with candidate answers + adaptation data

Usage:
    python3 simulate_interviews.py
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import os
import sys
import time
import json
import logging
import html as html_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Any

# Ensure we can import engine modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from openai import OpenAI
from core.responses_client import ResponsesClient
from core.interview_config import InterviewConfig, InterviewContext, RuntimeSkillState
from core.difficulty_policy import get_escalation_debug_info, compute_difficulty_vector

# Phase 3A imports
from core.evaluation_engine import evaluate_answer, audit_question_quality
from integrations.aira_sql import (
    Session as AiraSession,
    insert_question_row,
    update_question_evaluation,
    mark_evaluation_failed,
    upsert_project,
    InterviewQuestion,
    update_skill_baseline,
    update_interview,
    save_response
)
from aira_3_webrtc_orchestration import get_db_connection, _claim_ready_interviews, DEPLOYMENT
import random
from zoneinfo import ZoneInfo
from reflex_engine import ReflexEngine

# Phase 3B imports
from skill_adaptation import aggregate_interview_results

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

from dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    print("❌ ERROR: OPENAI_API_KEY environment variable not found.")
    sys.exit(1)

openai_client = OpenAI(api_key=API_KEY)

MANDATORY_SKILLS = "Strong foundation in Linear Algebra and Probability, Hands-on experience with PyTorch for deep learning, Deep understanding of Transformer architectures, Experience fine-tuning LLMs using LoRA or PEFT, Proven experience building RAG systems, Practical experience with LangChain ecosystem, Experience with vector databases such as FAISS or Pinecone, Strong Python programming skills for ML integration"
JOB_DESCRIPTION = """
---

# Job Title: Senior AI Engineer – LLM & Intelligent Systems

## Role Overview

We are seeking a high-impact AI Engineer to design, build, and deploy scalable machine learning and large language model (LLM) systems in production environments. This role requires strong mathematical foundations, deep learning expertise, and hands-on experience with the LangChain ecosystem for agentic and retrieval-augmented architectures.

You will operate at the intersection of research and engineering — translating theoretical models into robust, optimized, and secure AI systems that deliver measurable business value.

---

## Key Responsibilities

* Design and implement deep learning models using TensorFlow and PyTorch for NLP, computer vision, and time-series applications
* Develop and optimize large language model pipelines including RAG, multi-agent workflows, and memory-augmented reasoning systems
* Build agentic architectures using LangChain, LangGraph, LangServe, and related ecosystem tools
* Fine-tune transformer models using LoRA, QLoRA, PEFT, and domain-specific adaptation techniques
* Apply mathematical optimization techniques to improve model convergence, training efficiency, and inference latency
* Develop scalable model-serving infrastructure using FastAPI, Docker, Kubernetes, and cloud platforms
* Design vector search pipelines using FAISS, Pinecone, or similar embedding databases
* Conduct model evaluation, benchmarking, and ablation studies to ensure robustness and minimize hallucinations
* Collaborate with cross-functional teams to translate business requirements into AI-driven solutions
* Contribute to research initiatives in alignment, interpretability, and model efficiency

---

## Required Qualifications

* Bachelor's or Master's degree in Computer Science, Artificial Intelligence, Mathematics, or related field
* Strong foundation in Linear Algebra, Probability, Statistics, and Optimization
* 3+ years of hands-on experience with TensorFlow and/or PyTorch
* Deep understanding of Transformer architectures and attention mechanisms
* Experience building RAG systems and agentic workflows
* Proficiency with LangChain ecosystem tools (LangChain, LangGraph, LangServe)
* Experience with vector databases (FAISS, Weaviate, Pinecone, Milvus)
* Strong programming skills in Python
* Experience deploying ML systems in cloud environments (AWS, GCP, or Azure)

---

## Preferred Qualifications

* Experience with distributed training and mixed precision optimization
* Familiarity with CUDA and GPU acceleration
* Knowledge of CI/CD pipelines for ML systems (MLOps practices)
* Experience with reinforcement learning or model alignment techniques
* Contributions to open-source AI projects or published research
* Exposure to DevOps practices including Docker, Kubernetes, Terraform

---

## Core Competencies

* Strong mathematical reasoning and algorithmic thinking
* Ability to translate research papers into production-grade implementations
* Performance optimization mindset
* Systems-level thinking across training, serving, and monitoring
* Ownership and accountability for end-to-end AI lifecycle

---

## What Success Looks Like

* Production-ready LLM systems with low latency and high reliability
* Measurable reduction in model hallucination or drift
* Scalable agentic systems handling multi-step reasoning tasks
* Optimized training pipelines with efficient compute utilization
* Research-informed engineering decisions

---
"""
RESUME_TEXT = """

# HEMANTH REDDY NALABOLU

**AI Researcher & Developer | LLM Systems Architect | Applied ML Engineer**
Telangana, India
+91 9014995824 | hemanth984849@gmail.com | LinkedIn | GitHub

---

## EXECUTIVE SUMMARY

AI Researcher and Developer with 7+ years of experience designing, fine-tuning, and deploying large-scale AI systems across NLP, generative AI, and distributed ML infrastructure. Proven expertise in Transformer architectures, parameter-efficient fine-tuning (LoRA/QLoRA), Retrieval-Augmented Generation (RAG), and agentic multi-step reasoning frameworks.

Experience spans enterprise-grade model optimization, scalable inference pipelines, research prototyping, and production deployment across cloud-native ecosystems.

Former AI Intern at NVIDIA and AI Developer at OpenAI, currently contributing to advanced research initiatives focused on efficient LLM alignment, scalable RAG architectures, and next-generation agentic systems.

---

## PROFESSIONAL EXPERIENCE

### AI Researcher & Developer

Confidential Research Team | 2024 – Present

* Leading research initiatives on scalable RAG pipelines and low-latency LLM inference architectures
* Designing multi-agent systems using LangGraph and advanced ReAct-style orchestration
* Conducting experimentation on alignment strategies, hallucination mitigation, and memory-augmented LLM systems
* Publishing internal research findings on efficiency optimization in parameter-efficient fine-tuning

---

### AI Developer

OpenAI | 2020 – 2024

* Contributed to development and optimization of large-scale Transformer-based language models
* Worked on fine-tuning workflows using LoRA/QLoRA and PEFT for domain-specific adaptation
* Designed retrieval pipelines integrating FAISS and semantic chunking strategies for production RAG systems
* Built internal tools for model evaluation, benchmarking, and hallucination detection
* Collaborated cross-functionally with research and infrastructure teams to improve inference efficiency and reduce serving latency
* Supported deployment of cloud-scale AI services with high availability and automated CI/CD pipelines

---

### AI Intern

NVIDIA | 2019 – 2020

* Assisted in research on GPU-accelerated Transformer training optimization
* Benchmarked large-model training performance across CUDA-based architectures
* Contributed to memory-efficient training experiments for large-scale sequence models
* Supported experimentation involving mixed precision training and distributed data parallelism

---

## SELECTED PROJECTS & RESEARCH WORK

### Enterprise Medical Intelligence System

* Designed an agentic medical assistant leveraging multi-step reasoning workflows
* Implemented secure user session management and structured triage scoring mechanisms
* Integrated real-time knowledge retrieval pipelines from medical research databases
* Optimized inference latency for real-time diagnostic interaction

---

### Legal LLM with Domain-Specific Fine-Tuning

* Fine-tuned a 1.3B parameter Falcon model using QLoRA for legal domain adaptation
* Implemented FAISS-powered vector retrieval with metadata-aware chunking
* Designed citation-backed response mechanisms with multi-turn reasoning memory
* Reduced hallucination rate by 23% through retrieval-grounded prompting techniques

---

### Anomaly Transformer for Time-Series Intelligence

* Implemented discrepancy-based attention mechanisms for temporal anomaly detection
* Achieved high F1 performance across unseen SMAP dataset sequences
* Developed visualization interface for threshold tuning and anomaly traceability

---

## TECHNICAL EXPERTISE

**Programming:** Python, Java, Bash, YAML
**AI/ML:** PyTorch, HuggingFace Transformers, PEFT, LangChain, LangGraph
**LLM Systems:** RAG, LoRA/QLoRA, Vector Databases, ReAct Workflows, Model Alignment
**Cloud & DevOps:** AWS (EC2, S3, ECR, Lambda), Docker, Terraform, Jenkins, CI/CD
**Databases:** MongoDB, FAISS, MySQL, SQLite
**Infrastructure:** GPU Optimization, Distributed Training, CUDA, Mixed Precision

---

## EDUCATION

Bachelor of Engineering – Computer Science & Engineering
Hyderabad, India



"""

ROLES = os.environ.get("SIM_ROLES", "mid").split(",")
DIFFICULTIES = os.environ.get("SIM_DIFFICULTIES", "medium").split(",")
ROUNDS = [1]

TOTAL_QUESTIONS = 20
QUESTIONS_PER_ROUND = int(os.environ.get("SIM_QPR", "8"))

# Limit concurrency to avoid rate limits
MAX_WORKERS = 3

# Simulated interview_id base (incremented per simulation)
_sim_interview_counter = 900000  # High range to avoid colliding with real interviews

# Phase 3A: Dedicated executor for simulation evaluations (separate from production)
sim_evaluation_executor = ThreadPoolExecutor(
    max_workers=5,
    thread_name_prefix="sim-eval-worker",
)


# ═══════════════════════════════════════════════════════════════════════════
# GPT-AS-CANDIDATE: Generates realistic 150-word max answers
# ═══════════════════════════════════════════════════════════════════════════

CANDIDATE_SYSTEM_PROMPT = """You are simulating a technical interview candidate for an AI/ML engineering role.
You have the following background (use it to ground your answers):
{resume_summary}

{role_persona}

RULES:
- Answer the interviewer's question in 150 words MAXIMUM
- Give substantive, technical answers — not filler
- Reference specific tools, frameworks, or projects from your experience when relevant
- Sound like a real human candidate, not a chatbot
- Use first person ("I", "we", "my team")
- Mention project names, numbers, or metrics when you can
- Mentally roll a 10-sided die. On a 1, ask a conversational question (e.g., "Can you clarify?", "How's the company culture?"). On a 2, ask to skip the question. On a 3, ask to end the interview early. Otherwise, answer normally."""

_ROLE_PERSONAS = {
    "junior": (
        "You are a JUNIOR developer with 1-2 years of experience.\n"
        "BEHAVIOR PROFILE:\n"
        "- You know Python well and have basic PyTorch exposure\n"
        "- You've done a few course projects but limited production experience\n"
        "- For topics like RAG, LangChain, Docker, you give vague or textbook answers\n"
        "- For advanced topics (LoRA, distributed training, system design), honestly say you have limited experience\n"
        "- You occasionally mention a college project: 'sentiment analysis tool using BERT'\n"
        "- You never mention production scale, SLAs, or multi-team coordination\n"
        "- Some answers are good (Python, basics), some are clearly weak (infra, scale)"
    ),
    "mid": (
        "You are a MID-LEVEL developer with 3-5 years of experience.\n"
        "BEHAVIOR PROFILE:\n"
        "- Solid hands-on with PyTorch, Transformers, and basic fine-tuning\n"
        "- You've built a RAG pipeline at your previous company using FAISS + LangChain\n"
        "- You know Docker well, have some Kubernetes exposure\n"
        "- You can discuss trade-offs but sometimes miss edge cases\n"
        "- You mention real projects: 'document search system serving 10K daily queries'\n"
        "- For system design questions, you give reasonable but not expert-level answers\n"
        "- You're honest about gaps in distributed training and model alignment"
    ),
    "senior": (
        "You are a SENIOR engineer with 7+ years of experience.\n"
        "BEHAVIOR PROFILE:\n"
        "- Deep expertise across the full ML stack — training, serving, monitoring\n"
        "- You've led the development of a production RAG system handling 500K queries/day\n"
        "- You fine-tuned LLMs using QLoRA on custom enterprise data at scale\n"
        "- You designed a multi-agent system using LangGraph for automated compliance review\n"
        "- You speak with authority about trade-offs: latency vs accuracy, cost vs quality\n"
        "- You reference real metrics: 'reduced hallucination rate by 23%', 'P95 latency 120ms'\n"
        "- You mention team leadership: 'I led a team of 4 engineers on this project'\n"
        "- For system design, you decompose problems into components with clear reasoning\n"
        "- You proactively mention production concerns: monitoring, rollback, A/B testing"
    ),
}


def generate_candidate_answer(question: str, role: str, round_num: int, difficulty: str = "medium") -> str:
    """Use GPT to simulate a candidate answering an interview question.
    Role-calibrated: junior has gaps, mid is solid, senior has depth + projects."""
    persona = _ROLE_PERSONAS.get(role, _ROLE_PERSONAS["mid"])

    # Difficulty hint: harder questions might get weaker answers from junior
    diff_hint = ""
    if role == "junior" and difficulty == "hard":
        diff_hint = "\nThis is a very hard question for your experience level. Struggle visibly — give a partial answer."
    elif role == "senior" and difficulty == "easy":
        diff_hint = "\nThis is an easy question for you. Answer confidently and briefly."

    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": CANDIDATE_SYSTEM_PROMPT.format(
                        resume_summary=RESUME_TEXT[:1500],
                        role_persona=persona,
                    ) + diff_hint,
                },
                {"role": "user", "content": f"Interview question: {question}"},
            ],
            temperature=0.7,
            max_tokens=250,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Candidate GPT failed: {e}")
        return "I would need to think more carefully about that and discuss with my team."


# ═══════════════════════════════════════════════════════════════════════════
# PHASE 3A: Background evaluation (simulation version)
# ═══════════════════════════════════════════════════════════════════════════

def sim_process_evaluation(
    interview_id: int,
    row_id: int,
    skill: str,
    question_text: str,
    answer_text: str,
    round_number: int,
    question_number: int,
    skill_states: dict = None,
) -> None:
    """Background thread evaluation for simulation — same logic as production."""
    logger.info(
        "🧵 [sim-eval] Starting: sim_interview=%s Q%s row=%s",
        interview_id, question_number, row_id,
    )
    thread_session = AiraSession()
    try:
        result = evaluate_answer(skill, question_text, answer_text, round_number)

        update_question_evaluation(
            thread_session,
            row_id,
            summary=result.get("summary", ""),
            confidence_score=result.get("confidence_score", 0.5),
            evaluation_tags=result.get("evaluation_tags", []),
        )

        if skill_states and skill in skill_states:
            state = skill_states[skill]
            if getattr(state, "status", "active") != "removed":
                conf = result.get("confidence_score", 0.5)
                state.recent_conf.append(conf)
                logger.info("📈 [sim-eval] Appended conf %.2f to '%s' (history: %s)", conf, skill, list(state.recent_conf))
                
                # Concept Tracking (Phase 4): Extract tags safely, no status mutation
                from core.interview_config import ConceptState
                concept_tags = result.get("concept_tags", [])
                for tag in concept_tags:
                    if not isinstance(tag, str) or not tag.strip():
                        continue
                    tag = tag.strip().lower()
                    if tag not in state.concepts:
                        state.concepts[tag] = ConceptState(tag)
                    state.concepts[tag].score_history.append(conf)
                    logger.info(f"🧠 [sim-eval] Appended conf {conf:.2f} to concept '{tag}' -> history len: {len(state.concepts[tag].score_history)}")

        projects = result.get("projects_mentioned", [])
        for proj in projects:
            if isinstance(proj, dict) and proj.get("name"):
                upsert_project(
                    thread_session,
                    interview_id,
                    proj,
                    question_number,
                    round_number,
                )

        logger.info(
            "✅ [sim-eval] Done: sim_interview=%s Q%s conf=%.2f tags=%s",
            interview_id, question_number,
            result.get("confidence_score", 0.5),
            result.get("evaluation_tags", []),
        )
    except Exception as e:
        logger.error(
            "❌ [sim-eval] Failed: sim_interview=%s Q%s row=%s: %s",
            interview_id, question_number, row_id, e,
            exc_info=True,
        )
        try:
            mark_evaluation_failed(thread_session, row_id)
        except Exception:
            pass
    finally:
        thread_session.close()


# ═══════════════════════════════════════════════════════════════════════════
# SIMULATION LOGIC
# ═══════════════════════════════════════════════════════════════════════════

def run_simulation(interview: dict, config: 'InterviewConfig', context: 'InterviewContext', sim_candidate_id: int) -> dict:
    '''
    Simulates a single interview round using ReflexEngine for split brain logic.
    '''
    global _sim_interview_counter
    _sim_interview_counter += 1
    sim_interview_id = interview['id']
    
    role = config.role_experience_required
    difficulty = config.base_difficulty
    round_num = context.current_round
    sim_id = f"SIM_{sim_interview_id}_{role.upper()}_{difficulty.upper()}"

    logger.info(f"🚀 Starting Simulation Inner: {sim_id}")

    candidate_skill_baselines = {}
    if round_num > 1:
        try:
            baseline_session = AiraSession()
            from integrations.aira_sql import load_skill_baselines
            candidate_skill_baselines = load_skill_baselines(baseline_session, sim_candidate_id)
            baseline_session.close()
            logger.info("📈 [sim] Loaded %d baselines for %s", len(candidate_skill_baselines), sim_id)
        except Exception as e:
            logger.warning("⚠️ [sim] Failed to load baselines: %s", e)

    client = ResponsesClient(
        api_key=API_KEY, 
        interview_config=config,
        candidate_skill_baselines=candidate_skill_baselines,
    )
    req_skills = interview.get('mandatory_skills') or MANDATORY_SKILLS
    client.init_skill_tracker(req_skills.split(", "))
    
    reflex_engine = ReflexEngine(api_key=API_KEY, interview_config=config)
    process_id = os.getpid()
    room_id = f"sim_room_{sim_interview_id}"
    recent_acks = []

    messages = [{"role": "system", "content": f"You are an AI interviewer. Skills: {req_skills}."}]
    log_entries = []
    eval_futures = []

    try:
        # ── Q1: Greeting ──
        q1_messages = messages.copy()
        q1_messages.append({"role": "user", "content": "Start the interview with the greeting question."})

        greeting_resp, _, prev_resp_id = client.create_response(
            question_number=0,
            messages=q1_messages,
            use_stateful=False,
            context=context,
        )

        greeting_debug = get_escalation_debug_info(
            difficulty, role, 1, 1,
            override_vector=getattr(client, '_last_used_vector', None)
        )

        greeting_answer = generate_candidate_answer(greeting_resp, role, round_num)

        log_entries.append({
            "q_num": 1,
            "question": greeting_resp,
            "answer": greeting_answer,
            "skills": ["Communication"],
            "format": "GREETING",
            "constraint": "None",
            "level": greeting_debug.get("A", 1),
            "debug": greeting_debug,
            "eval_row_id": None,
            "intent": "None"
        })

        # ── "How's your day?" sub-step (mirrors production Q1 flow) ──
        day_answer = generate_candidate_answer(
            "How has your day been so far?", role, round_num
        )
        logger.info(f"🎤 [sim] Day response: '{day_answer[:80]}...'")

        # ── ✨ WARMUP: "Tell me about yourself" (mirrors production — NOT evaluated) ──
        if round_num == 1:
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
            warmup_answer = generate_candidate_answer(warmup_q, role, round_num)
            logger.info(
                f"🎙️ [sim] WARMUP question: '{warmup_q[:60]}...' | "
                f"answer: '{warmup_answer[:80]}...'"
            )
            # Log as warmup — NOT added to scored log_entries, NOT counted in q_num
            log_entries.append({
                "q_num": "W",
                "question": warmup_q,
                "answer": warmup_answer,
                "skills": ["Warmup"],
                "format": "WARMUP",
                "constraint": "None",
                "level": 0,
                "debug": {},
                "eval_row_id": None,
                "intent": "None",
                "is_warmup": True,
            })

        current_resp_id = prev_resp_id
        last_question = greeting_resp
        previous_intent = "others"
        
        q_num = 1
        TOTAL_QUESTIONS_LOCAL = config.questions_per_round
        followup_chain_count = 0
        # Weighted random: how many follow-ups for THIS question [0,1,2,3] with [15%,35%,40%,10%]
        import random as _rand
        max_followups_for_question = _rand.choices([0, 1, 2, 3], weights=[15, 35, 40, 10], k=1)[0]
        logger.info(f"🎲 [sim] FOLLOWUP BUDGET for Q1: max={max_followups_for_question}")
        
        while q_num < TOTAL_QUESTIONS_LOCAL:
            q_num += 1

            candidate_answer = log_entries[-1]["answer"]
            
            # ═══════════════════════════════════════════════════════════
            # DUAL REFLEX STAGE — Main + Follow-up fired in PARALLEL
            # ═══════════════════════════════════════════════════════════
            
            # Get abstraction level for follow-up reflex
            current_abstraction_level = 2
            if hasattr(client, '_last_used_vector') and client._last_used_vector:
                current_abstraction_level = client._last_used_vector.abstraction_depth
            
            # Only fire follow-up reflex if: Q3+, chain < 2, and not first few questions.
            # Also suppress it when the configured quotas (conceptual/scenario/resume)
            # can no longer spare a slot — an experience follow-up would eat a slot the
            # config wants for a quota question.
            fire_followup_reflex = (
                q_num >= 4 and             # Q3+ (post-intro questions)
                followup_chain_count < max_followups_for_question and  # Within random budget
                not client.quota_needs_slot(q_num)  # Quota budget has room
            )
            
            if fire_followup_reflex:
                # Parallel fire: main reflex + follow-up reflex
                with ThreadPoolExecutor(max_workers=2) as dual_executor:
                    main_future = dual_executor.submit(
                        reflex_engine.generate_reflex,
                        transcript=candidate_answer,
                        current_question=last_question,
                        jd_summary=interview.get('job_description') or 'AI Engineer relevant to JD',
                        mandatory_skills=req_skills,
                        question_number=q_num - 1,
                        recent_acks=recent_acks,
                        room_id=room_id,
                        process_id=process_id
                    )
                    followup_future = dual_executor.submit(
                        reflex_engine.generate_followup_reflex,
                        transcript=candidate_answer,
                        current_question=last_question,
                        mandatory_skills=req_skills,
                        question_number=q_num - 1,
                        abstraction_level=current_abstraction_level,
                        recent_acks=recent_acks,
                        room_id=room_id,
                        process_id=process_id
                    )
                    main_reflex_result = main_future.result()
                    followup_reflex_result = followup_future.result()
                
                logger.info(
                    f"⚡🔄 [sim] DUAL REFLEX Q{q_num-1}: "
                    f"main_intent={main_reflex_result['intent']} | "
                    f"followup_worthy={followup_reflex_result.get('followup_worthy', False)}"
                )
            else:
                # Single reflex only (Q2-Q3, or chain limit reached)
                main_reflex_result = reflex_engine.generate_reflex(
                    transcript=candidate_answer,
                    current_question=last_question,
                    jd_summary=interview.get('job_description') or 'AI Engineer relevant to JD',
                    mandatory_skills=req_skills,
                    question_number=q_num - 1,
                    recent_acks=recent_acks,
                    room_id=room_id,
                    process_id=process_id
                )
                followup_reflex_result = {"followup_worthy": False, "ack": "", "followup_topic": ""}
                logger.info(f"⚡ [sim] SINGLE REFLEX Q{q_num-1}: intent={main_reflex_result['intent']}")
            
            # ═══════════════════════════════════════════════════════════
            # PRIORITY LOGIC — Decide main vs follow-up
            # ═══════════════════════════════════════════════════════════
            intent = main_reflex_result["intent"]
            use_followup = False
            
            if intent.upper() in ('REPEAT', 'SKIP', 'EXIT'):
                # Main reflex WINS — highest priority
                use_followup = False
                ack = main_reflex_result["ack"]
            elif followup_reflex_result.get("followup_worthy", False) and fire_followup_reflex:
                # Check coverage: can we afford a follow-up?
                remaining_skills = len(client.skill_tracker.uncovered) if client.skill_tracker else 0
                remaining_qs = TOTAL_QUESTIONS_LOCAL - q_num
                
                if remaining_skills <= remaining_qs:
                    # 70/30 weighted coin flip
                    coin_flip = _rand.choices([True, False], weights=[80, 20], k=1)[0]
                    if coin_flip:
                        use_followup = True
                        ack = followup_reflex_result["ack"]
                        logger.info(
                            f"🔄 [sim] FOLLOWUP ACTIVATED Q{q_num}: "
                            f"chain={followup_chain_count + 1}/{max_followups_for_question} | "
                            f"remaining_skills={remaining_skills} remaining_qs={remaining_qs} | coin=YES | "
                            f"topic='{followup_reflex_result.get('followup_topic', '')[:60]}'"
                        )
                    else:
                        use_followup = False
                        ack = main_reflex_result["ack"]
                        logger.info(
                            f"🎲 [sim] FOLLOWUP SKIPPED by coin flip (30% chance) Q{q_num}"
                        )
                else:
                    # Coverage too tight → must advance to new skill
                    use_followup = False
                    ack = main_reflex_result["ack"]
                    logger.info(
                        f"⚠️ [sim] FOLLOWUP SUPPRESSED Q{q_num}: "
                        f"remaining_skills={remaining_skills} > remaining_qs={remaining_qs}"
                    )
            else:
                # Follow-up not worthy or not fired → use main
                use_followup = False
                ack = main_reflex_result["ack"]
            
            if ack:
                recent_acks.append(ack)
                if len(recent_acks) > 3: recent_acks.pop(0)
                
            client.process_response(intent)
            
            # ═══════════════════════════════════════════════════════════
            # INTENT HANDLING + PLANNER DISPATCH
            # ═══════════════════════════════════════════════════════════
            
            if intent.upper() == 'EXIT':
                logger.info("🚪 [sim] Candidate exited early.")
                break
                
            if intent.upper() == 'REPEAT':
                logger.info("🔁 [sim] Candidate requested repeat.")
                response_text = last_question
                planner_metrics = {}
                q_num -= 1  # Stay on the same question
                followup_chain_count = 0  # Reset chain on repeat
                max_followups_for_question = _rand.choices([0, 1, 2, 3], weights=[15, 35, 40, 10], k=1)[0]
                
            elif use_followup:
                # ─── FOLLOW-UP PLANNER (separate from main planner) ───
                followup_chain_count += 1
                
                if candidate_answer and client.skill_tracker:
                    client.skill_tracker.record_answer(q_num - 1, candidate_answer)
                
                followup_result = client.create_followup_planner_response(
                    question_number=q_num - 1,
                    candidate_answer=candidate_answer,
                    previous_question=last_question,
                    followup_topic=followup_reflex_result.get("followup_topic", ""),
                    skills=client._next_skills if client._next_skills else req_skills[:2],
                    room_id=room_id,
                    process_id=process_id,
                    context=context,
                )
                
                response_text = followup_result.get("next_question", "")
                planner_metrics = followup_result.get("metrics", {})
                planner_metrics["is_followup"] = True
                planner_metrics["followup_chain"] = followup_chain_count
                current_resp_id = followup_result.get("response_id", current_resp_id)
                logger.info(
                    f"🔄 [sim] FOLLOWUP PLANNER Q{q_num}: "
                    f"chain={followup_chain_count}/{max_followups_for_question} | "
                    f"question='{response_text[:60]}...'"
                )
                
            else:
                # ─── ORIGINAL PLANNER (new topic, new skills) ───
                followup_chain_count = 0  # Reset chain on new topic
                max_followups_for_question = _rand.choices([0, 1, 2, 3], weights=[15, 35, 40, 10], k=1)[0]
                logger.info(f"🎲 [sim] FOLLOWUP BUDGET for next Q: max={max_followups_for_question}")
                
                if candidate_answer and client.skill_tracker and intent.upper() != 'SKIP':
                    client.skill_tracker.record_answer(q_num - 1, candidate_answer)
                
                planner_result = client.create_planner_response(
                    question_number=q_num - 1,
                    user_message=candidate_answer,
                    intent=intent,
                    previous_response_id=current_resp_id,
                    current_question_text=last_question,
                    resume_text=interview.get('resume_text') or RESUME_TEXT,
                    previous_intent=previous_intent,
                    context=context,
                )
                
                response_text = planner_result.get("next_question", "")
                planner_metrics = planner_result.get("metrics", {})
                current_resp_id = planner_result.get("response_id", current_resp_id)
                logger.info(f"🧠 [sim] PLANNER Q{q_num}: question='{response_text[:40]}...'")

            # Logging & DB sync
            last_log = client._question_log[-1] if client._question_log else {}
            skill_list = last_log.get("skills", []) if last_log else []
            if not skill_list and hasattr(client, '_next_skills'):
                skill_list = client._next_skills or []
            
            if response_text and client.skill_tracker and intent.upper() not in ['REPEAT', 'SKIP']:
                client.skill_tracker.register_question(q_num, skill_list, question_text=response_text)

            last_baseline = getattr(client, '_last_used_baseline', None)
            last_vector = getattr(client, '_last_used_vector', None)
            debug_info = get_escalation_debug_info(
                difficulty, role, round_num, q_num, 
                skill_baseline=last_baseline,
                override_vector=last_vector
            )
            primary_skill = skill_list[0] if skill_list else "General"

            # Predict next answer (if not Exiting)
            next_answer = generate_candidate_answer(response_text, role, round_num, difficulty)

            # Insert question into DB
            eval_row_id = None
            if candidate_answer and candidate_answer.strip() and intent.upper() not in ['SKIP', 'REPEAT']:
                try:
                    sync_session = AiraSession()
                    eval_row_id = insert_question_row(
                        sync_session,
                        interview_id=sim_interview_id,
                        round_number=round_num,
                        question_number=q_num - 1,
                        skill=str(primary_skill),
                        effective_level=int(debug_info.get("A", 1)),
                        constraint_name=str(debug_info.get("selected_constraint", "None")),
                        question_text=str(last_question),
                        answer_text=str(candidate_answer),
                        candidate_id=sim_candidate_id,
                        job_id=interview['job_id']
                    )
                    sync_session.close()

                    f = sim_evaluation_executor.submit(
                        sim_process_evaluation,
                        int(sim_interview_id), int(eval_row_id), str(primary_skill),
                        str(last_question), str(candidate_answer), int(round_num),
                        int(q_num - 1), context.skill_states,
                    )
                    eval_futures.append(f)
                except Exception as e:
                    logger.error(f"❌ [sim] DB Insert failed Q{q_num-1}: {e}")

            audit_future = None
            if response_text and q_num >= 2 and intent.upper() not in ['REPEAT']:
                vector_dict = {
                    "A": debug_info.get("A", 1), "C": debug_info.get("C", 0),
                    "Amb": debug_info.get("Amb", 0), "S": debug_info.get("S", 0),
                }
                audit_future = sim_evaluation_executor.submit(
                    audit_question_quality, response_text, skill_list, vector_dict, role, difficulty
                )

            log_entries.append({
                "q_num": q_num,
                "question": response_text,
                "answer": next_answer,
                "ack": ack if ack else "",
                "skills": skill_list,
                "format": last_log.get("format", "N/A"),
                "constraint": debug_info.get("selected_constraint", "None"),
                "level": debug_info.get("A", 1),
                "debug": debug_info,
                "eval_row_id": eval_row_id,
                "audit_future": audit_future,
                "intent": intent,
                "is_followup": use_followup,
                "followup_chain": followup_chain_count if use_followup else 0,
            })

            last_question = response_text
            previous_intent = intent

    except Exception as e:
        logger.error(f"❌ Failed Simulation {sim_id}: {e}", exc_info=True)
        return None

    logger.info(f"✅ Completed Simulation: {sim_id}")

    # Budget-arbiter adherence: planned vs delivered per quota (observability).
    if client.format_tracker:
        _adherence = client.format_tracker.adherence_report(
            pending_format=getattr(client, "_next_format", None)
        )
        if _adherence:
            logger.info(f"[{sim_id}] {_adherence}")

    if eval_futures:
        logger.info("⏳ [sim] Waiting for %d evaluations to complete...", len(eval_futures))
        from concurrent.futures import as_completed
        for future in as_completed(eval_futures):
            future.result()

    # Apply smoothing
    try:
        smooth_session = AiraSession()
        eval_rows = smooth_session.query(InterviewQuestion).filter(
            InterviewQuestion.interview_id == sim_interview_id,
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
            from skill_adaptation import aggregate_interview_results
            skill_agg = aggregate_interview_results(row_dicts)
            for skill_name, agg in skill_agg.items():
                update_skill_baseline(
                    smooth_session,
                    candidate_id=sim_candidate_id,
                    skill=skill_name,
                    interview_avg_confidence=agg["avg_confidence"],
                    interview_avg_complexity=agg["avg_complexity"],
                )
            logger.info("📈 [sim] Phase 3B: Updated %d skill baselines for %s", len(skill_agg), sim_id)
        smooth_session.close()
    except Exception as e:
        logger.warning(f"⚠️ [sim] Phase 3B smoothing failed: {e}")

    return {
        "id": sim_id,
        "role": role,
        "difficulty": difficulty,
        "round": round_num,
        "interview_id": sim_interview_id,
        "questions": log_entries,
    }



def generate_html_report(results: List[Dict]):
    total_qs = sum(len(r['questions']) for r in results)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── Resolve audit futures before building report ──
    for res in results:
        for q in res.get("questions", []):
            audit_future = q.pop("audit_future", None)
            if audit_future:
                try:
                    q["audit"] = audit_future.result(timeout=30)
                except Exception as e:
                    logger.warning("⚠️ Audit future failed: %s", e)
                    q["audit"] = {"pass": False, "reason": f"Audit error: {e}", "dimensions": {}}
            else:
                q["audit"] = None  # No audit for greeting

    # ── Compute global audit stats ──
    total_audited = 0
    total_passed = 0
    for res in results:
        for q in res.get("questions", []):
            audit = q.get("audit")
            if audit is not None:
                total_audited += 1
                if audit.get("pass"):
                    total_passed += 1

    css = (
        "* { box-sizing: border-box; margin: 0; padding: 0; } "
        "body { font-family: 'Inter', -apple-system, sans-serif; margin: 0; padding: 24px; background: #0a0a0f; color: #e0e0e0; } "
        "h1 { color: #fff; font-size: 1.8em; margin-bottom: 4px; } "
        ".subtitle { color: #888; margin-bottom: 20px; font-size: 0.9em; } "
        ".summary { display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; } "
        ".stat { background: linear-gradient(135deg, #1a1a2e, #16213e); padding: 12px 18px; border-radius: 8px; border: 1px solid #2a2a4a; min-width: 120px; } "
        ".stat-label { font-size: 0.7em; color: #888; text-transform: uppercase; letter-spacing: 1px; } "
        ".stat-value { font-size: 1.3em; font-weight: 700; color: #4fc3f7; } "
        ".config-block { background: #111116; border: 1px solid #2a2a3a; margin-bottom: 16px; border-radius: 8px; overflow: hidden; } "
        ".config-header { background: linear-gradient(135deg, #1a1a2e, #0d1b2a); padding: 10px 15px; font-weight: bold; color: #fff; font-size: 0.9em; border-bottom: 1px solid #2a2a3a; display: flex; justify-content: space-between; align-items: center; } "
        ".config-meta { display: flex; gap: 8px; } "
        ".badge { padding: 2px 8px; border-radius: 4px; font-size: 0.75em; font-weight: 600; } "
        ".badge-role { background: #1b5e20; color: #a5d6a7; } "
        ".badge-diff { background: #e65100; color: #ffcc80; } "
        ".badge-rnd { background: #0d47a1; color: #90caf9; } "
        ".badge-mult { background: #6a1b9a; color: #ce93d8; } "
        "table { width: 100%; border-collapse: collapse; } "
        "th { background: #16161e; color: #999; padding: 8px 10px; text-align: left; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.5px; } "
        "td { padding: 10px; border-top: 1px solid #1e1e2a; vertical-align: top; font-size: 0.85em; line-height: 1.5; } "
        "tr:hover td { background: #16162a; } "
        ".q-text { color: #90caf9; } "
        ".a-text { color: #a5d6a7; font-style: italic; max-height: 120px; overflow-y: auto; } "
        ".meta { font-size: 0.75em; color: #666; } "
        ".vector-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.8em; } "
        ".vec-low { background: #1b5e20; color: #a5d6a7; } "
        ".vec-mid { background: #0d47a1; color: #90caf9; } "
        ".vec-high { background: #e65100; color: #ffcc80; } "
        ".vec-max { background: #b71c1c; color: #ef9a9a; } "
        ".constraint-tag { background: #263238; color: #90a4ae; padding: 2px 6px; border-radius: 3px; font-size: 0.7em; display: inline-block; margin-top: 3px; } "
        ".audit-pass { background: #1b5e20; color: #a5d6a7; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.8em; cursor: help; } "
        ".audit-fail { background: #b71c1c; color: #ef9a9a; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.8em; cursor: help; } "
        ".audit-na { color: #555; font-size: 0.8em; } "
        ".audit-dims { font-size: 0.7em; color: #888; margin-top: 4px; } "
        ".dim-pass { color: #a5d6a7; } "
        ".dim-fail { color: #ef9a9a; }"
    )

    h = []
    h.append('<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">')
    h.append('<title>AIRA Simulation Report - Phase 3B</title>')
    h.append(f'<style>{css}</style></head><body>')
    h.append('<h1>&#x1f9e0; AIRA Simulation Report</h1>')
    h.append('<p class="subtitle">Phase 3B &mdash; Adaptive Difficulty + GPT Candidate + Async Eval + Longitudinal Smoothing</p>')
    h.append('<div class="summary">')
    h.append(f'<div class="stat"><div class="stat-label">Generated</div><div class="stat-value">{ts}</div></div>')
    h.append(f'<div class="stat"><div class="stat-label">Scenarios</div><div class="stat-value">{len(results)}</div></div>')
    h.append(f'<div class="stat"><div class="stat-label">Total Q&amp;As</div><div class="stat-value">{total_qs}</div></div>')
    h.append('<div class="stat"><div class="stat-label">Pipeline</div><div class="stat-value">3A+3B</div></div>')
    h.append('<div class="stat"><div class="stat-label">Candidate</div><div class="stat-value">GPT Role-Cal</div></div>')
    audit_pct = round(total_passed / total_audited * 100) if total_audited > 0 else 0
    audit_color = '#a5d6a7' if audit_pct >= 80 else '#ffcc80' if audit_pct >= 50 else '#ef9a9a'
    h.append(f'<div class="stat"><div class="stat-label">Audit Pass Rate</div><div class="stat-value" style="color:{audit_color}">{total_passed}/{total_audited} ({audit_pct}%)</div></div>')
    h.append('</div>')

    role_order = {"junior": 0, "mid": 1, "senior": 2}
    diff_order = {"easy": 0, "medium": 1, "hard": 2}
    results.sort(key=lambda x: (
        role_order.get(x["role"], 99),
        diff_order.get(x["difficulty"], 99),
        x["round"],
    ))

    for res in results:
        sim_id = res["id"]
        role = res["role"].title()
        diff = res["difficulty"].title()
        rnd = res["round"]

        h.append('<div class="config-block">')
        h.append('<div class="config-header">')
        h.append(f'<span>{sim_id}</span>')
        h.append('<div class="config-meta">')
        h.append(f'<span class="badge badge-role">{role}</span>')
        h.append(f'<span class="badge badge-diff">{diff}</span>')
        h.append(f'<span class="badge badge-rnd">R{rnd}</span>')
        h.append('</div></div>')
        h.append('<table><tr>')
        h.append('<th style="width:3%">#</th>')
        h.append('<th style="width:20%">Question</th>')
        h.append('<th style="width:18%">Candidate Answer</th>')
        h.append('<th style="width:15%">Ack</th>')
        h.append('<th style="width:4%">Intent</th>')
        h.append('<th style="width:5%">Format</th>')
        h.append('<th style="width:10%">Skills</th>')
        h.append('<th style="width:10%">Level</th>')
        h.append('<th style="width:15%">Audit</th>')
        h.append('</tr>')

        for q in res["questions"]:
            skills = ", ".join(q["skills"]) if isinstance(q["skills"], list) else str(q["skills"])
            debug = q.get("debug", {})
            answer = q.get("answer", "")
            ack_text = q.get("ack", "")

            a = debug.get("A", "?")
            c = debug.get("C", "?")
            amb = debug.get("Amb", "?")
            s = debug.get("S", "?")

            vector_str = f"A{a} C{c} Amb{amb} S{s}"
            
            # Vector-based CSS class mapped to Abstraction Depth
            vec_classes = {0: 'vec-low', 1: 'vec-low', 2: 'vec-mid', 3: 'vec-mid', 4: 'vec-high', 5: 'vec-max'}
            lc = vec_classes.get(a, 'vec-mid') if isinstance(a, int) else 'vec-low'
            lv = f'<span class="vector-badge {lc}">{vector_str}</span>'

            sq = html_module.escape(str(q.get("question", "")))[:500]
            sa = html_module.escape(str(answer))[:500]
            sk = html_module.escape(skills)
            fm = q.get("format", "N/A")
            s_ack = html_module.escape(str(ack_text))[:300]
            
            # Intent badge
            intent_val = q.get("intent", "None")
            intent_colors = {
                "others": ("#1b5e20", "#a5d6a7"),
                "skip": ("#e65100", "#ffcc80"),
                "repeat": ("#0d47a1", "#90caf9"),
                "rephrase": ("#6a1b9a", "#ce93d8"),
                "exit": ("#b71c1c", "#ef9a9a"),
                "None": ("#333", "#888"),
            }
            i_bg, i_fg = intent_colors.get(intent_val.lower() if intent_val else "none", ("#333", "#888"))
            intent_badge = f'<span style="background:{i_bg};color:{i_fg};padding:2px 6px;border-radius:4px;font-size:0.75em;font-weight:600;">{html_module.escape(str(intent_val))}</span>'

            # ── Audit cell ──
            audit = q.get("audit")
            if audit is None:
                audit_cell = '<span class="audit-na">—</span>'
            else:
                passed = audit.get("pass", False)
                reason = html_module.escape(str(audit.get("reason", "")))
                badge_cls = "audit-pass" if passed else "audit-fail"
                badge_text = "✓ PASS" if passed else "✗ FAIL"
                
                # Per-dimension breakdown
                dims = audit.get("dimensions", {})
                dim_parts = []
                for dim_key in ["A", "C", "Amb", "S", "skills"]:
                    dim = dims.get(dim_key, {})
                    dim_passed = dim.get("pass", False)
                    dim_cls = "dim-pass" if dim_passed else "dim-fail"
                    dim_icon = "✓" if dim_passed else "✗"
                    dim_note = html_module.escape(str(dim.get("note", ""))[:60])
                    dim_parts.append(f'<span class="{dim_cls}" title="{dim_note}">{dim_icon}{dim_key}</span>')
                
                dims_html = ' '.join(dim_parts)
                audit_cell = (
                    f'<span class="{badge_cls}" title="{reason}">{badge_text}</span>'
                    f'<div class="audit-dims">{dims_html}</div>'
                )

            # Follow-up / Warmup badge
            followup_badge = ""
            if q.get("is_followup", False):
                chain = q.get("followup_chain", 1)
                followup_badge = f' <span class="badge badge-role" style="background:#e040fb;font-size:0.65em;">🔄 FOLLOWUP {chain}/2</span>'
            elif q.get("is_warmup", False):
                followup_badge = ' <span class="badge badge-role" style="background:#29b6f6;font-size:0.65em;">💬 WARMUP</span>'

            h.append(f'<tr><td>{q["q_num"]}{followup_badge}</td>')
            h.append(f'<td class="q-text">{sq}</td>')
            h.append(f'<td class="a-text">{sa}</td>')
            h.append(f'<td class="a-text" style="color:#ffcc80;font-style:normal;">{s_ack}</td>')
            h.append(f'<td>{intent_badge}</td>')
            h.append(f'<td>{fm}</td>')
            h.append(f'<td>{sk}</td>')
            h.append(f'<td>{lv}</td>')
            h.append(f'<td>{audit_cell}</td></tr>')

        h.append('</table></div>')

    h.append('</body></html>')

    output_file = "interview_simulation_report_v6.html"
    with open(output_file, "w") as f:
        f.write("\n".join(h))
    logger.info(f"📄 Report generated: {output_file}")



# ═══════════════════════════════════════════════════════════════════════════
# MAIN EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def run_progressive_simulation(role: str, difficulty: str, round_num: int) -> dict:
    '''
    Phase 3C+: Progressive simulation for a single role/difficulty/round combo.
    Uses a synthetic interview dict (no DB dependency for the 27-case sweep).
    '''
    global _sim_interview_counter
    _sim_interview_counter += 1
    sim_interview_id = _sim_interview_counter
    sim_candidate_id = sim_interview_id  # Synthetic candidate

    logger.info("=== STARTING SIMULATION: %s/%s/R%d (ID=%d) ===", role, difficulty, round_num, sim_interview_id)

    # Create config for this combo
    # Mirror production: conceptual/resume mix from .env (conceptual 0.5, resume 0.4).
    # Reserve follow-up headroom BEFORE allocating quotas so the budget arbiter
    # can't suppress adaptive follow-ups entirely (ratios are % of the structured
    # questions). FOLLOWUP_RESERVE_PERCENTAGE default 0.2.
    _tech = max(1, QUESTIONS_PER_ROUND - 2)
    _reserve_ratio = max(0.0, min(0.5, float(os.getenv("FOLLOWUP_RESERVE_PERCENTAGE", "0.2"))))
    _followup_reserve = min(_tech - 1, round(_tech * _reserve_ratio))
    _structured = max(1, _tech - _followup_reserve)
    _resume_ratio = float(os.getenv("RESUME_QUESTION_PERCENTAGE", "0.4"))
    _concept_ratio = float(os.getenv("CONCEPTUAL_QUESTION_PERCENTAGE", "0.5"))
    _resume_ct = min(_structured, round(_resume_ratio * _structured))
    _jd_budget = max(0, _structured - _resume_ct)
    _concept_ct = min(_jd_budget, round(_concept_ratio * _structured))
    _scenario_ct = max(0, _jd_budget - _concept_ct)
    logger.info(
        "📋 [sim] Question mix: technical=%d (structured=%d, followup_reserve=%d) → "
        "resume=%d, conceptual=%d, scenario=%d",
        _tech, _structured, _followup_reserve, _resume_ct, _concept_ct, _scenario_ct,
    )
    config = InterviewConfig(
        total_rounds=3,
        questions_per_round=QUESTIONS_PER_ROUND,
        base_difficulty=difficulty,
        role_experience_required=role,
        resume_question_ratio=(_resume_ct / _tech),
        jd_question_count=_concept_ct + _scenario_ct,
        resume_question_count=_resume_ct,
        conceptual_question_count=_concept_ct,
        scenario_question_count=_scenario_ct,
        followup_reserve_count=_followup_reserve,
    )
    context = InterviewContext(config=config)
    context.current_round = round_num

    # Initialize skill states
    req_skills = MANDATORY_SKILLS
    for sk in req_skills.split(", "):
        sk = sk.strip()
        if not sk: continue
        context.skill_states[sk] = RuntimeSkillState(
            vector=compute_difficulty_vector(config.base_difficulty, config.role_experience_required, round_num, total_rounds=3),
            skill=sk,
            role=role,
            difficulty=difficulty,
            round_number=round_num,
        )

    # Create a synthetic interview dict
    interview = {
        'id': sim_interview_id,
        'candidate_id': sim_candidate_id,
        'mandatory_skills': MANDATORY_SKILLS,
        'job_description': JOB_DESCRIPTION,
        'resume_text': RESUME_TEXT,
        'job_id': sim_interview_id,
    }

    res = run_simulation(interview, config, context, sim_candidate_id)
    return res

def main():
    total_cases = len(ROLES) * len(DIFFICULTIES) * len(ROUNDS)
    logger.info(f"🏁 Starting Full Paradigm Sweep: {total_cases} cases ({len(ROLES)} roles × {len(DIFFICULTIES)} difficulties × {len(ROUNDS)} rounds)")

    results = []
    case_num = 0

    for role in ROLES:
        for difficulty in DIFFICULTIES:
            for round_num in ROUNDS:
                case_num += 1
                logger.info(f"━━━ Case {case_num}/{total_cases}: {role}/{difficulty}/R{round_num} ━━━")
                try:
                    res = run_progressive_simulation(role, difficulty, round_num)
                    if res:
                        results.append(res)
                except Exception as e:
                    logger.exception(f"❌ Case {case_num} failed ({role}/{difficulty}/R{round_num}): {e}")

    logger.info("⏳ Waiting for all background evaluations to complete...")
    sim_evaluation_executor.shutdown(wait=True)
    logger.info("✅ All evaluations drained.")

    generate_html_report(results)
    print_generated_questions(results)

    total_qs = sum(len(r["questions"]) for r in results)
    logger.info(f"🎉 Simulation complete: {len(results)} scenarios, {total_qs} Q&As generated + evaluated")


def print_generated_questions(results: List[Dict]):
    """Print each generated question with its difficulty vector + intent so you can
    see HOW questions are generated (natural phrasing, per-skill, skip→easier)."""
    for res in results:
        print("\n" + "═" * 100)
        print(f"  {res['id']}  ({res['role']}/{res['difficulty']}/R{res['round']})")
        print("═" * 100)
        for q in res["questions"]:
            d = q.get("debug", {}) or {}
            vec = f"A{d.get('A','?')} C{d.get('C','?')} Amb{d.get('Amb','?')} S{d.get('S','?')}"
            skills = ", ".join(q["skills"]) if isinstance(q["skills"], list) else str(q["skills"])
            tag = q.get("intent", "-")
            if q.get("is_followup"): tag = f"followup/{tag}"
            if q.get("is_warmup"): tag = "warmup"
            print(f"\n  [Q{q['q_num']}] {vec:<20} skill={skills}  intent={tag}")
            print(f"    Q: {q.get('question','')}")
            ans = (q.get('answer','') or '')[:160].replace('\n',' ')
            print(f"    A: {ans}{'…' if len(q.get('answer','') or '')>160 else ''}")

if __name__ == "__main__":
    main()
