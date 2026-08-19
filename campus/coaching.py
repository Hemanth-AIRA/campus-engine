"""
Practice-mode COACHING — the learning loop that turns an interview into prep.

Only active in PRACTICE mode (CampusConfig.gives_hints / gives_model_answers; force-off in
ASSESSMENT). Two moves:

  • HINT        — when a student struggles, nudge them toward the idea WITHOUT giving the
                  answer away, so they can try again (pairs with Phase-2 easier re-ask).
  • MODEL ANSWER — after a weak reply, a short ideal answer + why, so they learn what
                  "good" looks like before moving on.

Prompt builders are pure (testable); the generate_* helpers make the LLM call and never
raise (coaching is best-effort and must never break the interview).
"""
import logging

logger = logging.getLogger("campus_coaching")

_HINT_SYS = (
    "You are a supportive interview-prep coach for a college student. Give ONE short hint "
    "(1-2 sentences) that nudges the student toward the key idea WITHOUT stating the answer. "
    "Encourage them to try again. No preamble, no 'Sure!'. Spoken aloud, so plain sentences."
)
_MODEL_SYS = (
    "You are a supportive interview-prep coach for a college student. In 2-4 sentences, give a "
    "concise MODEL ANSWER to the question and briefly say WHY it's good, so the student learns "
    "what a strong answer looks like. Grade-appropriate, encouraging. Spoken aloud, plain sentences."
)


def build_hint_prompt(question: str, skill: str, difficulty_level: int = 2):
    user = (
        f"Skill being practiced: {skill}\n"
        f"Question the student is stuck on:\n{question}\n\n"
        f"Give a single encouraging hint that points at the core idea (difficulty level "
        f"{difficulty_level}/5) without revealing the full answer."
    )
    return _HINT_SYS, user


def build_model_answer_prompt(question: str, skill: str, student_answer: str = ""):
    sa = (student_answer or "").strip()
    user = (
        f"Skill: {skill}\n"
        f"Question:\n{question}\n\n"
        + (f"The student's answer was:\n{sa}\n\n" if sa else "")
        + "Give a short model answer and one line on why it's strong."
    )
    return _MODEL_SYS, user


def _chat(client, system: str, user: str, model: str, max_tokens: int = 180) -> str:
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.3,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def generate_hint(client, question: str, skill: str, difficulty_level: int = 2,
                  model: str = "gpt-4o-mini") -> str:
    """Best-effort hint; returns '' on any failure (never breaks the interview)."""
    try:
        sys_p, user_p = build_hint_prompt(question, skill, difficulty_level)
        return _chat(client, sys_p, user_p, model)
    except Exception as e:
        logger.warning(f"hint generation failed (non-blocking): {e}")
        return ""


def generate_model_answer(client, question: str, skill: str, student_answer: str = "",
                          model: str = "gpt-4o-mini") -> str:
    try:
        sys_p, user_p = build_model_answer_prompt(question, skill, student_answer)
        return _chat(client, sys_p, user_p, model)
    except Exception as e:
        logger.warning(f"model-answer generation failed (non-blocking): {e}")
        return ""
