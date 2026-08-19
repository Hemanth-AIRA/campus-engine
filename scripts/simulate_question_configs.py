"""
Interview Question Configuration Simulation & Analysis

Runs lightweight interview simulations using the planner to generate REAL questions
across multiple configuration profiles. No DB, no evaluations — just question generation
and format distribution analysis.

Configurations tested:
  A) Legacy (no explicit counts)
  B) JD=10, Resume=3 (no concept/scenario split)
  C) JD=10, Resume=3, Concept=4, Scenario=6
  D) JD=10, Resume=3, Concept=7, Scenario=3  (concept-heavy)
  E) JD=8,  Resume=5, Concept=3, Scenario=5  (resume-heavy)

Usage:
    python3 simulate_question_configs.py
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import os
import sys
import time
import json
import logging
import random
from datetime import datetime
from typing import List, Dict, Optional

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from openai import OpenAI
from core.responses_client import ResponsesClient, QuestionFormat
from core.interview_config import InterviewConfig, InterviewContext, RuntimeSkillState
from core.difficulty_policy import compute_difficulty_vector

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ═══════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════

from dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    print("❌ ERROR: OPENAI_API_KEY not found.")
    sys.exit(1)

openai_client = OpenAI(api_key=API_KEY)

MANDATORY_SKILLS = "Strong foundation in Linear Algebra and Probability, Hands-on experience with PyTorch for deep learning, Deep understanding of Transformer architectures, Experience fine-tuning LLMs using LoRA or PEFT, Proven experience building RAG systems, Practical experience with LangChain ecosystem, Experience with vector databases such as FAISS or Pinecone, Strong Python programming skills for ML integration"

RESUME_TEXT = """
# HEMANTH REDDY NALABOLU

**AI Researcher & Developer | LLM Systems Architect | Applied ML Engineer**

## PROFESSIONAL EXPERIENCE

### AI Researcher & Developer (2024 – Present)
* Leading research on scalable RAG pipelines and low-latency LLM inference architectures
* Designing multi-agent systems using LangGraph and advanced ReAct-style orchestration
* Conducting experimentation on alignment strategies, hallucination mitigation

### AI Developer — OpenAI (2020 – 2024)
* Contributed to optimization of large-scale Transformer-based language models
* Worked on fine-tuning workflows using LoRA/QLoRA and PEFT
* Designed retrieval pipelines integrating FAISS and semantic chunking strategies

### AI Intern — NVIDIA (2019 – 2020)
* Assisted in research on GPU-accelerated Transformer training optimization
* Benchmarked large-model training performance across CUDA-based architectures

## SELECTED PROJECTS

### Enterprise Medical Intelligence System
* Designed an agentic medical assistant leveraging multi-step reasoning
* Optimized inference latency for real-time diagnostic interaction

### Legal LLM with Domain-Specific Fine-Tuning
* Fine-tuned a 1.3B parameter Falcon model using QLoRA for legal domain
* Reduced hallucination rate by 23% through retrieval-grounded prompting

### Anomaly Transformer for Time-Series Intelligence
* Implemented discrepancy-based attention mechanisms for temporal anomaly detection
* Achieved high F1 performance across unseen SMAP dataset sequences

