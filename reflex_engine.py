"""
Reflex Engine — Fast-lane intent detection and acknowledgement generation.

Uses Chat Completions API (stateless) with minimal context for <1.5s response time.
Handles ONLY intent classification and ack generation. Does NOT generate next questions,
perform skill tracking, or maintain conversation state.
"""

import os
import json
import time
import logging
from typing import Dict, Optional
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv
from config_loader import NUMBER_OF_QUESTIONS

# Load .env
env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger('ReflexEngine')

REFLEX_MODEL = os.getenv("REFLEX_MODEL", "gpt-4o-mini")
REFLEX_MAX_TOKENS = int(os.getenv("REFLEX_MAX_TOKENS", "120"))
REFLEX_TEMPERATURE = float(os.getenv("REFLEX_TEMPERATURE", "0.4"))

# Task 2: intent + answer-quality classification runs on a cheap nano-tier model.
# This drives cached-ack bucket selection (ack_audio.bucket_for), so the common path
# never spends a live ack-generation call. Live ack generation (generate_reflex /
# generate_ack_stream) stays on REFLEX_MODEL as a rare fallback when the ack bank misses.
INTENT_MODEL = os.getenv("INTENT_MODEL", "gpt-4.1-nano")


# =============================================================================
# SPLIT REFLEX ARCHITECTURE — Intent Engine + ACK Engine (fired in parallel)
# =============================================================================

INTENT_ONLY_SYSTEM_PROMPT = """You are an intent classifier for an AI job interview. Classify the candidate's response into EXACTLY one intent. You do NOT generate acknowledgements or questions.

CURRENT INTERVIEW STATE:
- Question #{question_number}: "{current_question}"

INTENT CLASSIFICATION RULES (follow this decision tree in order):

1. TECHNICAL ANSWER (even partial/incomplete):
   Response contains ANY technical or relevant answer content (mentions skills, technologies, experience, approach, methodology).
   → "others"
   IMPORTANT: Statements like "I will ask my team" or "I coordinated with my team" are TECHNICAL ANSWERS → "others"

2. META/CASUAL/CONVERSATIONAL QUESTION (no technical content at all):
   Candidate asking about salary, team, location, company, role, how many questions, culture, benefits, hiring process, or casual question.
   → "repeat"

3. CLARIFICATION REQUEST:
   "can you explain?", "what do you mean?", "didn't understand", "rephrase", "confused", "which project?", "which one?", "what context?", "referring to what?"
   → "rephrase"

4. AUDIO/CONNECTION CHECK:
   "hello?", "can you hear me?", "are you there?"
   → "repeat"

5. EXPLICIT REPEAT REQUEST:
   "repeat the question", "say again", "what was the question", "I didn't hear you", "come again"
   → "repeat"

6. SKIP REQUEST:
   "skip", "next question", "let's move on", "pass", "next", "move on", "I don't know", "no experience", "haven't worked with"
   → "skip"

7. FILLER/NONSENSE:
   Single words (NOT "skip"/"pass"/"next"), "um", "yeah", fragments < 5 words with no substance
   → "repeat"

8. EXIT REQUEST:
   "I want to end", "stop the interview", "I want to leave"
   → "exit"

9. TECHNICAL EXPLANATION REQUEST:
   "What is Docker?", "Can you explain REST API?", "Give me a hint"
   → "repeat"

BENEFIT OF DOUBT:
If response is >5 words, contains technical-sounding terms, and attempts to answer → "others"
Even brief/partial answers ("I use Python for data analysis") are VALID → "others"
"I don't know" or "I don't have experience" or "haven't worked with" = "skip"
If the user repeats themselves, ignore repetition and treat as valid answer → "others"

Respond with ONLY valid JSON, no markdown, no explanation:
{{"intent": "..."}}

Valid intents: "others", "repeat", "rephrase", "skip", "exit"
"""


# Task 2 — combined intent + answer-quality classifier (nano). One cheap call returns
# the intent AND, when the candidate actually answered, how well they did. The result
# selects a pre-rendered ack bucket, so no live ack is generated in the common path.
INTENT_QUALITY_SYSTEM_PROMPT = """You classify a candidate's reply in an AI interview. Return intent, answer quality, and your confidence. You do NOT write acknowledgements or questions.

QUESTION #{question_number}: "{current_question}"
{rubric_block}
STEP 1 — INTENT (pick exactly one):
- "others"   = the candidate ATTEMPTED to answer the question (any technical/relevant content, even brief or partial). Also: statements like "I coordinated with my team".
- "skip"     = "I don't know", "no experience", "haven't worked with", "skip", "pass", "next", "move on".
- "repeat"   = asked to repeat, audio/hearing check ("can you hear me"), a meta/casual question (salary, team, how many questions), filler/nonsense, or a request to explain a concept.
- "rephrase" = asked you to reword/clarify the QUESTION ("what do you mean", "can you rephrase", "which one").
- "exit"     = wants to stop/leave the interview.

STEP 2 — QUALITY (only when intent="others"; otherwise use "na"):
- "good"    = a correct, substantive, on-topic answer that covers the main idea{rubric_good_hint}.
- "partial" = on the right track but incomplete, vague, or only partly correct. WHEN UNSURE between good and wrong, choose "partial".
- "wrong"   = clearly off-topic or incorrect for what was asked.
Be conservative: only say "good" when you are genuinely confident it is right; only say "wrong" when it is clearly incorrect. Otherwise "partial".

STEP 3 — CONFIDENCE: a number 0.0–1.0 for how sure you are of the QUALITY label (use ~0.4 or below when unsure; intent can still be confident).

Respond with ONLY valid JSON, no markdown:
{{"intent": "...", "quality": "...", "confidence": 0.0}}

Valid intent: "others","repeat","rephrase","skip","exit". Valid quality: "good","partial","wrong","na"."""


