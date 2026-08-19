import os
import re
import json
import time
import random
import logging
from typing import List, Dict, Tuple, Set, Optional
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from answer_analysis_integration import (
    trigger_answer_analysis_async,
    map_question_format_to_type
)
from dotenv import load_dotenv
from openai import OpenAI
from config_loader import NUMBER_OF_QUESTIONS
from core.difficulty_policy import compute_difficulty_vector, compile_vector_prompt, compile_followup_prompt, get_escalation_debug_info

# Load .env
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "800"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.3"))
SERVICE_TIER = os.getenv("SERVICE_TIER", "default")
RESUME_QUESTION_PERCENTAGE = float(os.getenv("RESUME_QUESTION_PERCENTAGE", "0.4"))

logger = logging.getLogger('ResponsesClient')

# ── split modules (re-exported for compatibility) ──────────────────────────
from core.question_format import (QuestionFormat, _SCENARIO_FAMILY, DIFFICULTY_STRATEGIES,
                                  QuestionFormatTracker, get_resume_slots, _even_count_slots,
                                  _det_frac, _det_pick)
from core.skill_tracker import generate_skill_groups, SkillTracker, decide_skill_count


# =============================================================================
# QUESTION FORMAT TRACKER - AUDIO INTERVIEW COMPATIBLE
# =============================================================================

import math



# Pre-calculate resume slots at module load
RESUME_SLOTS = get_resume_slots()
logger.info(f"📋 Resume question slots: {RESUME_SLOTS} ({len(RESUME_SLOTS)} of {NUMBER_OF_QUESTIONS - 2} technical Qs, {RESUME_QUESTION_PERCENTAGE*100:.0f}%)")




# ── CAMPUS DETERMINISM HELPERS ──────────────────────────────────────────────
# Replace RNG in selection paths with reproducible, low-discrepancy choices keyed
# by the question number, so the same (config, answers) yields the same sequence.






# ── CAMPUS DETERMINISM HELPERS ──────────────────────────────────────────────
# Replace RNG in selection paths with reproducible, low-discrepancy choices keyed
# by the question number, so the same (config, answers) yields the same sequence.






# ── CAMPUS DETERMINISM HELPERS ──────────────────────────────────────────────
# Replace RNG in selection paths with reproducible, low-discrepancy choices keyed
# by the question number, so the same (config, answers) yields the same sequence.








# The "scenario-shaped" formats — every non-conceptual, non-resume, non-direct
# format that counts against a scenario quota. Centralized so the budget arbiter
# and the format scheduler agree on what "scenario" means.




# =============================================================================
# SKILL GROUPING - LLM-BASED AUTO-GROUPING
# =============================================================================



# =============================================================================
# SKILL TRACKER - WITH BLACKLIST FEATURE AND GROUPING
# =============================================================================



CRITICAL_REMINDERS = """
🚨🚨🚨 MOST CRITICAL RULE - READ FIRST 🚨🚨🚨

{skill_tracking_context}

🎙️ AUDIO INTERVIEW CONSTRAINTS 🎙️
═══════════════════════════════════════════════════════════════════════════════
NEVER ask candidates to:
❌ Design something visually
❌ Draw diagrams or architectures  
❌ Write code
❌ Sketch anything
❌ Show something on screen

ALWAYS phrase questions naturally for conversation:
✅ "How would you..." instead of "Design..."
✅ "Walk me through..." 
✅ "Can you describe..."
✅ "What's your approach..."
✅ "Tell me about..."

❌ DON'T add redundant phrases like:
- "just explain verbally"
- "verbally describe" 
- "talk me through verbally"
- "just explain in words"

✅ It's an AUDIO interview - verbal responses are IMPLIED!
Keep questions natural and conversational without over-explaining the format.
═══════════════════════════════════════════════════════════════════════════════

🚨 QUESTION DEDUPLICATION - CRITICAL! 🚨
═══════════════════════════════════════════════════════════════════════════════
NEVER ASK THE SAME OR SIMILAR QUESTION TWICE!

ALL QUESTIONS YOU'VE ASKED IN THIS INTERVIEW:
{asked_questions_summary}

Your NEXT question MUST be:
✅ About a DIFFERENT scenario/context
✅ Testing a DIFFERENT aspect of the skill
✅ Using a DIFFERENT question format/angle
✅ Substantially different wording

❌ DON'T just rephrase the same question with different words!
❌ DON'T ask about the same scenario/system/problem again!
❌ DON'T ask something that's already been covered above!

BEFORE generating next_question, CHECK THE FULL LIST ABOVE:
- "Have I already asked about this scenario?"
- "Is this testing the same thing in a different way?"
- "Would the candidate notice this is basically a repeat?"

If YES to any → COMPLETELY CHANGE YOUR QUESTION!
═══════════════════════════════════════════════════════════════════════════════

CONTEXT FOR THIS RESPONSE:
- You just asked: "{current_question_text}"
- This is question number: {current_question_number}
- Skills being tested: {current_skills}

🚨 CLARIFICATION DETECTION - CRITICAL! 🚨
═══════════════════════════════════════════════════════════════════════════════
If the candidate's response contains ANY of these patterns, set intent to "rephrase":
- "explain" / "can you explain" / "please explain" / "could you explain"
- "clarify" / "clarification"
- "what do you mean" / "what are you asking"
- "didn't understand" / "don't understand" / "not understand"
- "unclear" / "more detail" / "elaborate"
- "rephrase" / "say that again" / "come again"
- "confused" / "what is the question"

Example candidate responses that MUST be intent="rephrase":
❌ "I didn't understand the question. Can you explain more?"  → intent: "rephrase"
❌ "Sorry, can you clarify what you mean?"  → intent: "rephrase"
❌ "I'm confused, could you elaborate on that?"  → intent: "rephrase"

DO NOT treat clarification requests as "others" and move to a new question!
The candidate is asking for help understanding the CURRENT question, not answering it!
═══════════════════════════════════════════════════════════════════════════════

🚨 INVALID/INCOMPLETE RESPONSE DETECTION - CRITICAL! 🚨
═══════════════════════════════════════════════════════════════════════════════
If the candidate's response is NOT a valid answer to the technical question, set intent to "repeat":

INVALID RESPONSES (intent="repeat") - CHECK THESE FIRST:

1. AUDIO/CONNECTION CHECKS (intent="repeat"):
   - "hello?" / "hello" / "hi"
   - "are you there?" / "can you hear me?" / "can you listen me?"
   - "i can't hear you" / "i can't hear" / "can't hear"
   - "are you listening?" / "hello, are you there?"
   - Any variation asking if AI can hear/see/is present

2. SINGLE WORDS/GREETINGS (intent="repeat"):
   - "hello", "hi", "hey", "yes", "no", "okay", "sure"
   - "uh", "um", "hmm", "well", "like"

3. INCOMPLETE FRAGMENTS (intent="repeat"):
   - Sentences that trail off: "it includes, having some part of"
   - Starting without finishing: "well, it's like, um"
   - Less than 5 words that don't answer anything

4. OFF-TOPIC/NON-ANSWERS (intent="repeat"):
   - Responses not related to the technical question asked
   - Empty or meaningless: ".", "you", "I", "the"
   - Unrelated statements: "I am hello", "I want to end" (unless truly exiting)

VALID RESPONSES (intent="others" or other appropriate intent):
- Technical answers with substance (even if brief or incomplete in quality)
- Explanations of experience or approach (even if not perfect)
- Actual attempts to answer the question (even if partially correct)
- Responses that show the candidate understood and tried to respond

EXAMPLES - STUDY THESE CAREFULLY:


❌ WRONG - These are NOT valid answers, use intent="repeat":
{{
    "user_said": "Hello?",
    "intent": "repeat",  ← Correct! Audio check, not an answer
    "ack": "I can hear you. Let me repeat the question.",
    "next_question": "{{current_question_text}}"  ← EXACT SAME
}}

{{
    "user_said": "Are you there? Can you hear me?",
    "intent": "repeat",  ← Correct! Checking connection, not answering
    "ack": "Yes, I can hear you clearly. Let me repeat the question.",
    "next_question": "{{current_question_text}}"  ← EXACT SAME
}}

{{
    "user_said": "I can't hear you.",
    "intent": "repeat",  ← Correct! Audio issue, not a technical answer
    "ack": "Let me repeat that for you.",
    "next_question": "{{current_question_text}}"  ← EXACT SAME
}}

{{
    "user_said": "Well, it includes, having some part of",
    "intent": "repeat",  ← Correct! Incomplete fragment, no substance
    "ack": "I'll repeat the question to help you provide a complete answer.",
    "next_question": "{{current_question_text}}"  ← EXACT SAME
}}

{{
    "user_said": "Um, yeah.",
    "intent": "repeat",  ← Correct! Just filler, not a real answer
    "ack": "Let me ask that again.",
    "next_question": "{{current_question_text}}"  ← EXACT SAME
}}

{{
    "user_said": "I am hello.",
    "intent": "repeat",  ← Correct! Meaningless statement, not an answer
    "ack": "Let me repeat the question.",
    "next_question": "{{current_question_text}}"  ← EXACT SAME
}}

✅ CORRECT - These ARE valid answers, use intent="others":
{{
    "user_said": "I would use a factory pattern to handle object creation and dependency injection.",
    "intent": "others",  ← Correct! This is a technical answer with substance
    "ack": "That's a good approach to managing dependencies.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "In my last project, we implemented Redux for state management.",
    "intent": "others",  ← Correct! This answers with real experience
    "ack": "Thank you for sharing that experience.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "I diagnose and resolve this issue by using AWS model in such a way like,",
    "intent": "others",  ← Correct! Started answering with technical content (even if incomplete)
    "ack": "It sounds like you have a good approach in mind. Can you elaborate on that?",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "I don't have any key qualifications.",
    "intent": "others",  ← Correct! Answering Q2 negatively, but IS a valid answer!
    "ack": "Thank you for your honesty.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "I don't have experience with that.",
    "intent": "others",  ← Correct! Negative answer, but IS addressing the question!
    "ack": "I appreciate your candor.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "So, currently, I'm working as an intern at NR Consulting.",
    "intent": "others",  ← Correct! PARTIAL answer to Q2, but IS answering!
    "ack": "Thank you for sharing that.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "I have a degree in Computer Science.",
    "intent": "others",  ← Correct! PARTIAL answer to Q2, but IS answering!
    "ack": "That's helpful background information.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

{{
    "user_said": "Let's move on to the next question.",
    "intent": "skip",  ← Correct! Candidate wants to skip this question
    "ack": "No problem! Let's move on.",
    "next_question": "[NEW QUESTION about {{next_skills}}]"
}}

🔴 CRITICAL DISTINCTION - ASK YOURSELF THIS QUESTION:
════════════════════════════════════════════════════════════════════════════════

"Is the candidate providing ANY information related to the question content?"

✅ YES → intent: "others" (move forward, EVEN IF PARTIAL OR INCOMPLETE):
- "I don't know" → Answering that they don't know = VALID ANSWER
- "I don't have experience" → Answering about their experience = VALID ANSWER  
- "I don't have any qualifications" → Answering Q2 = VALID ANSWER
- "I'm working as an intern" → PARTIAL answer to Q2 = VALID ANSWER
- "I have a CS degree" → PARTIAL answer to Q2 = VALID ANSWER
- "I'm not familiar with X" → Answering about familiarity = VALID ANSWER
- ANY response addressing the question = VALID ANSWER (even if brief/partial!)

❌ NO → intent: "repeat" (not answering, checking audio/connection):
- "Hello?" → NOT answering, checking if AI is there
- "Are you there?" → NOT answering, checking connection
- "Can you hear me?" → NOT answering, checking audio  
- "Um, yeah" → NOT answering, just filler
- "I am hello" → NOT answering, nonsense

🔵 SPECIAL CASE → intent: "skip":
- "Let's move on" / "Next question please" / "Skip this" → Candidate wants to skip

════════════════════════════════════════════════════════════════════════════════

IMPORTANT FOR Q2 (Background Question):
Q2 asks for "brief summary of professional background and key qualifications"
- "I'm an intern at X" → PARTIAL but VALID answer = intent: "others"
- "I have Y years experience" → PARTIAL but VALID answer = intent: "others"
- "I have a degree in Z" → PARTIAL but VALID answer = intent: "others"
DO NOT expect complete answers! PARTIAL = VALID!

CRITICAL RULE: If the candidate's response doesn't actually attempt to answer the technical question,
use intent="repeat" to ask the same question again. DO NOT move forward with "others" for invalid responses!

SPECIAL ATTENTION FOR AUDIO CHECKS:
- If candidate says "hello?", "can you hear me?", "are you there?" → ALWAYS intent="repeat"
- These are NOT answers to technical questions - they're checking if the AI is working!

═══════════════════════════════════════════════════════════════════════════════

🚨 CRITICAL: ACK FIELD MUST NEVER CONTAIN QUESTIONS 🚨
═══════════════════════════════════════════════════════════════════════════════
The "ack" field must ONLY contain statements.
The "ack" field must NEVER contain ANY questions.
DO NOT DUPLICATE the question in both ack and next_question!

❌ WRONG:
{{
    "ack": "Thank you. Can you describe a project?",
    "next_question": "Can you describe a project where you utilized Python?"
}}

✅ CORRECT:
{{
    "ack": "Thank you for sharing.",
    "next_question": "Can you describe a project where you utilized Python?"
}}

FORBIDDEN IN ACK: "Can you...", "Could you...", "Tell me...", "Describe...", any "?"
ALLOWED IN ACK: "Thank you.", "That's helpful.", "Let's continue."

═══════════════════════════════════════════════════════════════════════════════

RULES FOR NEXT_QUESTION:
- If intent is "repeat" → next_question MUST be: "{{current_question_text}}" (EXACT SAME)
- If intent is "rephrase" → next_question MUST be a REPHRASED version of: "{{current_question_text}}" testing {{current_skills}}
- If intent is "others" → next_question MUST be a NEW question testing: {{next_skills}} using format: {{required_format}}

🚨🚨🚨 CRITICAL: WHEN intent="repeat" 🚨🚨🚨
════════════════════════════════════════════════════════════════════════════════
JUST REPEAT THE CURRENT QUESTION EXACTLY - WORD FOR WORD.
The question is: "{{current_question_text}}"
DO NOT generate a new question!
DO NOT change anything!
COPY it exactly as written above!
════════════════════════════════════════════════════════════════════════════════

❌ WRONG - intent="repeat" with DIFFERENT question:
{{
    "intent": "repeat",
    "next_question": "Can you describe your experience with scikit-learn?"  ← WRONG! New topic!
}}
Current question was about Python projects, NOT scikit-learn!

✅ CORRECT - intent="repeat" with SAME question:
{{
    "intent": "repeat",
    "next_question": "{{current_question_text}}"  ← EXACT COPY!
}}

🚨🚨🚨 CRITICAL: WHEN intent="rephrase" 🚨🚨🚨
════════════════════════════════════════════════════════════════════════════════
JUST REPHRASE THE CURRENT QUESTION USING DIFFERENT WORDS.
The question is: "{{current_question_text}}"
DO NOT generate a NEW question!
DO NOT change the topic/skill being tested!
READ the current question and rewrite it in simpler/clearer words!
════════════════════════════════════════════════════════════════════════════════

❌ WRONG - intent="rephrase" but DIFFERENT topic:
Current question: "Can you describe a project where you utilized Python?"
{{
    "intent": "rephrase",
    "next_question": "Imagine your TensorFlow model is experiencing downtime..."  ← WRONG! Completely different!
}}

✅ CORRECT - intent="rephrase" with SAME content, different wording:
Current question: "Can you describe a project where you utilized Python?"
{{
    "intent": "rephrase",
    "next_question": "Walk me through a Python project you've worked on. What challenges did you face?"  ← SAME content, simpler words!
}}

THE ACK AND NEXT_QUESTION FIELDS ARE COMPLETELY SEPARATE!

✅ CORRECT EXAMPLES:
{{
    "ack": "Great explanation! Your understanding of that concept is solid.",
    "next_question": "Walk me through how you would implement [feature] using {next_skills}.",
    "intent": "others"
}}

❌ WRONG - NEVER DO THIS:
{{
    "ack": "Great! Now, can you discuss a challenging project?",  ← QUESTION IN ACK = WRONG!
    "next_question": "Can you discuss a challenging project?",
    "intent": "others"
}}

❌ WRONG - NEVER ASK FOR VISUAL TASKS:
{{
    "next_question": "Design a system architecture for...",  ← NO! Say "Describe" not "Design"
    "next_question": "Draw a diagram of...",  ← NO! This is audio only!
    "next_question": "Write code to implement...",  ← NO! Say "Explain how you'd implement"
}}

🚨 ABSOLUTE RULE: The ack field must NEVER contain questions!
🚨 ABSOLUTE RULE: Questions must be audio-friendly (no drawing/writing required)!
🚨 ABSOLUTE RULE: NEVER repeat or ask similar questions - check the list above!

═══════════════════════════════════════════════════════════════════════════════
FINAL CHECKLIST
═══════════════════════════════════════════════════════════════════════════════

Before responding:

1. ✓ Does next_question test skills from: {next_skills}?
2. ✓ Does next_question use the required format: {required_format}?
3. ✓ Is next_question audio-friendly (no drawing/writing)?
4. ✓ Is next_question DIFFERENT from all previously asked questions?
5. ✓ Is ack free of questions (pure acknowledgment)?
6. ✓ Is question_no correct?
7. ✓ If intent "others" → next_question is NEW question about {next_skills} using {required_format}?

═══════════════════════════════════════════════════════════════════════════════
⚠️  QUESTION DIFFICULTY & TONE — MANDATORY (OVERRIDES ALL OTHER RULES)
═══════════════════════════════════════════════════════════════════════════════

{difficulty_instructions}

🚨 The difficulty rules above OVERRIDE your default conversational style.
Do NOT soften the question into "How would you approach..." or "Can you describe...".
═══════════════════════════════════════════════════════════════════════════════

NOW PROCESS THE CANDIDATE'S RESPONSE:

CANDIDATE SAID: {user_message}
"""


