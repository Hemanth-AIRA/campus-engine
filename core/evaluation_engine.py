"""
Phase 3A — Per-Question Evaluation Engine

Pure evaluation module. No shared state. No DB access.
Returns structured evaluation dict or safe fallback on any error.
"""

import json
import logging
import os
from openai import OpenAI

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _client

EVALUATION_PROMPT = """You are a professional interview evaluator.

Given a question and candidate answer, produce a JSON evaluation.
{job_context}
QUESTION: {question}
SKILL BEING TESTED: {skill}
CANDIDATE ANSWER: {answer}

Return ONLY valid JSON matching this exact schema:
{{
  "skill": "string — the skill being tested",
  "summary": "string — 1-2 sentence evaluation of the answer quality",
  "confidence_score": 0.0,
  "evaluation_tags": ["string — e.g. 'partial', 'strong', 'off-topic', 'practical', 'theoretical'"],
  "concept_tags": ["string — 2-4 noun phrases representing specific topics covered in the answer"],
  "projects_mentioned": [
    {{
      "name": "string — project or experience name",
      "tech_stack": ["string — tools, equipment, or technologies used"],
      "ownership": "string — 'led', 'contributed', 'observed', 'unknown'",
      "production_exposure": true,
      "scale_context": "string — brief description of scope, volume, or environment"
    }}
  ]
}}

RULES:
- confidence_score: float 0.0 to 1.0 (0=no competence demonstrated, 1=expert mastery)
- If answer is vague or deflected, score low (0.1-0.3)
- If answer shows real experience, score higher (0.5-0.8)
- If answer is expert-level with depth, score high (0.8-0.95)
- projects_mentioned: empty list [] if no specific projects or experiences discussed
- evaluation_tags: at least one tag describing the answer quality
- Return ONLY JSON, no markdown, no explanation"""

# Safe fallback when anything goes wrong
_FALLBACK = {
    "skill": "",
    "summary": "",
    "confidence_score": 0.5,
    "evaluation_tags": ["evaluation_failed"],
    "concept_tags": [],
    "projects_mentioned": [],
}


def _clamp_confidence(score) -> float:
    """Clamp confidence to [0.05, 0.95] with type safety."""
    if not isinstance(score, (int, float)):
        return 0.5
    return max(0.05, min(0.95, float(score)))


def _validate_result(result: dict) -> dict:
    """Validate and normalize evaluation result. Fix any schema issues."""
    if not isinstance(result, dict):
        return dict(_FALLBACK)

    # Ensure required keys exist
    result.setdefault("skill", "")
    result.setdefault("summary", "")
    result.setdefault("evaluation_tags", [])
    result.setdefault("concept_tags", [])
    result.setdefault("projects_mentioned", [])

    # Clamp confidence
    result["confidence_score"] = _clamp_confidence(result.get("confidence_score"))

    # Validate tags
    if not isinstance(result["evaluation_tags"], list):
        result["evaluation_tags"] = []
    
    if not isinstance(result.get("concept_tags"), list):
        result["concept_tags"] = []

    # Validate projects
    if not isinstance(result["projects_mentioned"], list):
        result["projects_mentioned"] = []

    return result


def evaluate_answer(
    skill: str,
    question_text: str,
    answer_text: str,
    round_number: int,
    job_context: str = "",
) -> dict:
    """
    Call LLM to evaluate a single Q/A pair.

    Returns validated evaluation dict.
    On ANY failure, returns safe fallback (never raises).
    """
    if not answer_text or not answer_text.strip():
        fallback = dict(_FALLBACK)
        fallback["skill"] = skill
        fallback["summary"] = "No answer provided."
        fallback["confidence_score"] = 0.05
        fallback["evaluation_tags"] = ["no_answer"]
        return fallback

    try:
        prompt = EVALUATION_PROMPT.format(
            question=question_text,
            skill=skill,
            answer=answer_text[:2000],
            job_context=f"\nROLE CONTEXT: {job_context}\n" if job_context else "",
        )

        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=500,
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)
        return _validate_result(result)

    except json.JSONDecodeError as e:
        logger.warning("⚠️ Evaluation JSON parse failed: %s", e)
        fallback = dict(_FALLBACK)
        fallback["skill"] = skill
        return fallback

    except Exception as e:
        logger.error("❌ Evaluation LLM call failed: %s", e, exc_info=True)
        fallback = dict(_FALLBACK)
        fallback["skill"] = skill
        return fallback


