import json
import logging
from typing import List, Optional
from openai import OpenAI
from .models import SemanticDeviationResult, CandidateSemanticProfile

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an expert at detecting whether a job interview answer was spoken naturally by a human or read/pasted from an AI tool like ChatGPT.

You are analyzing SPOKEN answers captured via speech-to-text. The candidate is answering technical questions in a live video interview.

═══════════════════════════════════════════════════════════════════════════════
STRONG SIGNALS THAT THE ANSWER IS HUMAN (spoken naturally):
═══════════════════════════════════════════════════════════════════════════════
1. SPEECH DISFLUENCIES: "um", "uh", "like", "you know", "basically", "so", "yeah"
2. SELF-CORRECTIONS: "I mean", "actually wait", "let me rephrase", "sorry I meant"
3. REPETITIONS: "we we would", "I I think", "so so basically"
4. INCOMPLETE THOUGHTS: sentences that trail off, topic drift mid-answer
5. INFORMAL LANGUAGE: "gonna", "wanna", "kinda", "stuff like that", "and all"
6. PERSONAL EXPERIENCE: "In my previous company...", "When I was working on...", "My team and I..."
7. SPECIFIC DETAILS: team names, company names, dates, real numbers, project names
8. NATURAL UNCERTAINTY: "I think", "I'm not 100% sure but", "if I remember correctly"
9. CONVERSATIONAL TONE: "Yeah so", "Okay so", "Right", addressing the interviewer
10. MESSY GRAMMAR (from speech-to-text): missing punctuation, run-on sentences, wrong words

═══════════════════════════════════════════════════════════════════════════════
STRONG SIGNALS THAT THE ANSWER IS AI-GENERATED (copy-pasted):
═══════════════════════════════════════════════════════════════════════════════
1. PERFECT STRUCTURE: numbered lists, bullet points, clear intro-body-conclusion
2. FORMAL ACADEMIC TONE: "Furthermore", "Additionally", "In conclusion", "Moreover"
3. COMPREHENSIVE COVERAGE: covers ALL aspects of a topic with equal depth (too thorough)
4. NO DISFLUENCIES AT ALL: zero "um", "uh", filler words in a long answer
5. TEXTBOOK DEFINITIONS: explains concepts generally without personal context
6. BUZZWORD HEAVY: "scalable", "robust", "best practices", "industry standard" without specifics
7. NO PERSONAL STORIES: explains what something IS but not how THEY used it
8. CONSISTENT STYLE THROUGHOUT: no variation in tone, same level of polish from start to end
9. SUDDEN STYLE CHANGE: prior answers were messy/informal, this one is suddenly perfect
10. READS LIKE DOCUMENTATION: sounds like it was copied from an article, blog, or Stack Overflow

═══════════════════════════════════════════════════════════════════════════════
CRITICAL CALIBRATION RULES:
═══════════════════════════════════════════════════════════════════════════════
- An answer with 3+ speech disfluencies MUST get ai_probability <= 0.25
- An answer with zero disfluencies AND perfect structure MUST get ai_probability >= 0.70
- Personal stories with specific details (names, dates, problems solved) are STRONG human signals
- Technical answers CAN be detailed AND human — look for HOW they explain, not WHAT they explain
- Short answers (1-3 sentences) are HARDER to judge — default to moderate (0.4-0.5)
- If prior answers were messy but THIS answer is suddenly polished → increase suspicion by +0.2

═══════════════════════════════════════════════════════════════════════════════
SCORING GUIDE:
═══════════════════════════════════════════════════════════════════════════════
ai_probability (0.0 = definitely human, 1.0 = definitely AI):
  0.00-0.15: Very human — multiple disfluencies, personal stories, messy grammar
  0.15-0.30: Likely human — some natural speech markers, conversational tone
  0.30-0.45: Uncertain — few markers either way, or mixed signals
  0.45-0.60: Mildly suspicious — too polished for speech, few human markers
  0.60-0.80: Suspicious — structured, formal, no disfluencies, textbook-like
  0.80-1.00: Very likely AI — perfect structure, academic tone, comprehensive lists

polish_score (0.0 = messy/natural, 1.0 = highly polished/suspicious):
  How polished and structured is the language? Perfect grammar + structure = high.

human_markers (0.0 = no human signals, 1.0 = many human signals):
  Count of human speech indicators: disfluencies, self-corrections, informal tone.