# =============================================================================
# PLANNER PROMPTS — Used by create_planner_response (split-brain mode)
# Uses Chat Completions API with dedicated system prompt (no conversation chain)
# System prompt = behavioral identity, User message = context data
# =============================================================================

PLANNER_SYSTEM_PROMPT = """You are a senior technical interviewer. You've done hundreds of interviews.
You don't read from a script. You have a conversation.

Your job: generate ONE question that tests specific skills. That's it.

HOW YOU SOUND:
- Like a real person sitting across a table, genuinely curious about their work
- You vary your energy — sometimes you're casual ("So let's say..."), sometimes direct ("Walk me through...")
- You paint vivid, specific situations — not abstract concepts
- You make the candidate THINK, not recite definitions

THE GOLDEN RULE:
Your question must be impossible to answer without knowing the required skills.
If a random person off the street could attempt an answer — your question is garbage. Rewrite.

WHAT MAKES A GREAT INTERVIEW QUESTION:
1. It grounds the skill in a REAL situation — a design choice, a tradeoff, something they'd actually hit at work
2. It usually asks for a DECISION or a TRADEOFF — BUT a direct conceptual/mechanism question is
   also great when the format calls for it ("How does X actually work?", "Why does Y happen?").
   A real interviewer mixes both. What's banned is VAGUE recitation ("tell me about your experience with X").
3. It sounds different from every question you've asked before — different opener, different structure, different energy
4. It tests the skill without NAMING the skill — embed it in the scenario (except for pure concept questions,
   where naming the concept directly is fine and expected)

WHEN THE FORMAT SAYS CONCEPTUAL / DIRECT CONCEPT QUESTION:
Honor it. Ask a crisp theory/mechanism question directly — "What actually happens when…", "Explain how…
works", "What's the difference between… and…". Do NOT wrap it in a scenario or bolt on artificial
constraints. Scale the DEPTH to the difficulty (a junior gets a definition; a senior gets the mechanism,
edge cases, and the 'why'), not the amount of scenario scaffolding.

NOT EVERY QUESTION IS A CRISIS — AND VARY THE PRESSURE:
Match the intensity to the moment. Fundamentals and simpler skills deserve simple, direct,
curious questions — "How do you usually handle X?", "What's actually happening when Y occurs?".
Do NOT lean on the same device every time. The "it's 2 AM, the demo is in 15 minutes, production
is on fire" time-pressure framing is ONE option — and if you've used it recently, DON'T use it
again. Rotate the kind of pressure: a resource limit, a compatibility/legacy constraint, a
correctness/edge-case requirement, a scale target, a cost ceiling, a team/process disagreement,
or just a clean open design question with NO crisis at all. A wall of urgent incidents back-to-back
is just another template.

KEEP IT SHORT:
One or two sentences. If you're writing a paragraph-long word problem with a "the catch is…"
twist, you've lost the thread — cut it to the single sharp question underneath.

EXAMPLE — SAME SKILL, THREE DIFFERENT VIBES:
Skill: Redis caching

Vibe 1 (Debugging): "Your API response times just spiked from 50ms to 3 seconds. You check CloudWatch and your Redis hit rate dropped from 95% to 12% overnight. Nothing deployed. What do you look at first?"

Vibe 2 (Design decision): "You're building a recommendation engine. Your data scientist wants results in under 100ms but the model inference takes 800ms. How would you architect the caching layer?"

Vibe 3 (Tradeoff): "Your Redis cluster is eating 40GB of memory and your CTO is asking why the AWS bill doubled. You can't just throw more memory at it. What's your move?"

NEVER sound like:
✗ "Can you explain your experience with Redis caching and how you would use it in a distributed system?"
✗ "How would you implement caching using Redis to improve application performance?"
✗ "In your previous experience, how have you used Redis for caching purposes?"

THESE ARE DEAD. They sound like ChatGPT wrote them. Don't do it.

HARD CONSTRAINTS:
- Test ONLY the skills listed in the user message. No substitutions.
- Audio-friendly — no code writing, drawing, or visual tasks.
- Output ONLY valid JSON: {"question_no": N, "next_question": "..."}
- ONE question. Not two. Not three. One.
- If intent is "repeat" → return the exact same question.
- If intent is "skip" → completely new topic, new angle.
"""