## TECHNICAL EXPERTISE
Python, PyTorch, HuggingFace Transformers, PEFT, LangChain, LangGraph,
RAG, LoRA/QLoRA, Vector Databases, ReAct Workflows, Model Alignment,
AWS, Docker, Terraform, Jenkins, FAISS, MongoDB, GPU Optimization, CUDA
"""

JOB_DESCRIPTION = "Senior AI Engineer – LLM & Intelligent Systems. Design and implement deep learning models using PyTorch. Develop RAG pipelines, multi-agent workflows, and fine-tune transformer models."

# ═══════════════════════════════════════════════════════════════════════════
# SIMULATION CONFIGS
# ═══════════════════════════════════════════════════════════════════════════

CONFIGS = {
    "H_Pure_Resume": {
        "label": "100% Resume: JD=0, Resume=13",
        "config": InterviewConfig(
            jd_question_count=0,
            resume_question_count=13,
            conceptual_question_count=0,
            scenario_question_count=0,
            role_experience_required="mid",
            base_difficulty="medium",
        ),
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# CANDIDATE SIMULATION
# ═══════════════════════════════════════════════════════════════════════════

def generate_candidate_answer(question: str) -> str:
    """Lightweight candidate simulation — generates a short answer."""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are simulating a mid-level AI engineer interview candidate. "
                        "Answer in 80-120 words. Be substantive and technical. "
                        "Use first person. Reference real tools and frameworks."
                    ),
                },
                {"role": "user", "content": f"Interview question: {question}"},
            ],
            temperature=0.7,
            max_tokens=200,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"I would approach this by analyzing the problem systematically and using relevant frameworks like PyTorch and LangChain."


# ═══════════════════════════════════════════════════════════════════════════
# SIMULATION RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def run_config_simulation(config_key: str, config_data: dict) -> Dict:
    """Run a single interview simulation with the given config."""
    config = config_data["config"]
    label = config_data["label"]
    
    logger.info(f"\n{'='*60}")
    logger.info(f"🚀 STARTING: {config_key} — {label}")
    logger.info(f"{'='*60}")
    config.log_config(prefix=f"[{config_key}] ")
    
    client = ResponsesClient(api_key=API_KEY, interview_config=config)
    skills = MANDATORY_SKILLS.split(", ")
    client.init_skill_tracker(skills)
    
    # Parse resume projects using GPT (mirrors PARALLEL TASK 4 in production)
    client.parse_resume_projects(RESUME_TEXT)
    
    # Build proper skill states with DifficultyVector
    skill_states = {}
    for s in skills:
        vec = compute_difficulty_vector(
            base_difficulty=config.base_difficulty,
            role_experience_required=config.role_experience_required,
            round_number=1,
        )
        skill_states[s] = RuntimeSkillState(
            vector=vec,
            skill=s,
            role=config.role_experience_required,
            difficulty=config.base_difficulty,
            round_number=1,
        )
    
    context = InterviewContext(
        config=config,
        current_round=1,
        skill_states=skill_states,
    )
    
    questions = []
    total_q = config.questions_per_round
    start_time = time.time()
    
    # Q1: Greeting (simulated — no API call)
    greeting = f"Hi, welcome to the interview. I'll be asking you {total_q} questions over the next session."
    answer = "Thank you, I'm excited to be here. I've been working in AI and ML for about 4 years."
    questions.append({
        "q_num": 1,
        "question": greeting,
        "format": "GREETING",
        "skills": ["Communication"],
        "answer": answer,
    })
    
    # Q2: Background (simulated)
    q2 = "Tell me about your background and what you've been working on recently."
    a2 = generate_candidate_answer(q2)
    questions.append({
        "q_num": 2,
        "question": q2,
        "format": "DIRECT_EXPERIENCE",
        "skills": ["Background"],
        "answer": a2,
    })
    
    # Q3+: Planner-generated questions
    last_question = q2
    last_answer = a2
    prev_resp_id = None
    
    for q_num in range(3, total_q + 1):
        try:
            planner_result = client.create_planner_response(
                question_number=q_num - 1,
                user_message=last_answer,
                intent="others",
                previous_response_id=prev_resp_id,
                current_question_text=last_question,
                resume_text=RESUME_TEXT,
                previous_intent="others",
                context=context,
            )
            
            question_text = planner_result.get("next_question", "")
            prev_resp_id = planner_result.get("response_id")
            
            if not question_text:
                logger.warning(f"  ⚠️ Empty question at Q{q_num}")
                break
            
            # Get format from tracker (most reliable source)
            fmt = "unknown"
            if client._next_format:
                fmt = client._next_format.value if hasattr(client._next_format, 'value') else str(client._next_format)
            
            # Get skills
            skill_list = list(client._next_skills) if client._next_skills else []
            
            # Register question in tracker
            if client.skill_tracker:
                client.skill_tracker.register_question(q_num, skill_list, question_text=question_text)
            
            # Generate candidate answer
            answer = generate_candidate_answer(question_text)
            if client.skill_tracker:
                client.skill_tracker.record_answer(q_num, answer)
            
            questions.append({
                "q_num": q_num,
                "question": question_text,
                "format": fmt,
                "skills": skill_list,
                "answer": answer,
            })
            
            last_question = question_text
            last_answer = answer
            
            logger.info(f"  Q{q_num}: [{fmt.upper()}] {question_text[:80]}...")
            
        except Exception as e:
            logger.error(f"  ❌ Q{q_num} failed: {e}")
            questions.append({
                "q_num": q_num,
                "question": f"[ERROR: {str(e)[:100]}]",
                "format": "ERROR",
                "skills": [],
                "answer": "",
            })
    
    elapsed = time.time() - start_time
    
    # Get format distribution
    format_dist = {}
    for q in questions:
        fmt = q["format"]
        format_dist[fmt] = format_dist.get(fmt, 0) + 1
    
    result = {
        "config_key": config_key,
        "label": label,
        "questions": questions,
        "format_distribution": format_dist,
        "total_questions": len(questions),
        "elapsed_seconds": round(elapsed, 1),
        "config_details": {
            "questions_per_round": config.questions_per_round,
            "jd_question_count": config.jd_question_count,
            "resume_question_count": config.resume_question_count,
            "conceptual_question_count": config.conceptual_question_count,
            "scenario_question_count": config.scenario_question_count,
            "resume_ratio": config.resume_question_ratio,
            "role": config.role_experience_required,
        },
    }
    
    logger.info(f"✅ {config_key}: {len(questions)} questions in {elapsed:.1f}s")
    logger.info(f"   Distribution: {format_dist}")
    
    return result


# ═══════════════════════════════════════════════════════════════════════════
# OUTPUT GENERATION
# ═══════════════════════════════════════════════════════════════════════════

def write_results_txt(all_results: List[Dict], output_path: str):
    """Write all questions and analysis to a text file."""
    
    # Classify formats
    CONCEPT_FORMATS = {"conceptual"}
    SCENARIO_FORMATS = {"scenario_based", "troubleshooting", "experimental", 
                         "implementation_walkthrough", "verbal_architecture", "comparison"}
    RESUME_FORMATS = {"resume_project"}
    OTHER_FORMATS = {"direct_experience", "GREETING", "DIRECT_EXPERIENCE"}
    
    with open(output_path, "w") as f:
        f.write("=" * 80 + "\n")
        f.write("  INTERVIEW QUESTION CONFIGURATION — SIMULATION RESULTS\n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("=" * 80 + "\n\n")
        
        # ── Per-config question listings ──
        for result in all_results:
            f.write("\n" + "━" * 80 + "\n")
            f.write(f"  CONFIG: {result['config_key']}\n")
            f.write(f"  {result['label']}\n")
            f.write("━" * 80 + "\n\n")
            
            details = result["config_details"]
            f.write(f"  Settings:\n")
            f.write(f"    Total Questions:      {details['questions_per_round']}\n")
            f.write(f"    JD Questions:         {details['jd_question_count'] or 'auto'}\n")
            f.write(f"    Resume Questions:     {details['resume_question_count'] or 'auto'}\n")
            f.write(f"    Conceptual Questions: {details['conceptual_question_count'] or 'N/A'}\n")
            f.write(f"    Scenario Questions:   {details['scenario_question_count'] or 'N/A'}\n")
            f.write(f"    Resume Ratio:         {details['resume_ratio']:.0%}\n")
            f.write(f"    Role:                 {details['role']}\n")
            f.write(f"    Generation Time:      {result['elapsed_seconds']}s\n\n")
            
            for q in result["questions"]:
                fmt_tag = q["format"].upper().replace("_", " ")
                skills_str = ", ".join(q["skills"]) if q["skills"] else "N/A"
                f.write(f"  Q{q['q_num']} [{fmt_tag}]\n")
                f.write(f"  Skills: {skills_str}\n")
                f.write(f"  Question: {q['question']}\n")
                if q.get("answer"):
                    answer_preview = q["answer"][:200] + ("..." if len(q["answer"]) > 200 else "")
                    f.write(f"  Answer: {answer_preview}\n")
                f.write("\n")
            
            # Format distribution for this config
            f.write(f"  FORMAT DISTRIBUTION:\n")
            for fmt, count in sorted(result["format_distribution"].items()):
                bar = "█" * count
                f.write(f"    {fmt:<30} {count:>2}  {bar}\n")
            f.write("\n")
        
        # ── COMPARATIVE ANALYSIS ──
        f.write("\n" + "=" * 80 + "\n")
        f.write("  COMPARATIVE ANALYSIS\n")
        f.write("=" * 80 + "\n\n")
        
        # Header
        f.write(f"  {'Config':<25} {'Total':>5} {'Concept':>8} {'Scenario':>9} {'Resume':>7} {'Direct':>7} {'Other':>6}\n")
        f.write(f"  {'-'*25} {'-'*5} {'-'*8} {'-'*9} {'-'*7} {'-'*7} {'-'*6}\n")
        
        for result in all_results:
            dist = result["format_distribution"]
            concept_count = sum(dist.get(f, 0) for f in CONCEPT_FORMATS)
            scenario_count = sum(dist.get(f, 0) for f in SCENARIO_FORMATS)
            resume_count = sum(dist.get(f, 0) for f in RESUME_FORMATS)
            direct_count = sum(dist.get(f, 0) for f in OTHER_FORMATS)
            other_count = result["total_questions"] - concept_count - scenario_count - resume_count - direct_count
            
            f.write(f"  {result['config_key']:<25} {result['total_questions']:>5} {concept_count:>8} {scenario_count:>9} {resume_count:>7} {direct_count:>7} {other_count:>6}\n")
        
        f.write("\n")
        
        # ── Target vs Actual comparison ──
        f.write(f"\n  TARGET vs ACTUAL:\n")
        f.write(f"  {'-'*70}\n")
        f.write(f"  {'Config':<25} {'Concept Target':>15} {'Concept Actual':>15} {'Match':>6}\n")
        f.write(f"  {'-'*25} {'-'*15} {'-'*15} {'-'*6}\n")
        
        for result in all_results:
            dist = result["format_distribution"]
            concept_target = result["config_details"]["conceptual_question_count"]
            scenario_target = result["config_details"]["scenario_question_count"]
            concept_actual = sum(dist.get(f, 0) for f in CONCEPT_FORMATS)
            scenario_actual = sum(dist.get(f, 0) for f in SCENARIO_FORMATS)
            
            concept_str = str(concept_target) if concept_target is not None else "N/A"
            scenario_str = str(scenario_target) if scenario_target is not None else "N/A"
            
            match = "✅" if (concept_target is None or concept_actual >= concept_target * 0.7) else "❌"
            f.write(f"  {result['config_key']:<25} {concept_str:>15} {concept_actual:>15} {match:>6}\n")
        
        f.write(f"\n  {'Config':<25} {'Scenario Target':>15} {'Scenario Actual':>15} {'Match':>6}\n")
        f.write(f"  {'-'*25} {'-'*15} {'-'*15} {'-'*6}\n")
        
        for result in all_results:
            dist = result["format_distribution"]
            scenario_target = result["config_details"]["scenario_question_count"]
            scenario_actual = sum(dist.get(f, 0) for f in SCENARIO_FORMATS)
            
            scenario_str = str(scenario_target) if scenario_target is not None else "N/A"
            match = "✅" if (scenario_target is None or scenario_actual >= scenario_target * 0.7) else "❌"
            f.write(f"  {result['config_key']:<25} {scenario_str:>15} {scenario_actual:>15} {match:>6}\n")
        
        # ── Detailed format breakdown ──
        f.write(f"\n\n  DETAILED FORMAT BREAKDOWN:\n")
        f.write(f"  {'-'*80}\n")
        
        all_formats = set()
        for r in all_results:
            all_formats.update(r["format_distribution"].keys())
        all_formats = sorted(all_formats)
        
        header = f"  {'Format':<30}"
        for r in all_results:
            header += f" {r['config_key'][:12]:>12}"
        f.write(header + "\n")
        f.write(f"  {'-'*30}" + f" {'-'*12}" * len(all_results) + "\n")
        
        for fmt in all_formats:
            row = f"  {fmt:<30}"
            for r in all_results:
                count = r["format_distribution"].get(fmt, 0)
                row += f" {count:>12}"
            f.write(row + "\n")
        
        # Totals
        row = f"  {'TOTAL':<30}"
        for r in all_results:
            row += f" {r['total_questions']:>12}"
        f.write(f"  {'-'*30}" + f" {'-'*12}" * len(all_results) + "\n")
        f.write(row + "\n")
        
        f.write("\n" + "=" * 80 + "\n")
        f.write("  END OF REPORT\n")
        f.write("=" * 80 + "\n")
    
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "🧪 " * 20)
    print("  QUESTION CONFIG SIMULATION — MULTI-CONFIG")
    print("🧪 " * 20 + "\n")
    
    all_results = []
    total_start = time.time()
    
    for config_key, config_data in CONFIGS.items():
        print(f"\n🔄 Running: {config_key} — {config_data['label']}...")
        try:
            result = run_config_simulation(config_key, config_data)
            all_results.append(result)
            print(f"   ✅ Done: {result['total_questions']} questions in {result['elapsed_seconds']}s")
            print(f"   📊 Distribution: {result['format_distribution']}")
        except Exception as e:
            print(f"   ❌ Failed: {e}")
            import traceback
            traceback.print_exc()
    
    total_elapsed = time.time() - total_start
    
    # Write output
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "simulation_question_analysis.txt"
    )
    write_results_txt(all_results, output_path)
    print(f"\n📄 Results written to: {output_path}")
    print(f"⏱️  Total time: {total_elapsed:.1f}s")
    
    # Quick console summary
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  {'Config':<25} {'Questions':>10} {'Concept':>8} {'Scenario':>9} {'Resume':>7}")
    print(f"  {'-'*25} {'-'*10} {'-'*8} {'-'*9} {'-'*7}")
    for r in all_results:
        dist = r["format_distribution"]
        concept = dist.get("conceptual", 0)
        scenario = sum(dist.get(f, 0) for f in ["scenario_based", "troubleshooting", "experimental", 
                                                   "implementation_walkthrough", "verbal_architecture", "comparison"])
        resume = dist.get("resume_project", 0)
        print(f"  {r['config_key']:<25} {r['total_questions']:>10} {concept:>8} {scenario:>9} {resume:>7}")
    print(f"{'='*70}\n")