ACK_STREAM_SYSTEM_PROMPT = """You are an acknowledgement generator for an AI job interview.
You generate WARM, NATURAL, HUMAN-SOUNDING acknowledgements. You do NOT generate questions. You do NOT classify intent (that's already done).

JOB DESCRIPTION (summary):
{jd_summary}

MANDATORY SKILLS BEING ASSESSED:
{mandatory_skills}

{company_context_block}

CURRENT INTERVIEW STATE:
- Question #{question_number} was just asked
- The question was: "{current_question}"
- The candidate's intent has been classified as: "{intent}"

GENERATE an acknowledgement based on the intent:

IF intent="others" (candidate answered the question):
- Generate a warm, natural ack (1-2 sentences MAXIMUM, ~15-25 words).
- START with a quick conversational filler: "Hmm, interesting —", "Right, okay —", "Yeah, that makes sense —", "Gotcha —", "Nice —"
- Follow with ONE brief reflection on something SPECIFIC they said (a tech name, approach, experience) — no performance evaluation.
- That's it. Do NOT add wrap-ups, transitions, or filler beyond that.
- NEVER mention or hint at any specific next topic.
- If their response ALSO contains a meta question (salary, team, etc.): Include a brief answer using JD info.
- Examples: "Hmm interesting, so you've been working with PyTorch for fine-tuning — nice.", "Right, gotcha — microservices with Docker, that's solid.", "Yeah, that makes sense — building out the CI/CD pipeline from scratch, cool."

IF intent="repeat" (meta question, audio check, repeat request, filler, or tech explanation request):
- Answer their question in 1-2 short sentences max, then pivot back naturally.
- If salary: "Compensation details will be discussed in later rounds."
- If number of questions: "This interview has about {number_of_questions} questions."
- If role/tech/company: Answer briefly using JD summary.
- If audio check: Quick warm assurance.
- If repeat request: Be understanding, brief.
- If filler/nonsense: Quick nudge back.
- If tech explanation: Gently decline briefly.
- Pivot phrasing examples: "Anyway, let me go through that one again —", "So getting back to it —", "Alright, let's jump back in."

IF intent="rephrase" (clarification request):
- 1-2 sentences max: "No worries at all — let me put that differently for you."

IF intent="skip" (skip request):
- 1-2 sentences, warm and brief: "Totally fine — let's move on and find something more in your wheelhouse."

IF intent="exit":
- Output NOTHING (empty string).

CRITICAL RULES:
- Output ONLY the ack text — NO JSON, NO formatting, NO labels, NO "ack:" prefix.
- ack must NEVER contain questions (no "?", no "Can you...", no "Tell me...")
- ack = pure acknowledgement/statement only
- Sound NATURAL and HUMAN — like a friendly colleague chatting, not a corporate bot.
- NEVER answer any technical question or explain technical concepts.
- NEVER comment on the user's performance or tell them whether they are right or wrong.
- Just MOTIVATE the user and transition smoothly.
- NEVER reference or guess what the next question topic will be.

{recent_acks_block}
"""


REFLEX_SYSTEM_PROMPT = """You are an intent classifier and acknowledgement generator for an AI job interview.

Your job is to classify the candidate's response and generate a WARM, NATURAL, HUMAN-SOUNDING acknowledgement. You do NOT generate questions.
You are a friendly, empathetic interviewer — NOT a robot. Your acks should sound like a real person reacting to what they just heard.

JOB DESCRIPTION (summary):
{jd_summary}

MANDATORY SKILLS BEING ASSESSED:
{mandatory_skills}

{company_context_block}

CURRENT INTERVIEW STATE:
- Question #{question_number} was just asked
- The question was: "{current_question}"

INTENT CLASSIFICATION RULES (follow this decision tree in order):

1. TECHNICAL ANSWER (even partial/incomplete):
   Does the response contain ANY technical or relevant answer content?
   (mentions skills, technologies, experience, approach, methodology, "I don't know", "I don't have experience")
   → intent = "others"
   → If response ALSO contains a meta question (salary, team, location, etc.): Include a brief answer to the meta question in your ack using the JD info above.

2. META/CASUAL/CONVERSATIONAL QUESTION (no technical content at all):
   Is the candidate asking a casual question, or asking about: salary, team, location, company, role, how many questions, culture, benefits, hiring process?
   → intent = "repeat"
   → 1-2 sentences max: Answer their question briefly, then pivot back naturally.
     - If salary: "Compensation details will be discussed in later rounds."
     - If number of questions: "This interview has about {number_of_questions} questions."
     - If role/tech/company: Answer briefly using JD summary.
     - If how you are doing: "I'm doing great, thanks for asking!"
     AFTER answering, pivot back. Vary phrasing: "Anyway, let me go through that again —", "So getting back to it —"
   IMPORTANT: Statements like "I will ask my team" or "I coordinated with my team" are TECHNICAL ANSWERS (intent="others"), NOT meta questions.

3. CLARIFICATION REQUEST:
   "can you explain?", "what do you mean?", "didn't understand", "rephrase", "confused", "which project?", "which one?", "what context?", "referring to what?"
   → intent = "rephrase"
   → 1-2 sentences: "No worries — let me put that differently for you."

4. AUDIO/CONNECTION CHECK:
   "hello?", "can you hear me?", "are you there?"
   → intent = "repeat"
   → 1-2 sentences: "Yep, you're coming through perfectly clear! Let me just go through that question one more time."

5. EXPLICIT REPEAT REQUEST:
   "repeat the question", "say again", "what was the question", "I didn't hear you", "come again"
   → intent = "repeat"
   → 1-2 sentences: "Sure thing — let me go through that one more time for you."

6. SKIP REQUEST:
   "skip", "next question", "let's move on", "pass", "next", "move on", "I don't know", "no experience", "haven't worked with"
   → intent = "skip"
   → 1-2 sentences, warm and brief: "Totally fine — let's move on and find something more in your wheelhouse."

7. FILLER/NONSENSE:
   Single words (NOT "skip"/"pass"/"next"), "um", "yeah", fragments < 5 words with no substance
   → intent = "repeat"
   → 1-2 sentences: "Hmm, I think you might have gotten cut off there — let me just go through that one more time."

8. EXIT REQUEST:
   "I want to end", "stop the interview", "I want to leave"
   → intent = "exit"
   → ack = ""

9. TECHNICAL EXPLANATION REQUEST:
   "What is Docker?", "Can you explain REST API?", "Give me a hint"
   → intent = "repeat"
   → 1-2 sentences: "Ha, I wish I could help — but just share whatever comes to mind! Let me repeat the question."

CRITICAL RULES FOR ACK:
- ack must NEVER contain questions (no "?", no "Can you...", no "Tell me...")
- ack = pure acknowledgement/statement only
- Keep ack SHORT: 1-2 sentences maximum, ~15-25 words. Brevity is key.
- Sound NATURAL and HUMAN — like a friendly colleague, not a corporate bot.
- NEVER answer any technical question or explain technical concepts.
- NEVER comment on the user's performance or tell them whether they are right or wrong.
- START with a quick conversational filler: "Hmm interesting —", "Right, gotcha —", "Yeah, that makes sense —", "Nice —"
- Follow with ONE brief reflection on something SPECIFIC they said — no evaluating.
- That's it. Do NOT add wrap-ups, transitions, or extra filler.
- NEVER reference or guess what the next question topic will be.

{recent_acks_block}

BENEFIT OF DOUBT:
If response is >5 words, contains technical-sounding terms, and attempts to answer → intent = "others"
Even brief/partial answers ("I use Python for data analysis") are VALID → intent = "others"
"I don't know" or "I don't have experience" or "haven't worked with" = intent = "skip"

REPETITION/STUTTERING:
If the user repeats themselves, ignore the repetition and treat it as a valid answer → intent = "others".

Respond with ONLY valid JSON, no markdown, no explanation:
{{"intent": "...", "ack": "..."}}

Valid intents: "others", "repeat", "rephrase", "skip", "exit"
"""