FOLLOWUP_PLANNER_SYSTEM_PROMPT = """You are a FOLLOW-UP QUESTION GENERATOR for professional interviews.

Your ONLY job is to generate a single, natural FOLLOW-UP question based on the candidate's previous answer.
You are NOT starting a new topic. You are digging deeper into what they just said.

═══════════════════════════════════════════════════════════════════════════════
🔄 FOLLOW-UP RULES
═══════════════════════════════════════════════════════════════════════════════

1. The question MUST START by referencing something SPECIFIC the candidate said.
   This is the most critical rule. The candidate should feel like you were LISTENING.
2. After the reference, ask ONE focused question that digs deeper.
3. It must test the SAME skills as the original question.
4. Keep it conversational and audio-friendly (no code, no diagrams).
5. Do NOT re-ask the original question in different words.
6. Sound genuinely curious, not interrogative or formulaic.
7. ONE sentence only. No multi-part questions.

═══════════════════════════════════════════════════════════════════════════════
🎙️ QUESTION STRUCTURE (MANDATORY)
═══════════════════════════════════════════════════════════════════════════════

EVERY follow-up question MUST follow this 2-part structure:

  [Reference to their answer] + [Focused deeper question]

PERFECT examples:
  - "So you mentioned using LoRA for the fine-tuning — how did you decide on the rank and alpha values for that?"
  - "You said you went with an event-driven architecture — what happens when one of those events fails midway through processing?"
  - "Going back to what you said about FAISS — how did you handle the index updates when new embeddings came in?"
  - "You talked about using Redis as a caching layer — what was your eviction strategy when memory started running low?"
  - "So you built that RAG pipeline with LangChain — what did you do when the retriever was pulling in irrelevant chunks?"
  - "You mentioned running that on Kubernetes — how did you handle scaling the model inference pods under heavy load?"

FORBIDDEN patterns (will sound robotic):
  - "Can you elaborate on your experience with..." ← GENERIC, BORING
  - "Tell me more about how you..." ← LAZY
  - "Could you provide more details about..." ← FORMULAIC
  - "What is your approach to..." ← NOT REFERENCING THEIR ANSWER
  - "Describe how you would handle..." ← NOT A FOLLOW-UP, IT'S A NEW QUESTION

STARTER PHRASES (use one of these or similar):
  - "So you mentioned..."
  - "You said you used..."
  - "Going back to what you said about..."
  - "You talked about..."
  - "So when you were working on that..."
  - "You brought up..."

═══════════════════════════════════════════════════════════════════════════════
📏 FORMAT
═══════════════════════════════════════════════════════════════════════════════

Output ONLY valid JSON: {"question_no": N, "next_question": "..."}
The question MUST be a single sentence that sounds natural when spoken aloud.
"""


PLANNER_REMINDERS = """
YOUR NEXT QUESTION MUST TEST: {next_skills}
Question #{next_question_number} | Format: {required_format} | Intent: {intent}
{question_budget}
{difficulty_instructions}

{skill_tracking_context}

PREVIOUS Q&A:
Q{current_question_number}: "{current_question_text}"
Skills: {current_skills}
Answer: {user_message}

QUESTIONS ALREADY ASKED (don't repeat topics, angles, or openers):
{asked_questions_summary}

SELF-CHECK: Read your question aloud. Does it sound like a real human said it? If it sounds like a ChatGPT prompt — rewrite it.

Respond with ONLY valid JSON:
{{"question_no": {next_question_number}, "next_question": "..."}}
"""


# =============================================================================
# HELPER FUNCTIONS & STRATEGIES
# =============================================================================


def decide_blend_mode(question_number: int) -> Optional[str]:
    """Deterministic blend mode based on pre-calculated RESUME_SLOTS."""
    if question_number in RESUME_SLOTS:
        return "resume"
    return None



# =============================================================================
# RESPONSES CLIENT - COMPLETE FIXED VERSION
# =============================================================================

class ResponsesClient:
    """Client for OpenAI Responses API with skill tracking and format diversity."""
    
    def __init__(self, api_key: str = None, model: str = None, interview_config=None, candidate_skill_baselines: dict = None):
        self.api_key = api_key or OPENAI_API_KEY
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self.client = OpenAI(api_key=self.api_key)
        self.model = model or OPENAI_MODEL
        
        # Interview config (Phase 1: stored for future centralization)
        self.interview_config = interview_config
        self._skill_baselines = candidate_skill_baselines or {}
        
        # Skill tracker
        self.skill_tracker: Optional[SkillTracker] = None
        self._next_skills: List[str] = []
        
        # Format tracker for question diversity
        self.format_tracker: Optional[QuestionFormatTracker] = None
        self._next_format: Optional[QuestionFormat] = None
        
        # NEW: Track current question for registration
        self._current_question_text: str = ""
        
        # Per-question metadata log for pre-report diagnostic
        self._question_log: List[dict] = []
        
        # GPT-parsed resume project data (populated async during intro speech)
        self.parsed_resume_projects: Optional[str] = None

        # CAMPUS Phase 3: hybrid question sourcing — curated bank + on-target guard.
        # Feature-flagged (CAMPUS_USE_BANK, default on). Empty bank → pure LLM (no-op).
        self._campus_used_ids: set = set()
        self._campus_bank = None
        if os.getenv("CAMPUS_USE_BANK", "1") == "1":
            try:
                from campus.question_bank import QuestionBank
                self._campus_bank = QuestionBank()
            except Exception as e:
                logger.warning(f"Campus question bank unavailable ({e}); using LLM-only sourcing.")

        # CAMPUS coaching config (practice-mode hints / model answers). Resolved from env;
        # per-interview overrides ride on interview_config if present.
        try:
            from campus.config import CampusConfig
            _cov = getattr(interview_config, "campus", None) if interview_config else None
            self._campus_cfg = CampusConfig.resolve(_cov if isinstance(_cov, dict) else None)
        except Exception:
            self._campus_cfg = None

    def maybe_coach_hint(self, question: str, skill: str, difficulty_level: int = 2) -> str:
        """Practice-mode HINT when a student struggles; '' if coaching is off/assessment."""
        if not (self._campus_cfg and self._campus_cfg.gives_hints()):
            return ""
        from campus.coaching import generate_hint
        return generate_hint(self.client, question, skill, difficulty_level)

    def maybe_coach_model_answer(self, question: str, skill: str, student_answer: str = "") -> str:
        """Practice-mode MODEL ANSWER after a weak reply; '' if coaching is off/assessment."""
        if not (self._campus_cfg and self._campus_cfg.gives_model_answers()):
            return ""
        from campus.coaching import generate_model_answer
        return generate_model_answer(self.client, question, skill, student_answer)

        # CAMPUS coaching config (practice-mode hints / model answers). Resolved from env;
        # per-interview overrides ride on interview_config if present.
        try:
            from campus.config import CampusConfig
            _cov = getattr(interview_config, "campus", None) if interview_config else None
            self._campus_cfg = CampusConfig.resolve(_cov if isinstance(_cov, dict) else None)
        except Exception:
            self._campus_cfg = None

    def maybe_coach_hint(self, question: str, skill: str, difficulty_level: int = 2) -> str:
        """Practice-mode HINT when a student struggles; '' if coaching is off/assessment."""
        if not (self._campus_cfg and self._campus_cfg.gives_hints()):
            return ""
        from campus.coaching import generate_hint
        return generate_hint(self.client, question, skill, difficulty_level)

    def maybe_coach_model_answer(self, question: str, skill: str, student_answer: str = "") -> str:
        """Practice-mode MODEL ANSWER after a weak reply; '' if coaching is off/assessment."""
        if not (self._campus_cfg and self._campus_cfg.gives_model_answers()):
            return ""
        from campus.coaching import generate_model_answer
        return generate_model_answer(self.client, question, skill, student_answer)

        # CAMPUS coaching config (practice-mode hints / model answers). Resolved from env;
        # per-interview overrides ride on interview_config if present.
        try:
            from campus.config import CampusConfig
            _cov = getattr(interview_config, "campus", None) if interview_config else None
            self._campus_cfg = CampusConfig.resolve(_cov if isinstance(_cov, dict) else None)
        except Exception:
            self._campus_cfg = None

    def maybe_coach_hint(self, question: str, skill: str, difficulty_level: int = 2) -> str:
        """Practice-mode HINT when a student struggles; '' if coaching is off/assessment."""
        if not (self._campus_cfg and self._campus_cfg.gives_hints()):
            return ""
        from campus.coaching import generate_hint
        return generate_hint(self.client, question, skill, difficulty_level)

    def maybe_coach_model_answer(self, question: str, skill: str, student_answer: str = "") -> str:
        """Practice-mode MODEL ANSWER after a weak reply; '' if coaching is off/assessment."""
        if not (self._campus_cfg and self._campus_cfg.gives_model_answers()):
            return ""
        from campus.coaching import generate_model_answer
        return generate_model_answer(self.client, question, skill, student_answer)

    def _campus_source_question(self, llm_question: str, skills, question_no: int) -> str:
        """Hybrid sourcing for the primary planner question (Phase 3):
          1. Prefer an UNUSED curated bank question for the target skill + difficulty tier
             (on-target by construction, great for repeatable prep).
          2. Otherwise validate the LLM question is on-target for the skill; log drift.
        No-op when the bank is disabled/empty."""
        bank = self._campus_bank
        if not bank:
            return llm_question
        skill = (list(skills) or [None])[0] if skills else None
        if not skill:
            return llm_question
        # Only banked skills can be served/validated cheaply (we have their vocabulary);
        # verbose or unbanked JD skills fall straight through to the LLM (hybrid design).
        if not bank.has_skill(skill):
            return llm_question
        from campus.question_bank import is_on_target, vector_to_tier, APPLIED
        vector = getattr(self, "_last_used_vector", None)
        tier = vector_to_tier(vector) if vector is not None else APPLIED
        level = self.interview_config.role_experience_required if self.interview_config else "mid"
        entry = bank.get(skill, level, tier, self._campus_used_ids)
        if entry:
            self._campus_used_ids.add(entry["id"])
            logger.info(f"📚 Campus bank hit for '{skill}' [{level}/{tier}] → {entry['id']}")
            return entry["question"]
        # Banked but pool exhausted → validate the LLM fallback stayed on-target.
        if llm_question and not is_on_target(llm_question, skill, bank.aliases(skill), bank.topics(skill)):
            logger.warning(f"⚠️ Campus on-target: Q{question_no} may be off-skill for '{skill}': {llm_question[:80]}")
        return llm_question

    def process_response(self, intent: str):
        """Delegate response processing to skill tracker."""
        if self.skill_tracker:
            self.skill_tracker.process_response(intent)
    
    def parse_resume_projects(self, resume_text: str) -> str:
        """
        Use GPT to extract structured project info from resume.
        Called ASYNC during intro speech (22s window).
        Stores result in self.parsed_resume_projects for use in resume question prompts.
        """
        if not resume_text or not resume_text.strip():
            self.parsed_resume_projects = None
            return ""
        
        try:
            import time as _t
            start = _t.time()
            
            response = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": """You are a resume parser. Extract ALL projects AND work experiences from this resume.
Your output will be used by an interviewer to ask specific, natural questions.

SECTION 1 — WORK EXPERIENCE (extract ALL jobs/roles):
For EACH role, output:

📌 EXPERIENCE: [Company Name] — [Role Title]
   Duration: [time period, e.g. "2020-2024"]
   Responsibilities: [what they did — be specific]
   Tech Used: [specific technologies, frameworks, tools mentioned]
   Key Achievement: [any metrics, impact, or notable accomplishments]

SECTION 2 — PROJECTS (extract ALL projects):
For EACH project, output:

📌 PROJECT: [Exact Project Name as written in resume]
   Tech Stack: [all technologies, models, tools used]
   What They Built: [1-2 line summary]
   Key Metrics: [any numbers, percentages, scale, performance stats]
   Notable Detail: [anything unique — custom architecture, novel approach, specific challenge mentioned]

