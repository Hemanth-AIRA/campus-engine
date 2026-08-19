"""
Simulate HR Interview Round — End-to-end test of the HR engine.

Runs the full pipeline:
  1. HRInterviewEngine generates questions (randomized topics)
  2. GPT simulates candidate responses (realistic, with speech patterns)
  3. Short-answer follow-up probes fire when needed
  4. HREvaluator scores the transcript against all 8 default dimensions
  5. Outputs a formatted scorecard

Usage:
    python simulate_hr_interview.py
    python simulate_hr_interview.py --candidate "senior engineer" --job "Backend Developer"
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import argparse
import json
import logging
import os
import random
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI

from hr_interview_config import HRInterviewConfig
from hr_interview_engine import HRInterviewEngine, QUESTION_BANK
from hr_evaluator import evaluate_hr_interview, compute_hr_score

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("SimulateHR")


# ═══════════════════════════════════════════════════════════════════════════
# Default Dimensions (mirrors DB seed data)
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_DIMENSIONS = [
    {"dimension_key": "communication_clarity", "dimension_name": "Communication Clarity",
     "description": "Ability to articulate thoughts clearly and adapt communication style."},
    {"dimension_key": "ownership_mindset", "dimension_name": "Ownership Mindset",
     "description": "Takes responsibility for outcomes and proactively addresses problems."},
    {"dimension_key": "conflict_handling", "dimension_name": "Conflict Handling",
     "description": "Manages disagreements constructively and maintains professional relationships."},
    {"dimension_key": "career_motivation", "dimension_name": "Career Motivation",
     "description": "Shows clear career direction and genuine interest in the role."},
    {"dimension_key": "team_collaboration", "dimension_name": "Team Collaboration",
     "description": "Works effectively with others and values diverse perspectives."},
    {"dimension_key": "adaptability", "dimension_name": "Adaptability",
     "description": "Handles change well and remains productive under uncertainty."},
    {"dimension_key": "integrity_signals", "dimension_name": "Integrity Signals",
     "description": "Demonstrates honesty and consistency between words and actions."},
    {"dimension_key": "culture_alignment", "dimension_name": "Culture Alignment",
     "description": "Values and work style align with the organization environment."},
]


# ═══════════════════════════════════════════════════════════════════════════
# Simulated Candidate — Realistic, messy, human behavior
# ═══════════════════════════════════════════════════════════════════════════

CANDIDATE_SYSTEM_PROMPT = """You are simulating a REAL job candidate in a behavioral HR interview.

CANDIDATE PROFILE:
{profile}

YOUR BEHAVIOR PATTERN FOR THIS ANSWER: {behavior}