class ReflexEngine:
    """
    Fast-lane conversational reflex for intent detection and ack generation.
    
    Uses Chat Completions API (stateless) with minimal context.
    Target latency: <1.5 seconds.
    """
    
    def __init__(self, api_key: str = None, interview_config=None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=self.api_key)
        self.model = REFLEX_MODEL           # ack generation (rare fallback / special flows)
        self.intent_model = INTENT_MODEL    # cheap nano classification (Task 2)
        self.interview_config = interview_config
    
    def generate_reflex(
        self,
        transcript: str,
        current_question: str,
        jd_summary: str,
        mandatory_skills: list,
        question_number: int = 1,
        company_context: str = None,
        recent_acks: list = None,
        spoken_filler: str = None,  # deprecated, kept for backward compat
        room_id: str = "",
        process_id: int = 0
    ) -> Dict[str, str]:
        """
        Generate intent and acknowledgement from candidate's response.
        
        Args:
            transcript: The candidate's transcribed response
            current_question: The question that was just asked
            jd_summary: Job description (first 800 chars used)
            mandatory_skills: List of mandatory skills being assessed
            question_number: Current question number
            company_context: Optional company/culture context
            room_id: For logging
            process_id: For logging
            
        Returns:
            Dict with keys: "intent", "ack"
            Fallback: {"intent": "others", "ack": "Hmm, okay — I appreciate you sharing that."}
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        
        start_time = time.time()
        
        # Build minimal context
        skills_str = ", ".join(mandatory_skills) if isinstance(mandatory_skills, list) else str(mandatory_skills)
        jd_trimmed = jd_summary[:800] if jd_summary else "Not available"
        
        company_block = ""
        if company_context:
            company_block = f"COMPANY CONTEXT:\n{company_context[:300]}"
        
        # Build recent acks block to prevent repetition
        recent_acks_block = ""
        if recent_acks and len(recent_acks) > 0:
            ack_list = "\n".join(f"  - \"{a}\"" for a in recent_acks[-3:])
            recent_acks_block = f"AVOID REPEATING these recent acks (use different phrasing):\n{ack_list}"
        
        system_prompt = REFLEX_SYSTEM_PROMPT.format(
            jd_summary=jd_trimmed,
            mandatory_skills=skills_str,
            company_context_block=company_block,
            question_number=question_number,
            current_question=current_question[:300],
            number_of_questions=self.interview_config.questions_per_round if self.interview_config else NUMBER_OF_QUESTIONS,
            recent_acks_block=recent_acks_block,
        )
        
        # Estimate input tokens (~4 chars per token)
        input_estimate = (len(system_prompt) + len(transcript[:500])) // 4
        
        logger.info(
            f"⚡ {log_prefix} REFLEX START | Q{question_number} | "
            f"~{input_estimate} input tokens | transcript: '{transcript[:80]}...'"
        )
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"}
                ],
                max_tokens=REFLEX_MAX_TOKENS,
                temperature=REFLEX_TEMPERATURE,
                response_format={"type": "json_object"}
            )
            
            latency = time.time() - start_time
            raw_text = response.choices[0].message.content.strip()
            
            # Parse JSON
            result = json.loads(raw_text)
            intent = result.get("intent", "others")
            ack = result.get("ack", "Hmm, okay \u2014 I appreciate you sharing that. That gives me a good sense of your thinking on this, which is really helpful.")
            
            # Validate intent
            valid_intents = {"others", "repeat", "rephrase", "skip", "exit"}
            if intent not in valid_intents:
                logger.warning(
                    f"⚠️ {log_prefix} REFLEX invalid intent '{intent}', defaulting to 'others'"
                )
                intent = "others"
            
            # Validate ack has no questions
            if "?" in ack and intent not in ("exit",):
                # Smart strip: only remove trailing question marks
                # If there's a question mark in the middle, it might be rhetorical "Really? That's great."
                # But we definitely don't want the ack to END with a question.
                if ack.strip().endswith("?"):
                    ack = ack.strip()[:-1] + "."
                    logger.info(f"🔧 {log_prefix} REFLEX stripped trailing question mark")
                elif "?" in ack:
                     logger.info(f"ℹ️ {log_prefix} REFLEX kept internal question mark: '{ack}'")
            
            # Hard cap ack length (prevent JD dumps) — raised to 400 for longer natural acks
            if len(ack) > 400:
                # Truncate at last sentence boundary within limit
                truncated = ack[:400]
                last_period = truncated.rfind(".")
                if last_period > 50:
                    ack = truncated[:last_period + 1]
                else:
                    ack = truncated.rstrip() + "."
                logger.info(f"✂️ {log_prefix} REFLEX ack truncated to {len(ack)} chars")
            
            logger.info(
                f"⚡ {log_prefix} REFLEX DONE | {latency*1000:.0f}ms | "
                f"intent={intent} | ack='{ack[:80]}...'"
            )
            
            return {"intent": intent, "ack": ack}
            
        except json.JSONDecodeError as e:
            latency = time.time() - start_time
            logger.error(
                f"❌ {log_prefix} REFLEX JSON parse failed ({latency*1000:.0f}ms): {e} | raw: '{raw_text[:200]}'"
            )
            return self._fallback_reflex(transcript, log_prefix)
            
        except Exception as e:
            latency = time.time() - start_time
            logger.error(
                f"❌ {log_prefix} REFLEX failed ({latency*1000:.0f}ms): "
                f"{type(e).__name__}: {e}",
                exc_info=True
            )
            return self._fallback_reflex(transcript, log_prefix)
    
    def _fallback_reflex(self, transcript: str, log_prefix: str) -> Dict[str, str]:
        """Deterministic fallback when API call fails."""
        transcript_lower = transcript.strip().lower() if transcript else ""
        
        # Simple keyword-based fallback
        # Simple keyword-based fallback
        token_count = len(transcript.split())
        
        # KEY CHANGE: Benefit of Doubt
        # If response is substantial (>8 words) and contains technical terms or "but"/"however",
        # treat as "others" even if it says "don't have experience".
        # Example: "I don't have direct experience with AWS but I've used GCP." -> OTHERS (partial answer)
        has_pivot = any(kw in transcript_lower for kw in ["but", "however", "although", "except", "instead"])
        
        if any(kw in transcript_lower for kw in ["hello", "hear me", "are you there"]):
            intent = "repeat"
            ack = "Yep, you're coming through perfectly clear, don't worry about a thing! Sometimes these virtual setups can be a bit finicky, but we're all good on my end. Let me just go through that question one more time for you."
            
        elif any(kw in transcript_lower for kw in ["repeat", "say again", "say that again", "what was the question"]):
            intent = "repeat"
            ack = "Oh sure, absolutely — no problem at all! I know sometimes these audio calls can make it a bit hard to catch everything the first time around. Let me just go through that one more time for you."
            
        elif any(kw in transcript_lower for kw in ["skip", "next question", "move on", "pass"]):
            # Explicit skip is always skip
            intent = "skip"
            ack = "Hey, that's absolutely fine — not everyone has had the chance to work with every technology out there, and that's totally okay. There's no expectation that you'd know everything. Let's move on to something else and find an area where you can really shine."
            
        elif any(kw in transcript_lower for kw in ["end", "stop", "leave", "exit"]):
            intent = "exit"
            ack = ""
            
        elif any(kw in transcript_lower for kw in ["explain", "clarify", "understand", "rephrase", "confused", "which project", "context"]):
            intent = "rephrase"
            ack = "Oh absolutely, no worries at all — I probably could have framed that a bit better. Let me try to put that in a different way so it's a bit clearer for you."
            
        elif any(kw in transcript_lower for kw in ["don't know", "no experience", "haven't used", "not familiar"]):
            # Check for pivot or length before skipping
            if has_pivot or token_count > 10:
                intent = "others" 
                ack = "Right, okay — I appreciate you being upfront about that. It sounds like you've still got some relevant perspective to share there, which is really helpful context to have."
                logger.info(f"🔄 {log_prefix} REFLEX Fallback: 'don't know' + pivot/length = OTHERS")
            else:
                intent = "skip"
                ack = "That's completely okay, don't stress about it at all! Everyone has different areas of expertise, and there's zero judgment here. Let's explore a different topic where you might feel more at home."
                
        else:
            intent = "others"
            ack = "Hmm, okay — I appreciate you sharing that. That gives me a good sense of your perspective on this, which is really helpful."
        
        logger.warning(f"🔄 {log_prefix} REFLEX using fallback: intent={intent}")
        return {"intent": intent, "ack": ack}

    # =========================================================================
    # SPLIT REFLEX ARCHITECTURE — Intent Engine + ACK Engine
    # =========================================================================

    def generate_intent_only(
        self,
        transcript: str,
        current_question: str,
        question_number: int = 1,
        room_id: str = "",
        process_id: int = 0
    ) -> Dict[str, str]:
        """
        Fast intent-only classification. No ack generation.
        
        Uses a minimal prompt with NO JD context, NO skills, and max_tokens=20
        to achieve sub-500ms response time.
        
        Returns:
            Dict with key: "intent"
            Fallback: {"intent": "others"}
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        start_time = time.time()
        
        system_prompt = INTENT_ONLY_SYSTEM_PROMPT.format(
            question_number=question_number,
            current_question=current_question[:200],
        )
        
        logger.info(
            f"⚡ {log_prefix} INTENT ENGINE START | Q{question_number} | "
            f"transcript: '{transcript[:60]}...'"
        )
        
        try:
            response = self.client.chat.completions.create(
                model=self.intent_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"}
                ],
                max_tokens=20,
                temperature=0.2,
                response_format={"type": "json_object"}
            )

            latency = time.time() - start_time
            raw_text = response.choices[0].message.content.strip()
            result = json.loads(raw_text)
            intent = result.get("intent", "others")

            valid_intents = {"others", "repeat", "rephrase", "skip", "exit"}
            if intent not in valid_intents:
                logger.warning(f"⚠️ {log_prefix} INTENT ENGINE invalid '{intent}', defaulting to 'others'")
                intent = "others"

            logger.info(
                f"⚡ {log_prefix} INTENT ENGINE DONE | {latency*1000:.0f}ms | intent={intent}"
            )

            return {"intent": intent}

        except Exception as e:
            latency = time.time() - start_time
            logger.error(
                f"❌ {log_prefix} INTENT ENGINE failed ({latency*1000:.0f}ms): "
                f"{type(e).__name__}: {e}"
            )
            # Use deterministic fallback for intent
            return {"intent": self._fallback_intent(transcript, log_prefix)}

    def generate_intent_quality(
        self,
        transcript: str,
        current_question: str,
        question_number: int = 1,
        rubric: str = None,
        room_id: str = "",
        process_id: int = 0,
    ) -> Dict[str, object]:
        """Task 2: one cheap nano call → {intent, quality, confidence}.

        `quality` (good/partial/wrong/na) is only meaningful when the candidate answered
        (intent="others"); it selects the ack bucket via ack_audio.bucket_for. Passing a
        `rubric` (available for curated bank questions) makes the good/partial/wrong call
        far more reliable. On any failure, returns a low-confidence result so the caller
        routes to the neutral ack bucket.
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        start_time = time.time()

        rubric_block = f'EXPECTED (rubric): "{rubric[:200]}"\n' if rubric else ""
        rubric_good_hint = " — it should hit the rubric points above" if rubric else ""
        system_prompt = INTENT_QUALITY_SYSTEM_PROMPT.format(
            question_number=question_number,
            current_question=current_question[:250],
            rubric_block=rubric_block,
            rubric_good_hint=rubric_good_hint,
        )

        valid_intents = {"others", "repeat", "rephrase", "skip", "exit"}
        valid_quality = {"good", "partial", "wrong", "na"}
        try:
            response = self.client.chat.completions.create(
                model=self.intent_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"},
                ],
                max_tokens=40,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            latency = time.time() - start_time
            raw_text = response.choices[0].message.content.strip()
            result = json.loads(raw_text)

            intent = str(result.get("intent", "others")).lower().strip()
            if intent not in valid_intents:
                intent = "others"
            quality = str(result.get("quality", "na")).lower().strip()
            if quality not in valid_quality:
                quality = "na"
            if intent != "others":
                quality = "na"
            try:
                confidence = float(result.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = max(0.0, min(1.0, confidence))

            logger.info(
                f"⚡ {log_prefix} INTENT+QUALITY DONE | {latency*1000:.0f}ms | "
                f"intent={intent} quality={quality} conf={confidence:.2f}"
            )
            return {"intent": intent, "quality": quality, "confidence": confidence}

        except Exception as e:
            latency = time.time() - start_time
            logger.error(
                f"❌ {log_prefix} INTENT+QUALITY failed ({latency*1000:.0f}ms): "
                f"{type(e).__name__}: {e}"
            )
            intent = self._fallback_intent(transcript, log_prefix)
            # Low confidence → neutral ack bucket (never a wrongly-toned cached ack).
            return {"intent": intent, "quality": "na", "confidence": 0.3}

    def _fallback_intent(self, transcript: str, log_prefix: str) -> str:
        """Deterministic fallback when intent API call fails."""
        transcript_lower = transcript.strip().lower() if transcript else ""
        token_count = len(transcript.split())
        has_pivot = any(kw in transcript_lower for kw in ["but", "however", "although", "except", "instead"])
        
        if any(kw in transcript_lower for kw in ["hello", "hear me", "are you there"]):
            return "repeat"
        elif any(kw in transcript_lower for kw in ["repeat", "say again", "say that again", "what was the question"]):
            return "repeat"
        elif any(kw in transcript_lower for kw in ["skip", "next question", "move on", "pass"]):
            return "skip"
        elif any(kw in transcript_lower for kw in ["end", "stop", "leave", "exit"]):
            return "exit"
        elif any(kw in transcript_lower for kw in ["explain", "clarify", "understand", "rephrase", "confused", "which project", "context"]):
            return "rephrase"
        elif any(kw in transcript_lower for kw in ["don't know", "no experience", "haven't used", "not familiar"]):
            if has_pivot or token_count > 10:
                return "others"
            return "skip"
        else:
            return "others"

    def generate_ack_stream(
        self,
        transcript: str,
        current_question: str,
        intent: str,
        jd_summary: str,
        mandatory_skills: list,
        question_number: int = 1,
        company_context: str = None,
        recent_acks: list = None,
        room_id: str = "",
        process_id: int = 0
    ):
        """
        Stream acknowledgement text token-by-token.
        
        Uses stream=True on ChatCompletions API. Output is PLAIN TEXT, not JSON.
        Yields text chunks as they arrive from GPT.
        
        Args:
            transcript: The candidate's transcribed response
            current_question: The question that was just asked
            intent: Pre-classified intent from generate_intent_only()
            jd_summary: Job description (first 800 chars used)
            mandatory_skills: List of mandatory skills being assessed
            question_number: Current question number
            company_context: Optional company/culture context
            recent_acks: Recent acks for anti-repetition
            room_id: For logging
            process_id: For logging
            
        Yields:
            Text chunks (str) as they arrive from GPT streaming
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        start_time = time.time()
        
        # Don't generate ack for exit intent
        if intent == "exit":
            return
        
        skills_str = ", ".join(mandatory_skills) if isinstance(mandatory_skills, list) else str(mandatory_skills)
        jd_trimmed = jd_summary[:800] if jd_summary else "Not available"
        
        company_block = ""
        if company_context:
            company_block = f"COMPANY CONTEXT:\n{company_context[:300]}"
        
        recent_acks_block = ""
        if recent_acks and len(recent_acks) > 0:
            ack_list = "\n".join(f'  - "{a}"' for a in recent_acks[-3:])
            recent_acks_block = f"AVOID REPEATING these recent acks (use different phrasing):\n{ack_list}"
        
        system_prompt = ACK_STREAM_SYSTEM_PROMPT.format(
            jd_summary=jd_trimmed,
            mandatory_skills=skills_str,
            company_context_block=company_block,
            question_number=question_number,
            current_question=current_question[:300],
            intent=intent,
            number_of_questions=self.interview_config.questions_per_round if self.interview_config else NUMBER_OF_QUESTIONS,
            recent_acks_block=recent_acks_block,
        )
        
        logger.info(
            f"🔊 {log_prefix} ACK STREAM START | Q{question_number} | intent={intent}"
        )
        
        first_token_time = None
        total_text = ""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"}
                ],
                max_tokens=REFLEX_MAX_TOKENS,
                temperature=REFLEX_TEMPERATURE,
                stream=True,
                # NO response_format — plain text output for streaming
            )
            
            for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    
                    if first_token_time is None:
                        first_token_time = time.time()
                        ttft = first_token_time - start_time
                        logger.info(
                            f"⚡ {log_prefix} ACK STREAM TTFT: {ttft*1000:.0f}ms"
                        )
                    
                    total_text += text
                    yield text
            
            latency = time.time() - start_time
            logger.info(
                f"🔊 {log_prefix} ACK STREAM DONE | {latency*1000:.0f}ms | "
                f"total_chars={len(total_text)} | ack='{total_text[:80]}...'"
            )
            
        except Exception as e:
            latency = time.time() - start_time
            logger.error(
                f"❌ {log_prefix} ACK STREAM failed ({latency*1000:.0f}ms): "
                f"{type(e).__name__}: {e}"
            )
            # Yield deterministic fallback ack
            fallback = self._fallback_reflex(transcript, log_prefix)
            yield fallback.get("ack", "Hmm, okay — I appreciate you sharing that.")

    def generate_followup_reflex(
        self,
        transcript: str,
        current_question: str,
        mandatory_skills: list,
        question_number: int = 1,
        abstraction_level: int = 1,
        recent_acks: list = None,
        room_id: str = "",
        process_id: int = 0
    ) -> Dict[str, str]:
        """
        Follow-up reflex — fired in PARALLEL with main reflex.
        
        Analyzes the candidate's answer to determine if a follow-up question 
        would be valuable (vague claims, interesting details, incomplete reasoning).
        
        Returns:
            Dict with keys: "followup_worthy" (bool), "ack" (str), "followup_topic" (str)
            Fallback: {"followup_worthy": False, "ack": "", "followup_topic": ""}
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        start_time = time.time()
        
        skills_str = ", ".join(mandatory_skills) if isinstance(mandatory_skills, list) else str(mandatory_skills)
        
        # Build recent acks block
        recent_acks_block = ""
        if recent_acks and len(recent_acks) > 0:
            ack_list = "\n".join(f'  - "{a}"' for a in recent_acks[-3:])
            recent_acks_block = f"AVOID REPEATING these recent acks (use different phrasing):\n{ack_list}"
        
        system_prompt = f"""You are an interview depth analyzer. Your job is to decide whether the candidate's answer deserves a FOLLOW-UP question or whether we should move to a completely new topic.