RULES:
- Use the EXACT project names and company names from the resume — do NOT paraphrase
- Extract EVERY project and experience, even small ones
- For tech stack, list specific model names (e.g. "Falcon 1.3B", not just "LLM")
- Include specific metrics/numbers whenever mentioned (e.g. "reduced hallucination by 23%")
- If a project mentions training a model, note the parameters/architecture"""
                    },
                    {
                        "role": "user",
                        "content": f"Parse this resume and extract all projects and experiences:\n\n{resume_text[:3000]}"
                    }
                ],
                temperature=0,
                max_tokens=1000,
            )
            
            parsed = response.choices[0].message.content.strip()
            elapsed = _t.time() - start
            
            self.parsed_resume_projects = parsed
            logger.info(f"✅ Resume parsed: {len(parsed)} chars in {elapsed*1000:.0f}ms")
            return parsed
            
        except Exception as e:
            logger.error(f"❌ Resume parsing failed: {e}")
            self.parsed_resume_projects = None
            return ""
    
    @property
    def _total_questions(self) -> int:
        """Config-aware total questions. Single source of truth for instance methods."""
        if self.interview_config:
            return self.interview_config.questions_per_round
        return NUMBER_OF_QUESTIONS
    
    def _get_vector_prompt_instructions(self, question_number: int, skills: List[str] = None, context=None, conceptual: bool = False) -> str:
        """Get difficulty instructions from policy module using config and optional per-skill baseline."""
        if self.interview_config:
            round_num = getattr(self, '_current_round', 1)
            role = self.interview_config.role_experience_required
            base_diff = self.interview_config.base_difficulty
            
            best_baseline = None
            best_skill_name = "default"
            if self._skill_baselines and skills:
                for skill in skills:
                    if skill in self._skill_baselines:
                        best_baseline = self._skill_baselines[skill]
                        best_skill_name = skill
                        break
                    
                    lower_skill = skill.lower()
                    for b_key, b_data in self._skill_baselines.items():
                        if lower_skill in b_key.lower() or b_key.lower() in lower_skill:
                            best_baseline = b_data
                            best_skill_name = b_key
                            break
                    
                    if best_baseline:
                        break
            
            self._last_used_baseline = best_baseline  # Expose for external debug callers
            
            concept_focus = None
            concept_mode = None

            if context and best_skill_name in context.skill_states:
                # Phase 3C: Hit the adaptation layer!
                vector = context.prepare_skill_for_next_question(best_skill_name)
                state = context.skill_states[best_skill_name]
                concept_focus = getattr(state, "current_concept_focus", None)
                concept_mode = getattr(state, "current_concept_mode", None)

                # Fallback purely for safety if it returns None (e.g. removed skill slipped through)
                if not vector:
                    vector = compute_difficulty_vector(
                        base_difficulty=base_diff,
                        role_experience_required=role,
                        round_number=round_num,
                        skill_baseline=best_baseline,
                        total_rounds=self.interview_config.total_rounds,
                    )
            else:
                vector = compute_difficulty_vector(
                    base_difficulty=base_diff,
                    role_experience_required=role,
                    round_number=round_num,
                    skill_baseline=best_baseline,
                    total_rounds=self.interview_config.total_rounds,
                )
            self._last_used_vector = vector
            instructions = compile_vector_prompt(
                vector,
                role,
                concept_focus=concept_focus,
                concept_mode=concept_mode,
                conceptual=conceptual,
                rotation_index=question_number,
            )
            
            debug = get_escalation_debug_info(
                base_diff, role, round_num, question_number, best_baseline
            )
            
            logger.info(
                f"📐 Difficulty Vector: [{vector}] "
                f"| Base: {base_diff} Role: {role} "
                f"| Q{question_number} R{round_num}"
            )
            return instructions
        return ""
    
    def init_skill_tracker(self, mandatory_skills: List[str], optional_skills: List[str] = None, skill_ratio: Dict[str, int] = None):
        """Initialize skill tracker with mandatory skills and optional skills."""
        if mandatory_skills or optional_skills:
            if optional_skills is None:
                optional_skills = []
            if skill_ratio is None:
                from config_loader import SKILL_RATIO
                skill_ratio = SKILL_RATIO
            
            self.skill_tracker = SkillTracker(
                mandatory_skills=mandatory_skills or [],
                optional_skills=optional_skills,
                skill_ratio=skill_ratio,
                _total_questions=self._total_questions
            )
            # Wire resume_ratio from InterviewConfig into the format tracker
            resume_ratio = RESUME_QUESTION_PERCENTAGE  # Default from env var
            if self.interview_config and hasattr(self.interview_config, 'resume_question_ratio'):
                resume_ratio = self.interview_config.resume_question_ratio
                logger.info(f"📋 Using InterviewConfig resume_ratio={resume_ratio:.0%}")
            
            # Wire concept/scenario quotas + resume count from InterviewConfig
            target_conceptual = None
            target_scenario = None
            target_resume = None
            if self.interview_config:
                target_conceptual = getattr(self.interview_config, 'conceptual_question_count', None)
                target_scenario = getattr(self.interview_config, 'scenario_question_count', None)
                target_resume = getattr(self.interview_config, 'resume_question_count', None)

            # In deterministic (explicit-count) mode the config's questions_per_round is the
            # CONTENT count. The interview loop asks those as Q3..Q(qpr+2) — greeting (cq=1)
            # then qpr content — so the tracker must model qpr+2 total, else the last two
            # content questions fall outside the quota and default to scenario (the bug).
            _explicit = (target_conceptual is not None or target_scenario is not None)
            _tot_wg = (self._total_questions + 2) if _explicit else self._total_questions

            self.format_tracker = QuestionFormatTracker(
                _total_q_with_greeting=_tot_wg,
                resume_ratio=resume_ratio,
                target_conceptual=target_conceptual,
                target_scenario=target_scenario,
                target_resume=target_resume,
            )
            if _explicit:
                logger.info(f"📋 Format quotas (deterministic): conceptual={target_conceptual}, "
                            f"scenario={target_scenario}, resume={target_resume}, content_qs={self._total_questions}")
            logger.info(f"Trackers initialized: {len(mandatory_skills or [])} mandatory, {len(optional_skills)} optional skills, total_questions={self._total_questions}, ratio={skill_ratio}")
    
    def has_resume_relevance(self, resume_text: str, mandatory_skills: List[str]) -> bool:
        """Check if resume has relevant experience with mandatory skills."""
        if not resume_text:
            return False
        
        # Heuristic: If we have a decent amount of resume text, assume it's relevant
        # especially if the user manually provided it for simulation.
        # The keyword matching is flaky with long "skill sentences".
        if len(resume_text) > 100:
            return True
            
        resume_lower = resume_text.lower()
        matches = sum(1 for skill in mandatory_skills if skill.lower() in resume_lower)
        
        return matches >= 1 # Even 1 match is enough to attempt resume questions
    
    def _build_format_context(self,
                             format_type: QuestionFormat,
                             skills: List[str],
                             has_resume: bool,
                             resume_text: str = "",
                             role: str = "mid") -> str:
        """Build format-specific instructions that get injected dynamically."""
        skill_str = ', '.join(skills) if skills else 'relevant skills'
        
        base_instructions = self.format_tracker.get_format_prompt(
            format_type=format_type,
            skills=skills,
            has_resume=has_resume,
            resume_text=resume_text,
            role=role,
            parsed_resume_projects=self.parsed_resume_projects
        )
        
        format_status = self.format_tracker.get_status()
        exp_count = format_status['format_counts'].get('experimental', 0)
        resume_count = format_status['format_counts'].get('resume_project', 0)
        direct_count = format_status['format_counts'].get('direct_experience', 0)
        
        is_deep_dive = self.format_tracker.project_deep_dive_mode
        total_technical = self.format_tracker.total_questions
        target_resume_qs = math.floor(total_technical * self.format_tracker.resume_ratio) if is_deep_dive else self.format_tracker.max_resume_questions
        
        # Compute JD question budget for prompts
        jd_count = format_status['format_counts'].get('direct_experience', 0) + \
                   format_status['format_counts'].get('scenario_based', 0) + \
                   format_status['format_counts'].get('troubleshooting', 0) + \
                   format_status['format_counts'].get('comparison', 0) + \
                   format_status['format_counts'].get('experimental', 0) + \
                   format_status['format_counts'].get('verbal_architecture', 0) + \
                   format_status['format_counts'].get('implementation_walkthrough', 0)
        
        # Get explicit counts from config (if set)
        has_explicit_counts = (
            self.interview_config and 
            getattr(self.interview_config, 'jd_question_count', None) is not None and
            getattr(self.interview_config, 'resume_question_count', None) is not None
        )
        explicit_jd_target = getattr(self.interview_config, 'jd_question_count', None) if self.interview_config else None
        explicit_resume_target = getattr(self.interview_config, 'resume_question_count', None) if self.interview_config else None
        # Get concept/scenario config
        has_concept_scenario = (
            self.interview_config and
            getattr(self.interview_config, 'conceptual_question_count', None) is not None and
            getattr(self.interview_config, 'scenario_question_count', None) is not None
        )
        conceptual_count = format_status['format_counts'].get('conceptual', 0)
        scenario_count = (
            format_status['format_counts'].get('scenario_based', 0) +
            format_status['format_counts'].get('troubleshooting', 0) +
            format_status['format_counts'].get('comparison', 0) +
            format_status['format_counts'].get('experimental', 0) +
            format_status['format_counts'].get('verbal_architecture', 0) +
            format_status['format_counts'].get('implementation_walkthrough', 0)
        )
        explicit_concept_target = getattr(self.interview_config, 'conceptual_question_count', None) if self.interview_config else None
        explicit_scenario_target = getattr(self.interview_config, 'scenario_question_count', None) if self.interview_config else None

        format_urgency = []
        if is_deep_dive:
            # Deep-dive mode urgency
            remaining_resume = target_resume_qs - resume_count
            if remaining_resume > 0:
                format_urgency.append(f"🎯 DEEP-DIVE MODE: {remaining_resume} more RESUME PROJECT questions needed ({resume_count}/{target_resume_qs})")
            else:
                format_urgency.append(f"✅ Resume quota met ({resume_count}/{target_resume_qs}). Remaining Qs are skill-based variety.")
        else:
            # Standard mode urgency — use explicit targets when configured
            if exp_count < 2:
                format_urgency.append(f"⚠️ Need {2-exp_count} more EXPERIMENTAL questions")
            
            if has_explicit_counts:
                # Explicit counts configured — show precise budget
                remaining_resume = explicit_resume_target - resume_count
                remaining_jd = explicit_jd_target - jd_count
                if remaining_resume > 0 and has_resume:
                    format_urgency.append(f"📋 RESUME QUESTIONS: {remaining_resume} more needed ({resume_count}/{explicit_resume_target})")
                elif remaining_resume <= 0 and has_resume:
                    format_urgency.append(f"✅ Resume question quota met ({resume_count}/{explicit_resume_target})")
                if remaining_jd > 0:
                    format_urgency.append(f"📋 JD/SKILL QUESTIONS: {remaining_jd} more needed ({jd_count}/{explicit_jd_target})")
                elif remaining_jd <= 0:
                    format_urgency.append(f"✅ JD question quota met ({jd_count}/{explicit_jd_target})")
            else:
                # Legacy ratio-based urgency
                if resume_count < target_resume_qs and has_resume:
                    remaining_resume = target_resume_qs - resume_count
                    format_urgency.append(f"🚨 Need {remaining_resume} more RESUME PROJECT questions ({resume_count}/{target_resume_qs})")
                elif resume_count >= target_resume_qs and has_resume:
                    format_urgency.append(f"✅ Resume quota met ({resume_count}/{target_resume_qs})")
            
            # Concept/Scenario urgency
            if has_concept_scenario:
                concept_remaining = explicit_concept_target - conceptual_count
                scenario_remaining = explicit_scenario_target - scenario_count
                if concept_remaining > 0:
                    format_urgency.append(f"💡 CONCEPT QUESTIONS: {concept_remaining} more needed ({conceptual_count}/{explicit_concept_target})")
                elif concept_remaining <= 0:
                    format_urgency.append(f"✅ Concept question quota met ({conceptual_count}/{explicit_concept_target})")
                if scenario_remaining > 0:
                    format_urgency.append(f"🎭 SCENARIO QUESTIONS: {scenario_remaining} more needed ({scenario_count}/{explicit_scenario_target})")
                elif scenario_remaining <= 0:
                    format_urgency.append(f"✅ Scenario question quota met ({scenario_count}/{explicit_scenario_target})")
                    
            if direct_count >= 2:
                format_urgency.append("🚫 NO MORE DIRECT EXPERIENCE questions allowed")
        
        urgency_str = "\n".join(format_urgency) if format_urgency else "✅ Format quotas on track"
        
        # Deep-dive mode banner
        mode_banner = ""
        if is_deep_dive and format_type == QuestionFormat.RESUME_PROJECT:
            mode_banner = f"""