# ═══════════════════════════════════════════════════════════════════════════
# Question Quality Audit — checks if the generated question matches its
# intended difficulty vector (A, C, Amb, S) and target skills.
# ═══════════════════════════════════════════════════════════════════════════

_VECTOR_DESCRIPTIONS = {
    "A": {
        1: "CONCEPT RECALL — asks 'What is / How does / Explain'. Answerable in 2-3 sentences. No trade-offs required.",
        2: "REASONING — asks 'Why would you / How would you decide / What happens when'. Requires cause-and-effect logic.",
        3: "TRADE-OFFS — presents TWO named alternatives and forces a choice. For JUNIOR: compare tools or approaches. For MID: compare strategies or methods. For SENIOR: compare architectures or frameworks.",
        4: "ARCHITECTURE — presents a real scenario with 2-3 specific options. Forces an explicit choice, demands accountability and failure analysis.",
    },
    "C": {
        0: "NO CONSTRAINTS — open-ended, no resource or performance limits mentioned.",
        1: "SINGLE CONSTRAINT — exactly ONE explicit measurable limit (a specific number, deadline, or resource cap) from ANY category (time, budget, capacity, team, accuracy, data, compatibility, regulatory).",
        2: "COMPETING CONSTRAINTS — exactly TWO specific, measurable, conflicting limits from DIFFERENT categories.",
        3: "MULTI-FACTOR — THREE or more simultaneous compounding constraints from DIFFERENT categories.",
    },
    "Amb": {
        0: "CLEAR — all specs provided upfront. No intentional omissions.",
        1: "PARTIAL INFO — ONE critical detail intentionally omitted (scope, timing, accuracy, environment, data, or constraints). Presented as if complete.",
        2: "UNDERSPECIFIED — question sounds like a non-specialist. No method, tools, scope, or metrics given.",
    },
    "S": {
        0: "SMALL — single unit, local environment, minimal volume. No scale numbers required.",
        1: "MODERATE — a specific scale number must appear (volume, caseload, throughput, dataset size, or workload).",
        2: "HIGH VOLUME — a specific throughput number must appear (cases/day, requests/minute, events/hour, etc.).",
        3: "LARGE-SCALE OPERATIONS — specific operational parameters must appear (multi-site, SLA, compliance, high-volume production).",
    },
}

AUDIT_PROMPT = """You are a quality auditor for AI-generated interview questions.

QUESTION TO AUDIT:
"{question}"

INTENDED SKILLS TO TEST: {skills}
ROLE LEVEL: {role}
BASE DIFFICULTY: {base_difficulty}

INTENDED DIFFICULTY VECTOR:
  A (Abstraction Depth) = {a} → {a_desc}
  C (Constraint Pressure) = {c} → {c_desc}
  Amb (Ambiguity) = {amb} → {amb_desc}
  S (Scale Complexity) = {s} → {s_desc}

TASK: Evaluate whether the question ACTUALLY matches each dimension and the target skills.

ROLE-CALIBRATION RULES (critical for accurate auditing):
- JUNIOR + A=1 or A=2: If the question asks about a concept, reasoning, or a 'how/why', it PASSES A.
  Do NOT fail A just because the topic (e.g., 'attention mechanisms', 'Docker') sounds advanced.
  A junior CAN be asked to EXPLAIN a concept (A=1) or reason about WHY (A=2) on any topic.
  Only FAIL A if the question requires ARCHITECTURAL DECISIONS, SYSTEM DESIGN, or TRADE-OFFS at A=1-2.
- JUNIOR + A=3: The trade-off should compare TOOLS or LIBRARIES (e.g., pandas vs PySpark).
  Architecture-level trade-offs FAIL. Tool-level trade-offs PASS.
- MID + A=3: Design pattern or strategy trade-offs are appropriate. PASS.
- SENIOR + A>=3: System-level architectural trade-offs expected. PASS.

CONSTRAINT CALIBRATION (be GENEROUS at C=0-1):
- C=0: ALWAYS PASS. No constraint needed. If there happens to be one, still pass.
- C=1: PASS if the question contains ANY limiting condition. This includes:
  * Specific numbers ('2GB RAM', '$500/month', '100ms latency')
  * Hard rules ('no external dependencies', 'must be explainable')
  * Contextual limits ('with limited data', 'on a single machine', 'with a small team')
  * Implied constraints ('in production', 'without downtime', 'real-time')
  * Resource references ('low-resource environment', 'budget-constrained')
  ONLY fail C=1 if there is ZERO limiting condition of any kind.
- C=2: Needs 2 distinct constraints (numbers, rules, or strong contextual limits).
- C=3: Needs 3+ compounding constraints.

SCALE CALIBRATION (be GENEROUS at S=0-1):
- S=0: ALWAYS PASS. No scale reference needed.
- S=1: PASS if ANY scale context exists — specific numbers ('10K users'), 
  contextual references ('production', 'at scale', 'enterprise', 'large dataset'),
  or implied scale ('millions of records', 'high traffic').
  ONLY fail S=1 if the question has ZERO scale reference of any kind.
- S>=2: Needs explicit throughput numbers or production parameters.

For each dimension, check:
- A: Does the cognitive demand match the level, ADJUSTED FOR ROLE as described above?
- C: Does the question contain constraints matching the level (see calibration above)?
- Amb: Does the question have the right amount of intentional ambiguity?
- S: Does the question reference scale at the appropriate level (see calibration above)?
- skills: Does the question meaningfully relate to the listed skills?

Return ONLY valid JSON:
{{
  "pass": true/false,
  "reason": "1-2 sentence overall verdict",
  "dimensions": {{
    "A": {{"pass": true/false, "note": "brief reason"}},
    "C": {{"pass": true/false, "note": "brief reason"}},
    "Amb": {{"pass": true/false, "note": "brief reason"}},
    "S": {{"pass": true/false, "note": "brief reason"}},
    "skills": {{"pass": true/false, "note": "brief reason"}}
  }}
}}

RULES:
- C=0: ALWAYS pass C. C=1: pass if ANY limiting condition exists (be generous).
- S=0: ALWAYS pass S. S=1: pass if ANY scale context exists (be generous).
- A: PASS if cognitive demand matches. For junior A=1-2: explaining or reasoning about any topic = PASS.
- Overall "pass" = true only if ALL dimensions pass.
- Return ONLY JSON, no markdown, no explanation."""

