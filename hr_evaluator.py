"""
HR Evaluator — LLM-based behavioral interview evaluation.

Flow: transcript + dimensions → LLM → raw JSON stored → parsed into per-dimension rows.

Uses gpt-4o with strict JSON schema for reliable structured output.
"""

import json
import logging
from typing import List, Dict, Optional
from openai import OpenAI

logger = logging.getLogger("HREvaluator")


EVALUATION_PROMPT = """You are an expert behavioral interview evaluator for corporate hiring.

You are given the FULL transcript of a behavioral HR interview and a list of evaluation dimensions.

═══════════════════════════════════════════════════════════════════════════════
YOUR TASK:
═══════════════════════════════════════════════════════════════════════════════
1. Read the entire transcript carefully.
2. For EACH evaluation dimension, assign a score from 1 to 10 based on evidence from the transcript.
3. Provide a brief justification (1-2 sentences) for each score.
4. Write an overall summary (2-3 sentences).
5. List top 3 strengths and top 3 concerns.

═══════════════════════════════════════════════════════════════════════════════
SCORING GUIDE (1-10):
═══════════════════════════════════════════════════════════════════════════════
  1-2:  No evidence at all / actively negative signals
  3-4:  Weak evidence / vague or generic responses
  5-6:  Adequate / shows some relevant experience but lacks depth
  7-8:  Strong / clear examples with specific details and outcomes
  9-10: Exceptional / compelling stories with measurable impact

═══════════════════════════════════════════════════════════════════════════════
RULES:
═══════════════════════════════════════════════════════════════════════════════
- Score ONLY based on transcript evidence. Do NOT assume competence from silence.
- If a dimension was never discussed in the interview, score it 4 (neutral/insufficient data) and note "Not directly assessed in interview."
- Personal stories with specific details (names, timelines, outcomes) are stronger signals than abstract answers.
- Short/vague answers should lower the score for that dimension.
- Custom/recruiter dimensions (non-standard) should be scored based on any relevant signals in the transcript, even if not explicitly asked about.

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT — Return ONLY valid JSON:
═══════════════════════════════════════════════════════════════════════════════
{
  "scores": [
    {
      "dimension_key": "<string>",
      "dimension_name": "<string>",
      "score": <integer 1-10>,
      "justification": "<string 1-2 sentences>"
    }
  ],
  "summary": "<string 2-3 sentences overall assessment>",
  "strengths": ["<string>", "<string>", "<string>"],
  "concerns": ["<string>", "<string>", "<string>"]
}"""


def _build_evaluation_prompt(
    transcript_text: str,
    dimensions: List[Dict],
    job_title: str = "",
    company_name: str = "",
) -> str:
    """Build the user prompt with transcript and dimensions."""
    dim_lines = []
    for d in dimensions:
        dim_lines.append(
            f"  • {d['dimension_name']} ({d['dimension_key']}): {d.get('description', '')}"
        )
    dimensions_text = "\n".join(dim_lines)

    context = ""
    if job_title:
        context += f"\nRole: {job_title}"
    if company_name:
        context += f"\nCompany: {company_name}"

    return f"""INTERVIEW CONTEXT:{context}

EVALUATION DIMENSIONS:
{dimensions_text}

FULL INTERVIEW TRANSCRIPT:
{transcript_text}

Score each dimension based on the transcript evidence above. Return JSON only."""


def evaluate_hr_interview(
    transcript_text: str,
    dimensions: List[Dict],
    job_title: str = "",
    company_name: str = "",
    model: str = "gpt-4o",
) -> Optional[Dict]:
    """
    Run behavioral evaluation on a transcript.

    Args:
        transcript_text: Formatted transcript from HRInterviewEngine.get_transcript_text()
        dimensions: List of dicts with dimension_key, dimension_name, description
        job_title: For context
        company_name: For context
        model: LLM model to use

    Returns:
        Parsed evaluation dict or None on failure.
        Raw JSON is always returned for storage even if parsing partial.
    """
    if not transcript_text or len(transcript_text.strip()) < 50:
        logger.warning("Transcript too short for evaluation")
        return None

    client = OpenAI()

    user_prompt = _build_evaluation_prompt(
        transcript_text, dimensions, job_title, company_name
    )

    try:
        logger.info(
            f"🔍 [HR_EVALUATION_STARTED] dimensions={len(dimensions)} "
            f"transcript_length={len(transcript_text)} model={model}"
        )

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EVALUATION_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
            max_tokens=2000,
        )

        raw_content = response.choices[0].message.content
        parsed = json.loads(raw_content)

        logger.info(
            f"✅ [HR_EVALUATION_FINISHED] "
            f"scores={len(parsed.get('scores', []))} "
            f"summary_length={len(parsed.get('summary', ''))}"
        )

        # Validate structure
        if "scores" not in parsed:
            logger.error("Evaluation response missing 'scores' key")
            parsed["scores"] = []

        # Clamp scores to 1-10
        for score_entry in parsed.get("scores", []):
            if "score" in score_entry:
                score_entry["score"] = max(1, min(10, int(score_entry["score"])))

        # Ensure summary/strengths/concerns exist
        parsed.setdefault("summary", "Evaluation completed.")
        parsed.setdefault("strengths", [])
        parsed.setdefault("concerns", [])

        # Store the raw JSON string for debugging
        parsed["_raw_response"] = raw_content

        return parsed

    except json.JSONDecodeError as e:
        logger.error(f"❌ [HR_EVALUATION_FAILED] JSON parse error: {e}")
        return {"_raw_response": raw_content, "scores": [], "error": str(e)}

    except Exception as e:
        logger.error(f"❌ [HR_EVALUATION_FAILED] API error: {e}")
        return None


def compute_hr_score(evaluation: Dict) -> float:
    """
    Compute overall HR score from dimension evaluations.
    Returns score on 0-5 scale (LLM scores 1-10, divided by 2 for consistency with technical scoring).
    """
    scores = evaluation.get("scores", [])
    if not scores:
        return 0.0

    total = sum(s.get("score", 5) for s in scores)
    avg_10 = total / len(scores)
    return round(avg_10 / 2.0, 1)  # Convert 0-10 → 0-5


def compute_final_combined_score(
    technical_score: float,
    hr_score: float,
    technical_weight: float = 0.7,
    hr_weight: float = 0.3,
) -> float:
    """
    Combine technical and HR scores with configurable weights.
    Both scores should be on same scale (0-5 or 0-10).
    """
    return round(
        (technical_score * technical_weight) + (hr_score * hr_weight),
        1,
    )