🎯 PROJECT DEEP-DIVE MODE ACTIVE (resume_ratio={self.format_tracker.resume_ratio:.0%})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are DEEP-DIVING into the candidate's resume projects. Be NATURAL and SPECIFIC.

RULES FOR RESUME DEEP-DIVE:
- ALWAYS mention the PROJECT BY NAME — "So I saw your Legal LLM project..."
- First 1-2 resume questions should be OPENERS: "Tell me about your [Project Name] — how did you build that?"
- After the opener, switch to PROBES: "Why did you use X instead of Y in [Project Name]?"
- Reference SPECIFIC tech, tools, or metrics from the resume
- Each question should cover a DIFFERENT project or a DIFFERENT angle on the same project
- Sound like a real interviewer who read the resume, not a robot

Resume project questions asked so far: {resume_count}/{target_resume_qs}
"""
        
        # JD/Resume Budget Banner (only when explicit counts are configured)
        budget_banner = ""
        if has_explicit_counts and not is_deep_dive:
            concept_scenario_line = ""
            if has_concept_scenario:
                concept_scenario_line = f"""  ├─ Conceptual (theory):    {conceptual_count} / {explicit_concept_target} asked
  └─ Scenario (applied):     {scenario_count} / {explicit_scenario_target} asked"""
            budget_banner = f"""
📊 QUESTION BUDGET (Configured)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  JD/Skill-based questions:   {jd_count} / {explicit_jd_target} asked
{concept_scenario_line}
  Resume-based questions:     {resume_count} / {explicit_resume_target} asked
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        elif has_concept_scenario and not is_deep_dive:
            budget_banner = f"""
📊 CONCEPT vs SCENARIO BUDGET
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Conceptual (theory):    {conceptual_count} / {explicit_concept_target} asked
  Scenario (applied):     {scenario_count} / {explicit_scenario_target} asked
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        
        # Status section (mode-aware)
        if is_deep_dive:
            status_section = f"""FORMAT TRACKING STATUS:
- Resume Projects: {resume_count}/{target_resume_qs} ({self.format_tracker.resume_ratio:.0%} of technical Qs)
- Skill-variety Qs: {total_technical - target_resume_qs} allocated for non-project skill checks"""
        elif has_explicit_counts:
            concept_scenario_status = ""
            if has_concept_scenario:
                concept_scenario_status = f"""\n- Conceptual: {conceptual_count}/{explicit_concept_target} {"✅" if conceptual_count >= explicit_concept_target else "❌"}\n- Scenario: {scenario_count}/{explicit_scenario_target} {"✅" if scenario_count >= explicit_scenario_target else "❌"}"""
            status_section = f"""FORMAT TRACKING STATUS:
- JD/Skill Questions: {jd_count}/{explicit_jd_target} {"✅" if jd_count >= explicit_jd_target else "❌"}{concept_scenario_status}
- Resume Projects: {resume_count}/{explicit_resume_target} {"✅" if resume_count >= explicit_resume_target else "❌"}
- Experimental (what-if): {exp_count}/2-3 {"✅" if exp_count >= 2 else "❌"}
- Direct Experience: {direct_count}/2 {"🚫" if direct_count >= 2 else "✅"}"""
        else:
            concept_scenario_status = ""
            if has_concept_scenario:
                concept_scenario_status = f"""\n- Conceptual: {conceptual_count}/{explicit_concept_target} {"✅" if conceptual_count >= explicit_concept_target else "❌"}\n- Scenario: {scenario_count}/{explicit_scenario_target} {"✅" if scenario_count >= explicit_scenario_target else "❌"}"""
            status_section = f"""FORMAT TRACKING STATUS:
- Experimental (what-if): {exp_count}/2-3 {"✅" if exp_count >= 2 else "❌"}
- Resume Projects: {resume_count}/{target_resume_qs} {"✅" if resume_count >= target_resume_qs else ("🚨 CRITICAL - NEED MORE!" if has_resume else "N/A")}{concept_scenario_status}
- Direct Experience: {direct_count}/2 {"🚫" if direct_count >= 2 else "✅"}"""
        
        return f"""
═══════════════════════════════════════════════════════════════════════════════
📋 QUESTION FORMAT REQUIREMENTS
═══════════════════════════════════════════════════════════════════════════════
{mode_banner}{budget_banner}
{status_section}

{urgency_str}

🎙️ AUDIO INTERVIEW REMINDER: Questions must be answerable verbally - no drawing/writing!

═══════════════════════════════════════════════════════════════════════════════
🎯 REQUIRED FORMAT FOR NEXT QUESTION: {format_type.value.upper().replace('_', ' ')}
═══════════════════════════════════════════════════════════════════════════════

{base_instructions}