Return ONLY valid JSON:
{
  "ai_probability": <float 0.0-1.0>,
  "polish_score": <float 0.0-1.0>,
  "human_markers": <float 0.0-1.0>,
  "resume_alignment": <float 0.0-1.0>,
  "observations": [<max 3 short strings explaining your reasoning>],
  "uncertainty_note": "<string>"
}"""


def build_user_prompt(
    question: str,
    current_answer: str,
    prior_answers: List[str],
    resume_text: Optional[str] = None
) -> str:
    # Use last 5 prior answers for context
    if prior_answers:
        history_items = prior_answers[-5:]
        history = "\n---\n".join(
            f"[Answer {i+1}]: {a[:500]}" for i, a in enumerate(history_items)
        )
    else:
        history = "(No prior answers available — this is the first answer)"
    
    resume_section = ""
    if resume_text and len(resume_text.strip()) > 50:
        resume_preview = resume_text[:1000] + "..." if len(resume_text) > 1000 else resume_text
        resume_section = f"""
CANDIDATE'S RESUME (for verification):
{resume_preview}

Check: If they discuss a project from their resume, do they show PERSONAL knowledge 
(specific challenges, team details, technical decisions they made)?
Generic explanation of their OWN project = suspicious."""
    else:
        resume_section = "(No resume available)"
    
    return f"""QUESTION ASKED:
{question}

CURRENT ANSWER TO ANALYZE:
{current_answer}

PRIOR ANSWERS FROM SAME CANDIDATE (for style comparison):
{history}

{resume_section}

YOUR TASK:
1. Count human speech markers: disfluencies, filler words, self-corrections, informal phrasing
2. Check for AI markers: perfect structure, formal tone, numbered lists, textbook definitions
3. Compare style with prior answers — if prior answers were messy but this one is perfect = SUSPICIOUS
4. Score ai_probability, polish_score, human_markers, resume_alignment
5. Provide max 3 key observations explaining your reasoning"""


async def analyze_semantic_deviation(
    question: str,
    current_answer: str,
    prior_answers: List[str],
    resume_text: Optional[str] = None,
    model: str = "gpt-4o"
) -> Optional[SemanticDeviationResult]:

    if len(current_answer.split()) < 15:
        return None  # Not enough signal

    if not prior_answers:
        return None  # No baseline to compare against

    client = OpenAI()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(question, current_answer, prior_answers, resume_text)}
            ],
            temperature=0.1,  # Lower temp for more consistent scoring
            response_format={"type": "json_object"},
            max_tokens=500
        )

        parsed = json.loads(response.choices[0].message.content)
    except Exception as e:
        logger.error(f"Semantic analyzer API call failed: {e}")
        return None

    # Extract scores with validation
    ai_probability = parsed.get("ai_probability")
    polish_score = parsed.get("polish_score")
    human_markers = parsed.get("human_markers")
    resume_alignment = parsed.get("resume_alignment", 0.5)
    
    # Fallback to legacy field names
    if ai_probability is None:
        ai_probability = 1.0 - parsed.get("self_consistency", 0.5)
    if polish_score is None:
        polish_score = parsed.get("conceptual_novelty", 0.5)
    if human_markers is None:
        human_markers = parsed.get("semantic_alignment", 0.5)

    if ai_probability is None or polish_score is None or human_markers is None:
        logger.warning(f"LLM response missing required fields: {list(parsed.keys())}")
        return None

    # Clamp values to valid range
    ai_probability = max(0.0, min(1.0, float(ai_probability)))
    polish_score = max(0.0, min(1.0, float(polish_score)))
    human_markers = max(0.0, min(1.0, float(human_markers)))
    resume_alignment = max(0.0, min(1.0, float(resume_alignment)))

    # ── Cross-validation: ensure internal consistency ─────────
    # If GPT says high human markers but also high AI probability, something is off
    # Trust human_markers more (concrete signal) over ai_probability (judgment call)
    if human_markers >= 0.7 and ai_probability > 0.5:
        logger.info(
            f"Cross-validation correction: human_markers={human_markers:.2f} "
            f"but ai_prob={ai_probability:.2f} — reducing ai_prob"
        )
        ai_probability = max(0.15, ai_probability * 0.5)
    
    if human_markers <= 0.2 and ai_probability < 0.4:
        logger.info(
            f"Cross-validation correction: human_markers={human_markers:.2f} "
            f"but ai_prob={ai_probability:.2f} — increasing ai_prob"
        )
        ai_probability = min(0.85, ai_probability + 0.25)

    # Resume mismatch penalty
    if resume_alignment < 0.3:
        ai_probability = min(1.0, ai_probability + 0.15)
        logger.info(f"Resume mismatch (alignment={resume_alignment:.2f}), boosting ai_probability")

    logger.info(
        f"Semantic analysis: ai_prob={ai_probability:.2f} polish={polish_score:.2f} "
        f"human={human_markers:.2f} resume={resume_alignment:.2f} | "
        f"observations={parsed.get('observations', [])}"
    )

    # Map to legacy interface
    return SemanticDeviationResult(
        self_consistency=float(1.0 - ai_probability),
        conceptual_novelty=float(polish_score),
        semantic_alignment=float(human_markers),
        observations=parsed.get("observations", [])[:3],
        uncertainty_note=parsed.get("uncertainty_note", "")
    )