CURRENT INTERVIEW STATE:
- Question #{question_number}: "{current_question[:300]}"
- Skills being tested: {skills_str}
- Difficulty level: {abstraction_level}/5

ANALYZE the candidate's answer and decide:

1. followup_worthy = TRUE if ANY of these apply:
   - Candidate made a VAGUE CLAIM that needs specific evidence ("I've used Docker" → ask HOW)
   - Candidate mentioned an INTERESTING TECHNICAL DETAIL worth exploring deeper
   - Candidate gave INCOMPLETE REASONING — they started explaining but didn't finish
   - Candidate made a BOLD ASSERTION that should be challenged ("This is always the best approach")
   - Candidate described a project or experience worth probing ("Tell me more about that")
   - At higher difficulty levels (3+), candidate's answer can be tested with edge cases or scale problems

2. followup_worthy = FALSE if ANY of these apply:
   - Candidate gave a THOROUGH, COMPLETE answer that covers the topic well
   - Candidate clearly DOESN'T KNOW the topic (said "I don't know", "no experience")
   - Candidate gave a very SHORT response with nothing to dig into
   - The question was at difficulty level 1 (basic recall) — no depth needed

If followup_worthy is TRUE, also provide:
- ack: A warm, appreciative acknowledgement (15-20 words). This is NOT a question — it's a natural human reaction that smoothly leads into the follow-up.
  
  STRUCTURE: [Appreciation/reaction to their answer] + [Natural lead-in that connects to the follow-up]
  
  ABSOLUTE RULES:
  1. NEVER include a question mark or question phrasing ("Can you", "Could you", "What about", "How did you")
  2. MUST appreciate or react positively to something SPECIFIC they said (a technology, concept, approach, or experience)
  3. Keep it 15-20 words. Not shorter, not longer.
  4. END with a natural transition that ALIGNS with the follow-up question direction:
     Good endings: "— let's dig into that a bit.", "— I'd like to explore that further.", "— that's worth unpacking."
  5. NEVER say: "shift gears", "move on", "next topic", "different area", "switch to", "new question"
     These are FORBIDDEN because this is a FOLLOW-UP, not a topic change.
  
  PERFECT examples (appreciate + lead-in):
    - "That's a really solid approach with Redis for caching — let's dig into that a bit more."
    - "Nice, using LoRA for fine-tuning is a smart choice — I want to explore that further."
    - "Right, that makes a lot of sense for handling scale — let's unpack that."
    - "Good thinking with the event-driven architecture there — that's worth exploring deeper."
    - "Interesting, so you went with microservices for that — let's dive into the details."
    - "That's a practical way to handle the deployment pipeline — I'm curious about the specifics."
  
  BAD examples (FORBIDDEN):
    - "Ah, interesting — so you went with Redis." ← TOO SHORT, no lead-in
    - "Let's shift gears and talk about something else." ← FORBIDDEN PHRASE, this is a follow-up not a topic change
    - "Great, let's move on to the next area." ← FORBIDDEN, sounds like topic change
    - "Can you tell me more about that?" ← IS A QUESTION
    - "Good answer." ← TOO GENERIC
    - "That's interesting — I'd love to hear more about how you select and prepare those datasets for tuning." ← TOO LONG, contains a question