🚨 YOU MUST USE THIS FORMAT - This is not a suggestion, it's a requirement!
🚨 The question MUST test: {skill_str}
🚨 The question MUST be audio-friendly (no visual/writing tasks)!
🚨 STAY STRICTLY INSIDE THE SKILL LIST. Do NOT introduce any technology, tool, framework, or domain that is not one of: {skill_str}. In particular, NEVER bring up Docker, Kubernetes, containers, deployment, CI/CD, DevOps, microservices, cloud, or infrastructure UNLESS that exact word appears in the skill list above. The example phrasings below only show question SHAPE — copy the shape, never their domain; ground every part of the question in the listed skills.
🚨 STAY STRICTLY INSIDE THE SKILL LIST. Do NOT introduce any technology, tool, framework, or domain that is not one of: {skill_str}. In particular, NEVER bring up Docker, Kubernetes, containers, deployment, CI/CD, DevOps, microservices, cloud, or infrastructure UNLESS that exact word appears in the skill list above. The example phrasings below only show question SHAPE — copy the shape, never their domain; ground every part of the question in the listed skills.
🚨 STAY STRICTLY INSIDE THE SKILL LIST. Do NOT introduce any technology, tool, framework, or domain that is not one of: {skill_str}. In particular, NEVER bring up Docker, Kubernetes, containers, deployment, CI/CD, DevOps, microservices, cloud, or infrastructure UNLESS that exact word appears in the skill list above. The example phrasings below only show question SHAPE — copy the shape, never their domain; ground every part of the question in the listed skills.
🚨 STAY STRICTLY INSIDE THE SKILL LIST. Do NOT introduce any technology, tool, framework, or domain that is not one of: {skill_str}. In particular, NEVER bring up Docker, Kubernetes, containers, deployment, CI/CD, DevOps, microservices, cloud, or infrastructure UNLESS that exact word appears in the skill list above. The example phrasings below only show question SHAPE — copy the shape, never their domain; ground every part of the question in the listed skills.
{"" if format_type != QuestionFormat.CONCEPTUAL else '''
⛔⛔⛔ CRITICAL: CONCEPTUAL QUESTION FORMAT GUARD ⛔⛔⛔
THIS IS A DIRECT CONCEPT QUESTION. You MUST ask a PLAIN, DIRECT theory question.

✅ CORRECT STRUCTURE: "Can you explain X?" / "What's the difference between X and Y?" / "When would you use X?"
❌ WRONG STRUCTURE: "Imagine you're..." / "You're tasked with..." / "Your team is..." / Any project/scenario setup

Your question MUST start with phrases like:
- "Can you explain..."
- "What's the difference between..."
- "How does X work..."
- "When would you use..."
- "Why is X preferred over..."
- "Walk me through the concept of..."

DO NOT wrap this in a scenario. DO NOT set up a workplace situation. Just ASK the question directly.
⛔⛔⛔ END OF CONCEPTUAL GUARD ⛔⛔⛔
'''}
═══════════════════════════════════════════════════════════════════════════════
"""
    

    
    def _skip_soften_reuse(self, intent: str, context) -> List[str]:
        """On 'I don't know' (SKIP): lower the current skill's difficulty and re-ask
        it easier, instead of dropping it. Returns the skill(s) to re-ask (easier),
        or [] to fall through to normal new-skill selection (skill was at the
        easiest level and is now removed/blacklisted → move on)."""
        if not intent or intent.upper() != "SKIP" or not context or not self.skill_tracker:
            return []
        retry = []
        for sk in (self.skill_tracker._current_skills or []):
            outcome = context.soften_skill_on_skip(sk)
            if outcome == "retry":
                retry.append(sk)
                logger.info(f"🔽 SKIP-soften: re-asking '{sk}' at a lower difficulty (same skill).")
            elif outcome == "floor":
                # Engine removed it (easiest + still skipped). Keep the tracker in sync.
                self.skill_tracker._blacklisted.add(sk)
                logger.info(f"⛔ SKIP-soften: '{sk}' at easiest level and still skipped — moving on.")
        return retry

    def quota_needs_slot(self, upcoming_qnum: int) -> bool:
        """Budget-arbiter check, config-driven and role-agnostic.

        Delegates to the format tracker: True only when a conceptual/scenario
        mix is configured AND the combined remaining quota deficit (conceptual +
        scenario + resume) needs every remaining slot — i.e. a non-quota
        question can no longer be afforded without missing a target.

        Callers use it to (a) start quota enforcement from the FIRST technical
        question instead of Q3, and (b) suppress experience follow-ups so the
        slot is spent on a quota question. Returns False whenever no shape quota
        is configured, so default interviews (technical, non-technical,
        resume-heavy, any experience level) are completely unaffected.
        """
        ft = getattr(self, 'format_tracker', None)
        if not ft:
            return False
        # Fold in the last-decided-but-unregistered format (deferred registration).
        return ft.quota_needs_slot(upcoming_qnum, pending_format=getattr(self, '_next_format', None))

    def _skip_soften_reuse(self, intent: str, context) -> List[str]:
        """On 'I don't know' (SKIP): lower the current skill's difficulty and re-ask
        it easier, instead of dropping it. Returns the skill(s) to re-ask (easier),
        or [] to fall through to normal new-skill selection (skill was at the
        easiest level and is now removed/blacklisted → move on)."""
        if not intent or intent.upper() != "SKIP" or not context or not self.skill_tracker:
            return []
        retry = []
        for sk in (self.skill_tracker._current_skills or []):
            outcome = context.soften_skill_on_skip(sk)
            if outcome == "retry":
                retry.append(sk)
                logger.info(f"🔽 SKIP-soften: re-asking '{sk}' at a lower difficulty (same skill).")
            elif outcome == "floor":
                # Engine removed it (easiest + still skipped). Keep the tracker in sync.
                self.skill_tracker._blacklisted.add(sk)
                logger.info(f"⛔ SKIP-soften: '{sk}' at easiest level and still skipped — moving on.")
        return retry

    def quota_needs_slot(self, upcoming_qnum: int) -> bool:
        """Budget-arbiter check, config-driven and role-agnostic.

        Delegates to the format tracker: True only when a conceptual/scenario
        mix is configured AND the combined remaining quota deficit (conceptual +
        scenario + resume) needs every remaining slot — i.e. a non-quota
        question can no longer be afforded without missing a target.

        Callers use it to (a) start quota enforcement from the FIRST technical
        question instead of Q3, and (b) suppress experience follow-ups so the
        slot is spent on a quota question. Returns False whenever no shape quota
        is configured, so default interviews (technical, non-technical,
        resume-heavy, any experience level) are completely unaffected.
        """
        ft = getattr(self, 'format_tracker', None)
        if not ft:
            return False
        # Fold in the last-decided-but-unregistered format (deferred registration).
        return ft.quota_needs_slot(upcoming_qnum, pending_format=getattr(self, '_next_format', None))

    def _skip_soften_reuse(self, intent: str, context) -> List[str]:
        """On 'I don't know' (SKIP): lower the current skill's difficulty and re-ask
        it easier, instead of dropping it. Returns the skill(s) to re-ask (easier),
        or [] to fall through to normal new-skill selection (skill was at the
        easiest level and is now removed/blacklisted → move on)."""
        if not intent or intent.upper() != "SKIP" or not context or not self.skill_tracker:
            return []
        retry = []
        for sk in (self.skill_tracker._current_skills or []):
            outcome = context.soften_skill_on_skip(sk)
            if outcome == "retry":
                retry.append(sk)
                logger.info(f"🔽 SKIP-soften: re-asking '{sk}' at a lower difficulty (same skill).")
            elif outcome == "floor":
                # Engine removed it (easiest + still skipped). Keep the tracker in sync.
                self.skill_tracker._blacklisted.add(sk)
                logger.info(f"⛔ SKIP-soften: '{sk}' at easiest level and still skipped — moving on.")
        return retry

    def quota_needs_slot(self, upcoming_qnum: int) -> bool:
        """Budget-arbiter check, config-driven and role-agnostic.

        Delegates to the format tracker: True only when a conceptual/scenario
        mix is configured AND the combined remaining quota deficit (conceptual +
        scenario + resume) needs every remaining slot — i.e. a non-quota
        question can no longer be afforded without missing a target.

        Callers use it to (a) start quota enforcement from the FIRST technical
        question instead of Q3, and (b) suppress experience follow-ups so the
        slot is spent on a quota question. Returns False whenever no shape quota
        is configured, so default interviews (technical, non-technical,
        resume-heavy, any experience level) are completely unaffected.
        """
        ft = getattr(self, 'format_tracker', None)
        if not ft:
            return False
        # Fold in the last-decided-but-unregistered format (deferred registration).
        return ft.quota_needs_slot(upcoming_qnum, pending_format=getattr(self, '_next_format', None))

    def _skip_soften_reuse(self, intent: str, context) -> List[str]:
        """On 'I don't know' (SKIP): lower the current skill's difficulty and re-ask
        it easier, instead of dropping it. Returns the skill(s) to re-ask (easier),
        or [] to fall through to normal new-skill selection (skill was at the
        easiest level and is now removed/blacklisted → move on)."""
        if not intent or intent.upper() != "SKIP" or not context or not self.skill_tracker:
            return []
        retry = []
        for sk in (self.skill_tracker._current_skills or []):
            outcome = context.soften_skill_on_skip(sk)
            if outcome == "retry":
                retry.append(sk)
                logger.info(f"🔽 SKIP-soften: re-asking '{sk}' at a lower difficulty (same skill).")
            elif outcome == "floor":
                # Engine removed it (easiest + still skipped). Keep the tracker in sync.
                self.skill_tracker._blacklisted.add(sk)
                logger.info(f"⛔ SKIP-soften: '{sk}' at easiest level and still skipped — moving on.")
        return retry

    def quota_needs_slot(self, upcoming_qnum: int) -> bool:
        """Budget-arbiter check, config-driven and role-agnostic.

        Delegates to the format tracker: True only when a conceptual/scenario
        mix is configured AND the combined remaining quota deficit (conceptual +
        scenario + resume) needs every remaining slot — i.e. a non-quota
        question can no longer be afforded without missing a target.

        Callers use it to (a) start quota enforcement from the FIRST technical
        question instead of Q3, and (b) suppress experience follow-ups so the
        slot is spent on a quota question. Returns False whenever no shape quota
        is configured, so default interviews (technical, non-technical,
        resume-heavy, any experience level) are completely unaffected.
        """
        ft = getattr(self, 'format_tracker', None)
        if not ft:
            return False
        # Fold in the last-decided-but-unregistered format (deferred registration).
        return ft.quota_needs_slot(upcoming_qnum, pending_format=getattr(self, '_next_format', None))

    def create_planner_response(
        self,
        question_number: int,
        user_message: str,
        intent: str,
        previous_response_id: str = None,
        current_question_text: str = "",
        resume_text: str = "",
        previous_intent: str = None,
        room_id: str = "",
        process_id: int = 0,
        context=None
    ) -> Dict:
        """
        Planner-only response for split-brain mode.
        :param process_id: For logging
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        
        logger.info(f"🧠 {log_prefix} PLANNER START | Q{question_number} | intent={intent}")
        
        # ─── For repeat/rephrase, skip the API call entirely ───
        if intent.upper() == "REPEAT":
            logger.info(f"🔁 {log_prefix} PLANNER: intent=repeat, returning same question")
            return {
                "question_no": question_number,
                "next_question": current_question_text,
                "response_id": previous_response_id,
                "metrics": {"planner_skipped": True, "intent": "repeat"}
            }
        
        # ─── Build skill & format tracking context (reuses existing logic) ───
        _skip_retry = []  # skills being re-asked easier after a SKIP (set below)
        # Tracking/quotas cover Q3 onward (Q1 greeting, Q2 warm-up intro). Since a
        # call with question_number=N generates question N+1, "track from Q3" means
        # question_number >= 2 — but only engage early (Q3) when a shape quota is
        # configured, so default interviews keep their original 2-question warm-up.
        # quota_needs_slot() still forces the tracker on in the rare tight-budget
        # edge where even the first technical slot is owed to a quota.
        if self.skill_tracker and self.format_tracker and (
            question_number >= 3
            or (self.format_tracker._has_shape_quota() and question_number >= 2)
            or self.quota_needs_slot(question_number + 1)
        ):
            if self._next_skills and self._next_format:
                self.skill_tracker.register_question(question_number, self._next_skills)
                self.format_tracker.register_format(question_number, self._next_format)
            
            # On 'I don't know' (SKIP): lower the same skill's difficulty and re-ask
            # it easier instead of dropping it (down to easiest, then move on).
            _skip_retry = self._skip_soften_reuse(intent, context)
            if _skip_retry:
                next_skills = _skip_retry
                role = self.interview_config.role_experience_required if self.interview_config else "mid"
                blend_mode = None
                has_resume = self.has_resume_relevance(
                    resume_text, self.skill_tracker.mandatory_skills
                )
                next_format = self._next_format or self.format_tracker.get_next_format(
                    question_number=question_number + 1,
                    has_resume_relevance=has_resume,
                    is_blend_question=False,
                    role=role
                )
                logger.info(f"🔽 SKIP-soften: re-asking same skill(s) easier: {next_skills}")
            elif previous_intent and previous_intent.upper() == 'REPHRASE' and self._next_skills and self._next_format:
                logger.info(f"Prev intent=REPHRASE: Reusing skills {self._next_skills}")
                next_skills = self._next_skills
                next_format = self._next_format
                blend_mode = None
                role = self.interview_config.role_experience_required if self.interview_config else "mid"
                has_resume = self.has_resume_relevance(
                    resume_text, self.skill_tracker.mandatory_skills
                )
            else:
                blend_mode = decide_blend_mode(question_number)
                is_blend = blend_mode is not None
                role = self.interview_config.role_experience_required if self.interview_config else "mid"

                has_resume = self.has_resume_relevance(
                    resume_text, self.skill_tracker.mandatory_skills
                )
                next_format = self.format_tracker.get_next_format(
                    question_number=question_number + 1,
                    has_resume_relevance=has_resume,
                    is_blend_question=is_blend,
                    role=role
                )
                skill_count = decide_skill_count(question_number, self.skill_tracker, role)
                next_skills = self.skill_tracker.get_next_skills(
                    count=skill_count,
                    allow_repeat=self.skill_tracker.all_covered,
                    skill_states=context.skill_states if context else None
                )

                # CRITICAL FIX: If skills are exhausted (empty list), DO NOT CALL LLM.
                if not next_skills and self.skill_tracker.mandatory_skills:
                    logger.warning("⛔ ALL SKILLS EXHAUSTED (planner). Stopping.")
                    return {
                        "question_no": question_number + 1,
                        "next_question": "",
                        "response_id": f"resp_END_{int(time.time())}",
                        "metrics": {"api_latency_ms": 0}
                    }
            
            format_context = self._build_format_context(
                format_type=next_format,
                skills=next_skills,
                has_resume=has_resume,
                resume_text=resume_text,
                role=role
            )
            skill_context = self.skill_tracker.format_for_prompt(
                current_q=question_number,
                next_skills=next_skills,
                blend_mode=blend_mode
            )
            combined_context = f"{skill_context}\n{format_context}"
            
            current_skills = self.skill_tracker._current_skills
            remaining_questions = max(0, self._total_questions - question_number)
            required_format = next_format.value.replace('_', ' ').upper()
            
            self._next_skills = next_skills
            self._next_format = next_format
        else:
            # Pre-Q3 logic for tracking setup
            if self.skill_tracker and hasattr(self, '_next_skills'):
                next_skills = self.skill_tracker.get_next_skills(
                    count=3,
                    skill_states=context.skill_states if context else None
                )
            else:
                next_skills = ["relevant skills"]
                
            is_round_1 = not context or getattr(context, 'current_round', 1) == 1
            if is_round_1:
                role = self.interview_config.role_experience_required.lower() if self.interview_config else "mid"
                if role == "junior":
                    experience_instruction = f"FORMAT THIS QUESTION CONVERSATIONALLY like: 'Tell me about your experience in projects using technologies like {', '.join(next_skills)}...'"
                else:
                    experience_instruction = f"FORMAT THIS QUESTION CONVERSATIONALLY like: 'Tell me about your previous experience using technologies like {', '.join(next_skills)}...'"
                    
                combined_context = (
                    "📋 INTRODUCTORY TECHNICAL QUESTION (Q2):\n"
                    f"For this very first technical question, your REQUIRED FORMAT is DIRECT EXPERIENCE.\n"
                    f"{experience_instruction}\n"
                    "CRITICAL: Do NOT use formal phrasing like 'Can you provide an overview of your experience with...'. Keep it extremely simple, conversational, and direct."
                )
            else:
                combined_context = "📋 Skill and format tracking starts at Q3"
                
            current_skills = []
            remaining_questions = max(0, self._total_questions - question_number)
            required_format = "DIRECT EXPERIENCE"
            self._next_skills = next_skills
            self._next_format = None
        
        # ─── Build planner prompt ───
        asked_questions_summary = "None yet - this is your first question!"
        if self.skill_tracker and question_number >= 3:
            asked_questions_summary = self.skill_tracker.get_asked_questions_summary()
        
        next_question_number = question_number + 1 if intent.upper() in ("OTHERS", "SKIP") else question_number
        
        # Dynamic System Directive
        system_directive = ""
        if intent.upper() == "SKIP":
            if _skip_retry:
                # Re-asking the SAME skill, easier — do NOT change topic.
                system_directive = (
                    "⚠️ The candidate didn't know that one. Ask a SIMPLER, more foundational "
                    "question on the SAME topic — lower the bar and keep it approachable. "
                    "Do NOT switch to a different skill."
                )
            else:
                system_directive = "⚠️ SYSTEM ALERT: CANDIDATE SKIPPED. CHANGE TOPIC IMMEDIATELY."

        # Build question budget string for planner prompt
        question_budget = ""
        if (self.interview_config and 
            getattr(self.interview_config, 'jd_question_count', None) is not None and
            getattr(self.interview_config, 'resume_question_count', None) is not None):
            jd_target = self.interview_config.jd_question_count
            resume_target = self.interview_config.resume_question_count
            concept_scenario_line = ""
            if (getattr(self.interview_config, 'conceptual_question_count', None) is not None and
                getattr(self.interview_config, 'scenario_question_count', None) is not None):
                c_target = self.interview_config.conceptual_question_count
                s_target = self.interview_config.scenario_question_count
                concept_scenario_line = f" ({c_target} concept + {s_target} scenario)"
            question_budget = (
                f"\n📊 QUESTION BUDGET: {jd_target} JD/skill questions{concept_scenario_line} + "
                f"{resume_target} resume questions = {jd_target + resume_target} technical total"
            )
        elif (self.interview_config and
              getattr(self.interview_config, 'conceptual_question_count', None) is not None and
              getattr(self.interview_config, 'scenario_question_count', None) is not None):
            c_target = self.interview_config.conceptual_question_count
            s_target = self.interview_config.scenario_question_count
            question_budget = (
                f"\n📊 FORMAT BUDGET: {c_target} concept questions + "
                f"{s_target} scenario questions"
            )

        formatted_planner = PLANNER_REMINDERS.format(
            skill_tracking_context=combined_context,
            current_question_text=current_question_text or "Unknown",
            current_question_number=question_number,
            current_skills=current_skills if current_skills else "N/A",
            user_message=user_message[:500],
            next_skills=", ".join(next_skills) if isinstance(next_skills, list) else next_skills,
            required_format=required_format,
            asked_questions_summary=asked_questions_summary,
            intent=intent,
            next_question_number=next_question_number,
            difficulty_instructions=self._get_vector_prompt_instructions(question_number, skills=next_skills, context=context, conceptual=(getattr(self, '_next_format', None) == QuestionFormat.CONCEPTUAL)),
            question_budget=question_budget
        )
        
        if system_directive:
            formatted_planner = f"{system_directive}\n\n{formatted_planner}"
        
        # ── Build Chat Completions messages (system + user split) ──
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": formatted_planner}
        ]
        input_char_count = len(PLANNER_SYSTEM_PROMPT) + len(formatted_planner)
        
        logger.info(
            f"🧠 PLANNER [ChatCompletions] prompt: {input_char_count} chars | "
            f"Q{question_number} → Q{next_question_number} | Skills={next_skills}"
        )
        
        # ─── Make API call via Chat Completions (NO conversation chain) ───
        start_time = time.time()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=300,
                temperature=TEMPERATURE,
                top_p=0.9,
            )
            api_latency = time.time() - start_time
            
            # Extract text from Chat Completions response format
            response_text = response.choices[0].message.content or ""
            response_id = response.id
            
            # Parse JSON
            clean_text = response_text.strip()
            if clean_text.startswith("```"):
                clean_text = re.sub(r'^```(?:json)?\s*', '', clean_text)
                clean_text = re.sub(r'```\s*$', '', clean_text).strip()
            
            try:
                parsed = json.loads(clean_text)
                next_question = parsed.get("next_question", "")
                question_no = parsed.get("question_no", next_question_number)
            except json.JSONDecodeError:
                next_question = clean_text
                question_no = next_question_number

            # CAMPUS Phase 3: prefer a curated bank question / validate on-target.
            next_question = self._campus_source_question(next_question, next_skills, question_no)

            metrics = {
                'model': self.model,
                'response_id': response_id,
                'api_latency_ms': round(api_latency * 1000, 2),
                'mode': 'planner',
                'success': True
            }
            if self.skill_tracker:
                metrics['coverage_pct'] = self.skill_tracker.coverage_pct()
            
            if hasattr(self, '_last_used_vector') and self._last_used_vector:
                v = self._last_used_vector
                metrics["vector"] = {
                    "A": v.abstraction_depth,
                    "C": v.constraint_pressure,
                    "Amb": v.ambiguity,
                    "S": v.scale_complexity
                }

            return {
                "question_no": question_no,
                "next_question": next_question,
                # IMPORTANT: Return None — Chat Completions response_id is NOT
                # compatible with Responses API's previous_response_id chain.
                # webrtc_interview.py must keep using the last valid Responses API ID.
                "response_id": None,
                "metrics": metrics
            }
            
        except Exception as e:
            logger.error(f"❌ PLANNER failed: {e}")
            metrics = {'success': False, 'error': str(e)}
            
            if hasattr(self, '_last_used_vector') and self._last_used_vector:
                v = self._last_used_vector
                metrics["vector"] = {
                    "A": v.abstraction_depth,
                    "C": v.constraint_pressure,
                    "Amb": v.ambiguity,
                    "S": v.scale_complexity
                }

            return {
                "question_no": next_question_number,
                "next_question": "Can you tell me more about your experience?",
                "response_id": previous_response_id,
                "metrics": metrics
            }

    def create_followup_planner_response(
        self,
        question_number: int,
        candidate_answer: str,
        previous_question: str,
        followup_topic: str,
        skills: list = None,
        room_id: str = "",
        process_id: int = 0,
        context=None
    ) -> Dict:
        """
        Follow-up planner — completely separate from the main planner.
        Uses different system prompt and prompt template.
        
        Args:
            question_number: Current question number
            candidate_answer: The answer that triggered the follow-up
            previous_question: The question that was asked
            followup_topic: Topic hint from the follow-up reflex
            skills: Skills being tested (reused from previous question)
            room_id: For logging
            process_id: For logging
            context: InterviewContext
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        
        logger.info(f"🔄 {log_prefix} FOLLOWUP PLANNER START | Q{question_number} | topic='{followup_topic[:60]}'")
        
        # Get abstraction level from last used vector
        abstraction_level = 2
        if hasattr(self, '_last_used_vector') and self._last_used_vector:
            abstraction_level = self._last_used_vector.abstraction_depth
        
        role = self.interview_config.role_experience_required if self.interview_config else "mid"
        
        # Build follow-up prompt (completely separate from main planner prompt)
        followup_instructions = compile_followup_prompt(
            followup_topic=followup_topic,
            candidate_answer=candidate_answer,
            previous_question=previous_question,
            abstraction_level=abstraction_level,
            role_experience_required=role,
            rotation_index=question_number,
        )
        
        skills_str = ", ".join(skills) if skills else "same skills as previous question"
        
        user_prompt = (
            f"Generate a FOLLOW-UP question for Q{question_number + 1}.\n\n"
            f"SKILLS TO TEST: {skills_str}\n\n"
            f"{followup_instructions}\n\n"
            f"Respond with ONLY valid JSON:\n"
            f'{{"question_no": {question_number + 1}, "next_question": "..."}}'
        )
        
        messages = [
            {"role": "system", "content": FOLLOWUP_PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ]
        
        input_char_count = len(FOLLOWUP_PLANNER_SYSTEM_PROMPT) + len(user_prompt)
        logger.info(
            f"🔄 FOLLOWUP PLANNER [ChatCompletions] prompt: {input_char_count} chars | "
            f"Q{question_number} → Q{question_number + 1} | Skills={skills}"
        )
        
        start_time = time.time()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=300,
                temperature=0.7,
                top_p=0.9,
            )
            api_latency = time.time() - start_time
            
            response_text = response.choices[0].message.content or ""
            response_id = response.id
            
            # Parse JSON
            clean_text = response_text.strip()
            if clean_text.startswith("```"):
                clean_text = re.sub(r'^```(?:json)?\s*', '', clean_text)
                clean_text = re.sub(r'```\s*$', '', clean_text).strip()
            
            try:
                parsed = json.loads(clean_text)
                next_question = parsed.get("next_question", "")
                question_no = parsed.get("question_no", question_number + 1)
            except json.JSONDecodeError:
                next_question = clean_text
                question_no = question_number + 1
            
            metrics = {
                'model': self.model,
                'response_id': response_id,
                'api_latency_ms': round(api_latency * 1000, 2),
                'mode': 'followup_planner',
                'success': True,
                'followup_topic': followup_topic,
            }
            
            logger.info(
                f"🔄 {log_prefix} FOLLOWUP PLANNER DONE | {api_latency*1000:.0f}ms | "
                f"Q{question_no}: '{next_question[:60]}...'"
            )
            
            return {
                "question_no": question_no,
                "next_question": next_question,
                "response_id": None,
                "metrics": metrics
            }
            
        except Exception as e:
            logger.error(f"❌ FOLLOWUP PLANNER failed: {e}")
            return {
                "question_no": question_number + 1,
                "next_question": "",
                "response_id": None,
                "metrics": {'success': False, 'error': str(e)}
            }

    def create_response(self, 
                       messages: List[Dict[str, str]] = None,
                       question_number: int = 0,
                       user_message: str = None,
                       previous_response_id: str = None,
                       session_id: str = None,
                       use_stateful: bool = True,
                       inject_reminders: bool = True,
                       current_question_text: str = "",
                       resume_text: str = "",
                       previous_intent: str = None,
                       simulation_mode: bool = False,
                       context=None,
                       ) -> Tuple[str, Dict, str]:
        """
        Create a response using the Responses API with skill and format tracking.
        Handles main conversation flow including optional simulation mode.
        """
        session_prefix = f"[Session: {session_id}] " if session_id else ""
        is_first_message = previous_response_id is None
        _skip_retry = []  # skills being re-asked easier after a SKIP (set in skill-selection)
        _skip_retry = []  # skills being re-asked easier after a SKIP (set in skill-selection)
        _skip_retry = []  # skills being re-asked easier after a SKIP (set in skill-selection)
        _skip_retry = []  # skills being re-asked easier after a SKIP (set in skill-selection)
        
        # Simulation Mode
        simulation_instruction = ""
        if simulation_mode:
            simulation_instruction = (
                "\n\n🚨 SIMULATION MODE ACTIVE:\n"
                "1. DO NOT use conversational padding like 'It sounds like...', 'Great...', or 'Moving on...'.\n"
                "2. Ask the question DIRECTLY and IMMEDIATELY.\n"
                "3. IF the format is 'Resume Project', YOU MUST reference the resume content provided in context.\n"
                "4. Output MUST be the raw question text only.\n"
            )
        
        if use_stateful and not is_first_message:
            if not user_message:
                raise ValueError("user_message required for stateful mode")
            
            # Build skill & format tracking context for Q3+ (track from Q3 when a
            # shape quota is configured; quota_needs_slot covers the tight edge).
            if self.skill_tracker and self.format_tracker and (
                question_number >= 3
                or (self.format_tracker._has_shape_quota() and question_number >= 2)
                or self.quota_needs_slot(question_number + 1)
            ):
                if self._next_skills and self._next_format:
                    self.skill_tracker.register_question(question_number, self._next_skills)
                    self.format_tracker.register_format(question_number, self._next_format)

                _skip_retry = self._skip_soften_reuse(previous_intent, context)
                if _skip_retry:
                    # 'I don't know' → lower difficulty and re-ask the SAME skill easier.
                    next_skills = _skip_retry
                    role = self.interview_config.role_experience_required if self.interview_config else "mid"
                    blend_mode = None
                    has_resume = self.has_resume_relevance(resume_text, self.skill_tracker.mandatory_skills)
                    next_format = self._next_format or self.format_tracker.get_next_format(
                        question_number=question_number + 1,
                        has_resume_relevance=has_resume,
                        is_blend_question=False,
                        role=role
                    )
                    logger.info(f"🔽 SKIP-soften: re-asking same skill(s) easier: {next_skills}")
                elif previous_intent and previous_intent.upper() == 'REPHRASE' and self._next_skills and self._next_format:
                    logger.info(f"Prev intent=REPHRASE: Reusing skills {self._next_skills}")
                    next_skills = self._next_skills
                    next_format = self._next_format
                    blend_mode = None
                    role = self.interview_config.role_experience_required if self.interview_config else "mid"
                    has_resume = self.has_resume_relevance(resume_text, self.skill_tracker.mandatory_skills)
                else:
                    blend_mode = decide_blend_mode(question_number)
                    is_blend = blend_mode is not None
                    role = self.interview_config.role_experience_required if self.interview_config else "mid"
                    
                    has_resume = self.has_resume_relevance(resume_text, self.skill_tracker.mandatory_skills)
                    next_format = self.format_tracker.get_next_format(
                        question_number=question_number + 1,
                        has_resume_relevance=has_resume,
                        is_blend_question=is_blend,
                        role=role
                    )
                    skill_count = decide_skill_count(question_number, self.skill_tracker, role)
                    if self.skill_tracker:
                        next_skills = self.skill_tracker.get_next_skills(
                            count=skill_count,
                            allow_repeat=self.skill_tracker.all_covered,
                            skill_states=context.skill_states if context else None
                        )
                    
                    if not next_skills and self.skill_tracker.mandatory_skills:
                        logger.warning("⛔ ALL SKILLS EXHAUSTED (create_response). Stopping.")
                        return "", {'success': True, 'end_of_interview': True}, None
                
                format_context = self._build_format_context(
                    format_type=next_format,
                    skills=next_skills,
                    has_resume=has_resume,
                    resume_text=resume_text,
                    role=role
                )
                skill_context = self.skill_tracker.format_for_prompt(
                    current_q=question_number,
                    next_skills=next_skills,
                    blend_mode=blend_mode
                )
                combined_context = f"{skill_context}\n{format_context}{simulation_instruction}"
                
                required_format = next_format.value.replace('_', ' ').upper()
                self._next_skills = next_skills
                self._next_format = next_format

                # Log for report
                self._question_log.append({
                    "q_num": question_number + 1,
                    "skills": list(next_skills) if next_skills else [],
                    "format": next_format.value if next_format else "direct_experience",
                    "difficulty": None,
                    "blend_mode": blend_mode,
                })
            else:
                # Pre-Q3 logic for tracking setup
                if self.skill_tracker and self.skill_tracker.mandatory_skills:
                    next_skills = self.skill_tracker.mandatory_skills[:3]
                else:
                    next_skills = ["relevant skills"]
                    
                is_round_1 = not context or getattr(context, 'current_round', 1) == 1
                if is_round_1:
                    role = self.interview_config.role_experience_required.lower() if self.interview_config else "mid"
                    if role == "junior":
                        experience_instruction = f"FORMAT THIS QUESTION CONVERSATIONALLY like: 'Tell me about your experience in projects using technologies like {', '.join(next_skills)}...'"
                    else:
                        experience_instruction = f"FORMAT THIS QUESTION CONVERSATIONALLY like: 'Tell me about your previous experience using technologies like {', '.join(next_skills)}...'"
                        
                    combined_context = (
                        "📋 INTRODUCTORY TECHNICAL QUESTION (Q2):\n"
                        f"For this very first technical question, your REQUIRED FORMAT is DIRECT EXPERIENCE.\n"
                        f"{experience_instruction}\n"
                        "CRITICAL: Do NOT use formal phrasing like 'Can you provide an overview of your experience with...'. Keep it extremely simple, conversational, and direct."
                    )
                else:
                    combined_context = "📋 Skill and format tracking starts at Q3"
                    
                required_format = "DIRECT EXPERIENCE"
                self._next_skills = next_skills
                self._next_format = None
                
                self._question_log.append({
                    "q_num": question_number + 1,
                    "skills": list(next_skills),
                    "format": "direct_experience",
                    "difficulty": None,
                    "blend_mode": None,
                })
        else:
            # First message or stateless
            combined_context = ""
            next_skills = []
            required_format = "N/A"

        # ─── Build prompt ───
        difficulty_instructions = self._get_vector_prompt_instructions(question_number, skills=next_skills, context=context, conceptual=(getattr(self, '_next_format', None) == QuestionFormat.CONCEPTUAL))
        
        if question_number == 0:
            system_prompt = (
                "You are an expert technical interviewer.\n"
                "Task: Start the interview with a welcoming greeting and ask the candidate to introduce themselves.\n"
                f"Context: {resume_text[:200]}...\n"
                f"{simulation_instruction}"
            )
        elif question_number < 3:
            system_prompt = (
                "You are an expert technical interviewer.\n"
                f"Task: Ask question {question_number} focusing on the candidate's Resume or Introduction.\n"
                f"Context: {resume_text[:500]}\n"
                f"Instructions: {difficulty_instructions}\n"
                f"{simulation_instruction}"
            )
        else:
             # Q3+ logic
            asked_questions_summary = "None yet - this is your first question!"
            if self.skill_tracker:
                asked_questions_summary = self.skill_tracker.get_asked_questions_summary()
            
            system_directive = ""
            if previous_intent and previous_intent.upper() == "SKIP":
                if _skip_retry:
                    system_directive = (
                        "⚠️ The candidate didn't know that one. Ask a SIMPLER, more foundational "
                        "question on the SAME topic — lower the bar and keep it approachable. "
                        "Do NOT switch to a different skill."
                    )
                else:
                    system_directive = "⚠️ SYSTEM ALERT: CANDIDATE SKIPPED. CHANGE TOPIC IMMEDIATELY."
            
            formatted_reminders = CRITICAL_REMINDERS.format(
                skill_tracking_context=combined_context,
                current_question_text=current_question_text or "Unknown",
                current_question_number=question_number,
                current_skills=next_skills if next_skills else "N/A", # Pass raw list/string, format handles representation?
                # Wait, format expects string usually? No, f-string inside.
                # In create_planner_response I used ", ".join. Here I passed next_skills directly?
                # Ideally pass string representation.
                # Let's fix this inline.
                user_message=user_message[:500] if user_message else "N/A",
                next_skills=", ".join(next_skills) if isinstance(next_skills, list) else str(next_skills),
                required_format=required_format,
                asked_questions_summary=asked_questions_summary,
                difficulty_instructions=difficulty_instructions
            )
            
            system_prompt = f"{system_directive}\n\n{formatted_reminders}\n\n{simulation_instruction}"
        
        input_data = [{"role": "user", "content": system_prompt}]
        
        # ─── Make API call ───
        start_time = time.time()
        try:
            api_params = {
                'model': self.model,
                'input': input_data,
                'max_output_tokens': 1000, 
                'temperature': 0.7 if not simulation_mode else 0.3,
                'top_p': 0.9,
                'truncation': 'auto',
                'service_tier': SERVICE_TIER,
                'store': True,
            }
            if previous_response_id:
                api_params['previous_response_id'] = previous_response_id
            
            response = self.client.responses.create(**api_params)
            api_latency = time.time() - start_time
            response_text = self._extract_text(response)
            response_id = getattr(response, 'id', None)
            
            metrics = {
                'model': self.model,
                'response_id': response_id,
                'api_latency_ms': round(api_latency * 1000, 2),
                'success': True
            }
            if self.skill_tracker:
                metrics['coverage_pct'] = self.skill_tracker.coverage_pct()
            
            if hasattr(self, '_last_used_vector') and self._last_used_vector:
                v = self._last_used_vector
                metrics["vector"] = {
                    "A": v.abstraction_depth,
                    "C": v.constraint_pressure,
                    "Amb": v.ambiguity,
                    "S": v.scale_complexity
                }
            
            return response_text, metrics, response_id

        except Exception as e:
            logger.error(f"❌ API failed: {e}")
            return f"Error: {e}", {'success': False}, None

    
    def update_tracker_after_response(self, intent: str):
        """Update skill tracker after getting bot response."""
        if self.skill_tracker and self._next_skills:
            self.skill_tracker.process_response(intent)
    
    def register_asked_question(self, question_number: int, question_text: str):
        """
        Register a question that was asked to prevent repetition.
        Call this AFTER you extract the question from the LLM response.
        
        Args:
            question_number: The question number
            question_text: The actual question text that was asked
        """
        if self.skill_tracker and question_text:
            # Check if too similar to previous questions
            is_similar, similar_to = self.skill_tracker.is_question_too_similar(question_text)
            
            if is_similar:
                logger.error(
                    f"🚨 DUPLICATE DETECTED! Q{question_number} is too similar to Q{similar_to}!"
                )
            
            # Register with skills
            self.skill_tracker.register_question(
                question_number=question_number,
                skills=self._next_skills if self._next_skills else [],
                question_text=question_text
            )
            
            logger.info(f"Registered Q{question_number}: {question_text[:80]}...")

    def _normalize_messages(self, messages: List[Dict]) -> List[Dict]:
        """Convert messages into format expected by Responses API."""
        normalized = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            if isinstance(content, list):
                content = " ".join(
                    c["text"] if isinstance(c, dict) and "text" in c else str(c)
                    for c in content
                )
            
            normalized.append({
                "type": "message",
                "role": role,
                "content": str(content)
            })
        
        return normalized

    def _extract_text(self, response) -> str:
        """Extract text from Response object."""
        try:
            if hasattr(response, 'output') and response.output:
                first_output = response.output[0]
                if hasattr(first_output, 'content') and first_output.content:
                    first_content = first_output.content[0]
                    if hasattr(first_content, 'text'):
                        return first_content.text
        except (IndexError, AttributeError) as e:
            logger.error(f"Error extracting text: {e}")
            return f"Error extracting response: {e}"
        
        return "No response text found"
    
    def get_coverage_report(self) -> Dict:
        """Get comprehensive tracking report."""
        report = {'error': 'No trackers initialized'}
        
        if self.skill_tracker:
            report = {
                'mandatory_skills': self.skill_tracker.mandatory_skills,
                'covered': self.skill_tracker.covered,
                'uncovered': self.skill_tracker.uncovered,
                'skipped': self.skill_tracker.skipped,
                'blacklisted': self.skill_tracker.blacklisted,
                'skip_counts': self.skill_tracker._skip_count,
                'coverage_pct': self.skill_tracker.coverage_pct(),
                'all_covered': self.skill_tracker.all_covered,
                'question_history': self.skill_tracker._question_history,
                'asked_questions': self.skill_tracker._asked_questions,  # NEW: Include asked questions
                'total_questions_asked': len(self.skill_tracker._asked_questions),
            }
        
        if self.format_tracker:
            report['format_tracking'] = self.format_tracker.get_status()
        
        return report