CORE RULES:
- You are a REAL person, not an AI. Talk like a human in a slightly nervous interview.
- Use filler words naturally: "um", "uh", "like", "you know", "so basically", "I mean"
- Use first person only ("I", "we", "my team")
- NEVER sound polished or rehearsed. Sound like someone thinking on their feet.
- Match the behavior pattern given above EXACTLY.
"""

# Different behavior modes to cycle through — simulates a real candidate
CANDIDATE_BEHAVIORS = [
    # Normal solid answer
    "Give a decent, specific answer (40-60 words). Include a real example from your past work. Sound natural, not rehearsed.",

    # Very short / vague
    "Give a SHORT, vague answer (5-12 words). Like you're unsure or thinking. Example: 'Yeah, I mean... I think just working hard, you know?' or 'Hmm, not really sure how to answer that honestly.'",

    # Counter-question / deflection
    "Instead of directly answering, ask a clarifying question back. Like: 'That's a good question, um, do you mean like within my team or more broadly?' or 'Hmm, what exactly do you mean by that? Like in terms of day-to-day?'",

    # Rambling / off-topic then comes back
    "Start by going slightly off-topic or rambling (maybe mention something personal like your dog or weekend plans), then circle back to a partial answer. Total 50-80 words. Sound scattered but genuine.",

    # Strong detailed answer
    "Give a STRONG, detailed answer (60-90 words) with specific names, timelines, outcomes. This is your best answer — confident, clear, with a concrete story.",

    # Honest but negative/self-aware
    "Be honest about a weakness or struggle. Don't spin it positively. Just be real. Like: 'Honestly, I didn't handle that great. I kind of shut down when things got heated...' (30-50 words)",

    # Short then trails off
    "Start with a brief answer then trail off like you're thinking. Example: 'Yeah, so there was this one time at my last job... actually, hmm, let me think about a better example...' (15-25 words)",

    # Ask to repeat the question
    "You didn't catch the question properly. Ask them to repeat it. Like: 'Sorry, could you say that again? I kind of lost you after the first part.' or 'Wait, sorry — can you repeat that? My mind went blank for a second.' (8-15 words ONLY)",

    # Distracted mid-answer / interrupted by something
    "Start answering normally for 20 words, then get distracted. Like: 'Yeah so at my last company we were working on this big... sorry, I just got distracted for a second, where was I? Oh right, so we were...' then finish loosely (40-60 words total).",

    # Completely stalls / needs a moment
    "Stall and ask for time to think. Like: 'Hmm, that's a tough one actually. Give me a second to think about that...' or 'Oh wow, okay, um... let me think... I'm trying to remember a specific example.' (10-20 words ONLY)",

    # Starts answering wrong question, then self-corrects
    "Start answering a DIFFERENT question than what was asked (like: 'Oh yeah, so in terms of my technical skills...'). Then catch yourself: 'Wait, sorry, that's not what you asked. Let me try again.' Then give a brief answer to the actual question. (40-60 words total)",

    # Emotional / personal moment
    "Get a little emotional or personal. Like: 'You know, honestly, this one kind of hits close to home for me. I went through a really tough time at my last job where...' Share something genuine and slightly vulnerable. (40-60 words)",
]


def simulate_candidate_response(
    client: OpenAI,
    question: str,
    profile: str,
    conversation_history: list,
) -> str:
    """Use GPT to generate a realistic, varied candidate response."""

    # Pick a random behavior for this answer
    behavior = random.choice(CANDIDATE_BEHAVIORS)

    messages = [
        {"role": "system", "content": CANDIDATE_SYSTEM_PROMPT.format(
            profile=profile, behavior=behavior
        )},
    ]

    # Add conversation history for context
    for entry in conversation_history[-4:]:
        messages.append({"role": "assistant", "content": entry["question"]})
        messages.append({"role": "user", "content": entry["answer"]})

    messages.append({"role": "assistant", "content": question})

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=1.0,  # Higher temp = more varied responses
        max_tokens=150,
    )

    return response.choices[0].message.content.strip()



# ═══════════════════════════════════════════════════════════════════════════
# Main Simulation
# ═══════════════════════════════════════════════════════════════════════════

def run_simulation(
    candidate_profile: str = "Mid-level software engineer with 4 years experience at a startup. Worked on backend APIs, some team lead experience. Generally competent but not exceptional.",
    job_title: str = "Senior Backend Developer",
    company_name: str = "TechCorp",
    verbose: bool = True,
):
    """Run a full HR interview simulation."""
    client = OpenAI()

    print("\n" + "=" * 70)
    print("🎭 HR INTERVIEW SIMULATION")
    print("=" * 70)
    print(f"  Candidate: {candidate_profile[:60]}...")
    print(f"  Role:      {job_title} at {company_name}")
    print("=" * 70 + "\n")

    # --- Init engine (with reflection topic) ---
    topics = ["intro", "ownership", "conflict", "teamwork", "motivation", "adaptability", "reflection"]
    config = HRInterviewConfig(max_duration_minutes=20, min_responses=6, topics=topics)
    config.log_config("SIMULATION ")
    engine = HRInterviewEngine(interview_id=99999, config=config)

    conversation_history = []
    question_number = 0

    # --- Conversation loop (Phase 1 + Phase 2) ---
    while not engine.is_complete():
        topic = engine.select_next_topic()

        if topic == "closing":
            # Closing question
            question = engine.generate_question("closing")
            question_number += 1
            if verbose:
                print(f"\n{'─' * 60}")
                print(f"  Q{question_number} [CLOSING]")
                print(f"  🤖 Interviewer: {question}")

            answer = simulate_candidate_response(
                client, question, candidate_profile, conversation_history
            )
            if verbose:
                print(f"  👤 Candidate:   {answer}")

            engine.record_answer("closing", question, answer)
            conversation_history.append({"question": question, "answer": answer})
            break

        # Phase 1: banked question | Phase 2: LLM exploration question
        question = engine.generate_question(topic)

        if question is None:
            # Phase 2 signal — generate a curious follow-up
            question = engine.generate_exploration_question(topic, llm_client=client)
            entry_type = "exploration"
            phase_label = "🔍 EXPLORE"
        else:
            entry_type = "primary"
            phase_label = topic.upper()

        question_number += 1
        if verbose:
            print(f"\n{'─' * 60}")
            print(f"  Q{question_number} [{phase_label}] {'(Phase 2)' if entry_type == 'exploration' else ''}")
            print(f"  🤖 Interviewer: {question}")

        # Simulate candidate response
        answer = simulate_candidate_response(
            client, question, candidate_profile, conversation_history
        )

        if verbose:
            print(f"  👤 Candidate:   {answer}")

        # Record and check result: 'repeat', 'probe', or 'none'
        result = engine.record_answer(topic, question, answer, entry_type=entry_type)
        conversation_history.append({"question": question, "answer": answer})

        if result == "repeat":
            # Candidate asked to repeat — re-ask the same question
            repeat_msg = engine.get_repeat_response(question)
            if verbose:
                print(f"  🔄 Repeating:   {repeat_msg}")

            repeat_answer = simulate_candidate_response(
                client, repeat_msg, candidate_profile, conversation_history
            )
            if verbose:
                print(f"  👤 Candidate:   {repeat_answer}")

            # Record the repeat answer (this time it should be a real answer)
            engine.record_answer(topic, repeat_msg, repeat_answer, entry_type=entry_type)
            conversation_history.append({"question": repeat_msg, "answer": repeat_answer})

        elif result == "probe":
            probe = engine.get_followup_probe()
            if verbose:
                print(f"  🤖 Follow-up:   {probe}")

            followup_answer = simulate_candidate_response(
                client, probe, candidate_profile, conversation_history
            )
            if verbose:
                print(f"  👤 Candidate:   {followup_answer}")

            engine.record_followup(topic, probe, followup_answer)
            conversation_history.append({"question": probe, "answer": followup_answer})

    # --- Completion status ---
    status = engine.get_completion_status()
    print(f"\n{'=' * 70}")
    print("📋 INTERVIEW COMPLETE")
    print(f"  Phase:     {status.get('phase', '?')}")
    print(f"  Status:    {status['status']}")
    print(f"  Topics:    {status['topics_covered']}/{status['topics_total']} covered")
    print(f"  Explorations: {status.get('explorations', 0)}")
    print(f"  Responses: {status['responses']}")
    print(f"  Duration:  {status['duration_minutes']}m")
    print(f"  Coverage: {json.dumps(status['coverage'], indent=2)}")
    print("=" * 70)

    # --- Run evaluation ---
    print("\n🔍 Running behavioral evaluation...\n")
    transcript_text = engine.get_transcript_text()

    evaluation = evaluate_hr_interview(
        transcript_text=transcript_text,
        dimensions=DEFAULT_DIMENSIONS,
        job_title=job_title,
        company_name=company_name,
    )

    if not evaluation:
        print("❌ Evaluation failed!")
        return

    # --- Print scorecard ---
    hr_score = compute_hr_score(evaluation)

    print("=" * 70)
    print("📊 HR EVALUATION SCORECARD")
    print("=" * 70)
    print()

    for score_entry in evaluation.get("scores", []):
        score = score_entry.get("score", 0)
        bar = "█" * score + "░" * (10 - score)
        color = "🟢" if score >= 7 else "🟡" if score >= 5 else "🔴"
        print(f"  {color} {score_entry.get('dimension_name', '?'):<25} [{bar}] {score}/10")
        print(f"     {score_entry.get('justification', '')}")
        print()

    print(f"{'─' * 70}")
    print(f"  📈 Overall HR Score: {hr_score}/10")
    print(f"{'─' * 70}")

    print(f"\n  📝 Summary: {evaluation.get('summary', 'N/A')}")

    strengths = evaluation.get("strengths", [])
    if strengths:
        print(f"\n  ✅ Strengths:")
        for s in strengths:
            print(f"     • {s}")

    concerns = evaluation.get("concerns", [])
    if concerns:
        print(f"\n  ⚠️ Concerns:")
        for c in concerns:
            print(f"     • {c}")

    print(f"\n{'=' * 70}")

    # --- Save HTML report ---
    html_file = generate_html_report(
        candidate_profile=candidate_profile,
        job_title=job_title,
        company_name=company_name,
        status=status,
        transcript=engine.get_transcript(),
        evaluation=evaluation,
        hr_score=hr_score,
    )
    print(f"\n💾 HTML report saved to: {html_file}")
    return evaluation


# ═══════════════════════════════════════════════════════════════════════════
# HTML Report Generator
# ═══════════════════════════════════════════════════════════════════════════

TOPIC_COLORS = {
    "intro": "#6366f1",
    "ownership": "#f59e0b",
    "conflict": "#ef4444",
    "teamwork": "#10b981",
    "motivation": "#8b5cf6",
    "adaptability": "#06b6d4",
    "closing": "#6b7280",
}


def _score_color(score: int) -> str:
    if score >= 7:
        return "#10b981"
    elif score >= 5:
        return "#f59e0b"
    return "#ef4444"


def _score_label(score: int) -> str:
    if score >= 8:
        return "Excellent"
    elif score >= 7:
        return "Strong"
    elif score >= 5:
        return "Adequate"
    elif score >= 3:
        return "Weak"
    return "Poor"


def generate_html_report(
    candidate_profile: str,
    job_title: str,
    company_name: str,
    status: dict,
    transcript: list,
    evaluation: dict,
    hr_score: float,
) -> str:
    """Generate a styled HTML report and save it."""
    from datetime import datetime

    scores_html = ""
    for s in evaluation.get("scores", []):
        score = s.get("score", 0)
        color = _score_color(score)
        pct = score * 10
        scores_html += f"""
        <div class="dim-row">
            <div class="dim-name">{s.get('dimension_name', '?')}</div>
            <div class="dim-bar-wrap">
                <div class="dim-bar" style="width:{pct}%;background:{color}"></div>
            </div>
            <div class="dim-score" style="color:{color}">{score}/10</div>
            <div class="dim-label" style="color:{color}">{_score_label(score)}</div>
        </div>
        <div class="dim-justification">{s.get('justification', '')}</div>
        """

    transcript_html = ""
    q_num = 0
    for entry in transcript:
        topic = entry.get("topic", "")
        entry_type = entry.get("type", "primary")
        color = TOPIC_COLORS.get(topic, "#6b7280")

        if entry_type == "primary":
            q_num += 1
            transcript_html += f"""
            <div class="conv-block">
                <div class="conv-topic" style="background:{color}">{topic.upper()}</div>
                <div class="conv-q">
                    <span class="conv-label">🤖 Q{q_num}</span>
                    {entry.get('question', '')}
                </div>
                <div class="conv-a">
                    <span class="conv-label">👤</span>
                    {entry.get('answer', '')}
                </div>
            </div>"""
        else:
            transcript_html += f"""
            <div class="conv-block conv-followup">
                <div class="conv-q">
                    <span class="conv-label">🤖 Follow-up</span>
                    {entry.get('question', '')}
                </div>
                <div class="conv-a">
                    <span class="conv-label">👤</span>
                    {entry.get('answer', '')}
                </div>
            </div>"""

    strengths_html = "".join(
        f"<li>{s}</li>" for s in evaluation.get("strengths", [])
    )
    concerns_html = "".join(
        f"<li>{c}</li>" for c in evaluation.get("concerns", [])
    )

    overall_color = _score_color(int(hr_score))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AIRA HR Interview Report</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:'Inter',sans-serif; background:#0f172a; color:#e2e8f0; line-height:1.6; }}

.container {{ max-width:900px; margin:0 auto; padding:40px 24px; }}

/* Header */
.header {{ text-align:center; margin-bottom:48px; }}
.header h1 {{ font-size:28px; font-weight:700; background:linear-gradient(135deg,#6366f1,#8b5cf6); -webkit-background-clip:text; -webkit-text-fill-color:transparent; margin-bottom:8px; }}
.header .subtitle {{ color:#94a3b8; font-size:14px; }}
.meta-grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-top:24px; }}
.meta-card {{ background:#1e293b; border-radius:12px; padding:16px; text-align:center; }}
.meta-card .label {{ font-size:11px; text-transform:uppercase; letter-spacing:1px; color:#64748b; margin-bottom:4px; }}
.meta-card .value {{ font-size:16px; font-weight:600; color:#f1f5f9; }}

/* Overall Score */
.overall {{ text-align:center; margin:40px 0; padding:32px; background:linear-gradient(135deg,#1e293b,#0f172a); border:1px solid #334155; border-radius:16px; }}
.overall-score {{ font-size:64px; font-weight:700; }}
.overall-label {{ font-size:14px; color:#94a3b8; margin-top:4px; }}
.overall-summary {{ margin-top:16px; color:#cbd5e1; font-size:14px; max-width:600px; margin-left:auto; margin-right:auto; }}

/* Scorecard */
.section {{ margin:40px 0; }}
.section-title {{ font-size:18px; font-weight:600; margin-bottom:20px; padding-bottom:8px; border-bottom:1px solid #334155; }}
.dim-row {{ display:grid; grid-template-columns:180px 1fr 60px 80px; align-items:center; gap:12px; padding:8px 0; }}
.dim-name {{ font-size:13px; font-weight:500; }}
.dim-bar-wrap {{ height:8px; background:#1e293b; border-radius:4px; overflow:hidden; }}
.dim-bar {{ height:100%; border-radius:4px; transition:width 0.5s ease; }}
.dim-score {{ font-size:14px; font-weight:600; text-align:right; }}
.dim-label {{ font-size:11px; text-transform:uppercase; letter-spacing:0.5px; text-align:right; }}
.dim-justification {{ font-size:12px; color:#94a3b8; padding:2px 0 12px 0; border-bottom:1px solid #1e293b; margin-bottom:8px; }}

/* Strengths / Concerns */
.two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
.insights-card {{ background:#1e293b; border-radius:12px; padding:20px; }}
.insights-card h3 {{ font-size:14px; font-weight:600; margin-bottom:12px; }}
.insights-card ul {{ list-style:none; }}
.insights-card li {{ font-size:13px; padding:6px 0; padding-left:16px; position:relative; color:#cbd5e1; }}
.insights-card li::before {{ content:''; position:absolute; left:0; top:12px; width:6px; height:6px; border-radius:50%; }}
.strengths li::before {{ background:#10b981; }}
.concerns li::before {{ background:#f59e0b; }}

/* Transcript */
.conv-block {{ background:#1e293b; border-radius:12px; padding:16px; margin-bottom:12px; border-left:3px solid #334155; }}
.conv-followup {{ margin-left:24px; opacity:0.85; border-left-style:dashed; }}
.conv-topic {{ display:inline-block; font-size:10px; font-weight:600; letter-spacing:1px; padding:2px 8px; border-radius:4px; color:white; margin-bottom:8px; }}
.conv-q {{ font-size:13px; color:#94a3b8; margin-bottom:8px; }}
.conv-a {{ font-size:14px; color:#e2e8f0; }}
.conv-label {{ font-weight:600; margin-right:6px; }}

/* Status */
.status-bar {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:32px; }}
.status-item {{ background:#1e293b; border-radius:8px; padding:12px; text-align:center; }}
.status-item .val {{ font-size:20px; font-weight:700; color:#f1f5f9; }}
.status-item .lbl {{ font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; }}

.footer {{ text-align:center; margin-top:48px; padding-top:24px; border-top:1px solid #1e293b; font-size:12px; color:#475569; }}
</style>
</head>
<body>
<div class="container">

    <div class="header">
        <h1>AIRA — HR Interview Report</h1>
        <p class="subtitle">Behavioral Interview Analysis • {now}</p>
        <div class="meta-grid">
            <div class="meta-card">
                <div class="label">Role</div>
                <div class="value">{job_title}</div>
            </div>
            <div class="meta-card">
                <div class="label">Company</div>
                <div class="value">{company_name}</div>
            </div>
            <div class="meta-card">
                <div class="label">Candidate Profile</div>
                <div class="value" style="font-size:12px">{candidate_profile[:80]}</div>
            </div>
        </div>
    </div>

    <div class="status-bar">
        <div class="status-item">
            <div class="val">{status.get('topics_covered',0)}/{status.get('topics_total',0)}</div>
            <div class="lbl">Topics Covered</div>
        </div>
        <div class="status-item">
            <div class="val">{status.get('responses',0)}</div>
            <div class="lbl">Responses</div>
        </div>
        <div class="status-item">
            <div class="val">{status.get('duration_minutes',0)}m</div>
            <div class="lbl">Duration</div>
        </div>
        <div class="status-item">
            <div class="val" style="color:{'#10b981' if status.get('status')=='complete' else '#f59e0b'}">{status.get('status','?').upper()}</div>
            <div class="lbl">Status</div>
        </div>
    </div>

    <div class="overall">
        <div class="overall-score" style="color:{overall_color}">{hr_score}/10</div>
        <div class="overall-label">Overall HR Score</div>
        <div class="overall-summary">{evaluation.get('summary', '')}</div>
    </div>

    <div class="section">
        <div class="section-title">📊 Dimension Scorecard</div>
        {scores_html}
    </div>

    <div class="section">
        <div class="two-col">
            <div class="insights-card strengths">
                <h3>✅ Strengths</h3>
                <ul>{strengths_html or '<li>No strengths identified</li>'}</ul>
            </div>
            <div class="insights-card concerns">
                <h3>⚠️ Concerns</h3>
                <ul>{concerns_html or '<li>No concerns identified</li>'}</ul>
            </div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">💬 Interview Transcript</div>
        {transcript_html}
    </div>

    <div class="footer">
        Generated by AIRA HR Interview Engine • Simulation Mode • {now}
    </div>

</div>
</body>
</html>"""

    filename = f"hr_interview_report_{int(time.time())}.html"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html)

    return filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate an HR interview round")
    parser.add_argument(
        "--candidate",
        default="Mid-level software engineer with 4 years experience at a startup. Worked on backend APIs, some team lead experience.",
        help="Candidate profile description",
    )
    parser.add_argument("--job", default="Senior Backend Developer", help="Job title")
    parser.add_argument("--company", default="TechCorp", help="Company name")
    parser.add_argument("--quiet", action="store_true", help="Less verbose output")

    args = parser.parse_args()

    run_simulation(
        candidate_profile=args.candidate,
        job_title=args.job,
        company_name=args.company,
        verbose=not args.quiet,
    )