- followup_topic: A 1-line hint describing WHAT specifically to follow up on (e.g., "their Redis caching eviction strategy", "whether their approach scales to 100K users")

{recent_acks_block}

IMPORTANT: Be selective. Not every answer deserves a follow-up. Only choose TRUE if there's genuine technical depth to explore.

Respond with ONLY valid JSON:
{{"followup_worthy": true/false, "ack": "...", "followup_topic": "..."}}"""
        
        logger.info(
            f"🔄 {log_prefix} FOLLOWUP REFLEX START | Q{question_number} | "
            f"level={abstraction_level}"
        )
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"}
                ],
                max_tokens=REFLEX_MAX_TOKENS,
                temperature=REFLEX_TEMPERATURE,
                response_format={"type": "json_object"}
            )
            
            latency = time.time() - start_time
            raw_text = response.choices[0].message.content.strip()
            result = json.loads(raw_text)
            
            followup_worthy = bool(result.get("followup_worthy", False))
            ack = result.get("ack", "")
            followup_topic = result.get("followup_topic", "")
            
            # Validate: strip trailing question marks from ack
            if ack and ack.strip().endswith("?"):
                ack = ack.strip()[:-1] + "."
            
            # Hard cap ack length (max ~20 words ≈ 180 chars)
            if ack and len(ack) > 180:
                truncated = ack[:180]
                last_period = truncated.rfind(".")
                last_dash = truncated.rfind("—")
                cut = max(last_period, last_dash)
                if cut > 30:
                    ack = truncated[:cut + 1]
                else:
                    ack = truncated.rstrip() + "."
            
            logger.info(
                f"🔄 {log_prefix} FOLLOWUP REFLEX DONE | {latency*1000:.0f}ms | "
                f"worthy={followup_worthy} | topic='{followup_topic[:60]}'"
            )
            
            return {
                "followup_worthy": followup_worthy,
                "ack": ack,
                "followup_topic": followup_topic
            }
            
        except Exception as e:
            latency = time.time() - start_time
            logger.error(
                f"❌ {log_prefix} FOLLOWUP REFLEX FAILED ({latency*1000:.0f}ms): {e}"
            )
            return {"followup_worthy": False, "ack": "", "followup_topic": ""}

    def generate_q1_reflex(
        self,
        transcript: str,
        current_question: str,
        jd_summary: str,
        round_number: int = 1,
        room_id: str = "",
        process_id: int = 0
    ) -> Dict[str, str]:
        """
        Dedicated reflex for Q1 (instructions confirmation).
        
        Q1 now delivers interview instructions and asks the candidate to confirm
        they understand. This reflex classifies their confirmation and generates
        a warm comfort greeting + "how has your day been?" as the ack.
        
        Returns: Dict with keys: "intent", "ack"
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        start_time = time.time()

        q1_system_prompt = f"""You are an AI interview assistant. The interviewer just explained the interview instructions and format to the candidate and asked them to confirm they understood.
IMPORTANT NOTE: You must NEVER sound robotic. You should feel very humanic, conversational, and empathetic. Do not evaluate user performance.

The instructions message was: "{current_question[:300]}"
Job role context: {jd_summary[:400]}

The candidate just responded. Classify their intent:

1. "others" — This is the DEFAULT and MOST COMMON intent. Use it for ALL of the following:
   a) Confirmation/Readiness: (e.g., "Yes", "Okay", "Got it", "Sure", "I understand", "Ready", "Let's go", "Sounds good", "I'm ready", "Yeah", "Alright", "Perfect", "Cool", "No problem", "Fine", "That's clear", "Understood")
   b) Confusion/Repeat requests: (e.g., "I didn't understand", "Can you repeat that?", "What did you say?", "Come again?", "Explain again", "Sorry I didn't get that", "Pardon?")
   c) Questions about the interview, role, process, or audio: (e.g., "How many questions?", "What topics?", "Am I audible?", "Can you hear me?", "Tell me about the role", "What company is this?", "Hello?", "Are you there?")
   d) Any other conversational response whatsoever
2. "exit" — ONLY if they explicitly want to leave (e.g., "I want to end this", "Stop the interview").

CRITICAL RULE: intent MUST be "others" for EVERYTHING except explicit exit requests. The greeting instructions should NEVER be repeated — always move the conversation forward.

For ack:
- First, determine which sub-case (a, b, c, or d) the candidate's response falls into:

- CASE A — They CONFIRMED understanding or expressed readiness:
  - Generate a SHORT, warm transition (1-2 sentences, ~15-25 words) + end with asking how their day has been.
  - Keep it breezy — like a quick friendly comment, not a speech.
  - Examples:
    - "Awesome, glad that's clear! Just be yourself and take your time. So, how has your day been so far?"
    - "Perfect, sounds good! Think of this as a relaxed chat. So, how's your day going?"

- CASE B — They DID NOT UNDERSTAND or asked to repeat the instructions:
  - 1-2 sentences: quick simplified recap + ask about their day.
  - Example:
    - "No problem! I'll ask questions, you reply verbally — takes about {self.interview_config.max_duration_minutes if self.interview_config else 30} minutes. So, how has your day been?"

- CASE C — They asked a SPECIFIC QUESTION (audio check, role info, process, etc.):
  - 1-2 sentences: answer briefly + ask about their day.
  - Examples:
    - "Yep, you're coming through perfectly clear! So, how has your day been so far?"
    - "This interview has about {self.interview_config.questions_per_round if self.interview_config else 15} questions. So, how's your day going?"

- CASE D — Any other conversational response:
  - 1 sentence: respond naturally, then ask about their day.

- If intent="exit": ack = ""

Respond in JSON: {{"intent": "...", "ack": "..."}}"""
        
        logger.info(
            f"⚡ {log_prefix} Q1 REFLEX START | transcript: '{transcript[:80]}...'"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": q1_system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"}
                ],
                max_tokens=120,
                temperature=0.4,
                response_format={"type": "json_object"}
            )

            latency = time.time() - start_time
            raw_text = response.choices[0].message.content.strip()
            result = json.loads(raw_text)
            intent = result.get("intent", "others")
            ack = result.get("ack", "Great, glad that's clear! This is going to be a relaxed, conversational session — so just be yourself and take your time. So, how has your day been so far?")

            # Validate intent — Q1 only allows "others" or "exit"
            # Greeting should NEVER be repeated, so normalize everything to "others"
            if intent not in {"others", "exit"}:
                logger.info(
                    f"🔧 {log_prefix} Q1 REFLEX: Normalizing intent='{intent}' → 'others' "
                    f"(greeting must never be repeated)"
                )
                intent = "others"

            logger.info(
                f"⚡ {log_prefix} Q1 REFLEX DONE | {latency*1000:.0f}ms | "
                f"intent={intent} | ack='{ack[:80]}...'"
            )

            return {"intent": intent, "ack": ack}


        except Exception as e:
            latency = time.time() - start_time
            logger.error(f"❌ {log_prefix} Q1 REFLEX FAILED ({latency*1000:.0f}ms): {e}")
            return {"intent": "others", "ack": "Great, glad that's clear! This is going to be a relaxed, conversational session — so just be yourself and take your time. So, how has your day been so far?"}

    def generate_greeting_reflex(
        self,
        transcript: str,
        room_id: str,
        process_id: str
    ) -> dict:
        """
        Dedicated reflex for handling the candidate's response to the initial 
        'how has your day been so far?' greeting.
        
        Returns: Dict with keys: "intent", "ack"
        """
        log_prefix = f"[process id: {process_id}][room id: {room_id}]"
        start_time = time.time()

        greeting_system_prompt = """You are an AI interview assistant. The interviewer just warmly asked the candidate: "how has your day been so far?"
IMPORTANT NOTE: You must NEVER sound robotic. You should feel very humanic, conversational, and empathetic. Do not evaluate user performance.
The candidate just responded.

Classify their intent:
1. "others" — This is the MOST COMMON case. They answered the question (e.g., "Good, thanks", "Doing well", "It's been a busy day", "I'm great, how are you?").
2. "repeat" — They didn't hear you or asked you to repeat (e.g., "What did you say?", "Can you repeat that?").
3. "question" — They asked an unrelated question or a casual conversational question.
4. "exit" — They want to leave.

For ack (ONLY when intent="others"):
- Generate a SHORT, warm acknowledgment (1-2 sentences MAXIMUM, ~15-25 words).
- Sound like a real friendly colleague — not a bot reading a script.
- If they said their day is good: Quick warm reaction, then transition.
- If they asked how YOU are doing: Quick natural response first ("Doing great myself, thanks!"), then transition.
- Transition to interview naturally but briefly.
- Do NOT ask any follow-up questions.
- NEVER say "let me repeat the question" because there is no technical question yet.

GOOD EXAMPLES:
  - "Oh nice, glad to hear it! Alright, let's jump into things."
  - "Awesome, sounds like a good day! Let's get started."
  - "Doing great myself, thanks for asking! Alright, let's dive in."

BAD EXAMPLES (too long — NEVER do this):
  - "Oh that's really nice to hear! I always love when someone comes into an interview in a good mood — it honestly makes such a difference..." ← WAY too long

If intent="repeat": ack = "No worries! I was just asking how your day's been — just breaking the ice a little."
If intent="question": ack = 1-2 sentences answering their question briefly, then transitioning.
If intent="exit": ack = ""

Respond in JSON: {"intent": "...", "ack": "..."}"""
        
        logger.info(
            f"⚡ {log_prefix} GREETING REFLEX START | transcript: '{transcript[:80]}...'"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": greeting_system_prompt},
                    {"role": "user", "content": f"CANDIDATE SAID: {transcript[:500]}"}
                ],
                max_tokens=100,
                temperature=0.4,
                response_format={"type": "json_object"}
            )

            latency = time.time() - start_time
            raw_text = response.choices[0].message.content.strip()
            result = json.loads(raw_text)
            intent = result.get("intent", "others")
            ack = result.get("ack", "Oh nice, glad to hear it! Alright, let's jump into things.")

            if intent not in {"others", "repeat", "question", "exit"}:
                intent = "others"

            logger.info(
                f"⚡ {log_prefix} GREETING REFLEX DONE | {latency*1000:.0f}ms | "
                f"intent={intent} | ack='{ack[:80]}...'"
            )

            return {"intent": intent, "ack": ack}

        except Exception as e:
            latency = time.time() - start_time
            logger.error(f"❌ {log_prefix} GREETING REFLEX FAILED ({latency*1000:.0f}ms): {e}")
            return {"intent": "others", "ack": "Oh nice, glad you're doing well! Alright, let's get into it."}