_AUDIT_FALLBACK = {
    "pass": False,
    "reason": "Audit failed due to evaluation error",
    "dimensions": {
        "A": {"pass": False, "note": "audit error"},
        "C": {"pass": False, "note": "audit error"},
        "Amb": {"pass": False, "note": "audit error"},
        "S": {"pass": False, "note": "audit error"},
        "skills": {"pass": False, "note": "audit error"},
    },
}


def audit_question_quality(
    question_text: str,
    skills: list,
    vector_dict: dict,
    role: str = "mid",
    base_difficulty: str = "medium",
) -> dict:
    """
    Audit whether a generated question matches its intended difficulty vector.

    Returns dict with 'pass' (bool), 'reason' (str), and per-dimension scores.
    Never raises — returns safe fallback on any error.
    """
    if not question_text or not question_text.strip():
        fallback = dict(_AUDIT_FALLBACK)
        fallback["reason"] = "Empty question — cannot audit"
        return fallback

    a_val = vector_dict.get("A", 1)
    c_val = vector_dict.get("C", 0)
    amb_val = vector_dict.get("Amb", 0)
    s_val = vector_dict.get("S", 0)

    try:
        prompt = AUDIT_PROMPT.format(
            question=question_text[:1500],
            skills=", ".join(skills) if isinstance(skills, list) else str(skills),
            role=role,
            base_difficulty=base_difficulty,
            a=a_val,
            a_desc=_VECTOR_DESCRIPTIONS["A"].get(a_val, "Unknown"),
            c=c_val,
            c_desc=_VECTOR_DESCRIPTIONS["C"].get(c_val, "Unknown"),
            amb=amb_val,
            amb_desc=_VECTOR_DESCRIPTIONS["Amb"].get(amb_val, "Unknown"),
            s=s_val,
            s_desc=_VECTOR_DESCRIPTIONS["S"].get(s_val, "Unknown"),
        )

        response = _get_client().chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=400,
        )

        raw = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        result = json.loads(raw)

        # Validate structure
        if not isinstance(result, dict):
            return dict(_AUDIT_FALLBACK)
        result.setdefault("pass", False)
        result.setdefault("reason", "No reason provided")
        result.setdefault("dimensions", {})

        return result

    except json.JSONDecodeError as e:
        logger.warning("⚠️ Audit JSON parse failed: %s", e)
        return dict(_AUDIT_FALLBACK)

    except Exception as e:
        logger.error("❌ Audit LLM call failed: %s", e)
        return dict(_AUDIT_FALLBACK)
