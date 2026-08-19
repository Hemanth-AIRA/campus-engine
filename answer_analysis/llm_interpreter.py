import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger('LLMInterpreter')


@dataclass
class LLMAssessment:
    """
    LLM interpretive assessment result.
    
    CRITICAL: This is advisory only. It does NOT determine authorship.
    Maximum influence: 25% of final confidence adjustment.
    """
    structural_regularity_score: int  # 0-10 scale
    observations: List[str]
    uncertainty_note: str
    raw_response: str  # For audit trail
    
    def to_dict(self) -> Dict:
        return {
            'structural_regularity_score': self.structural_regularity_score,
            'observations': self.observations,
            'uncertainty_note': self.uncertainty_note,
            'raw_response': self.raw_response
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'LLMAssessment':
        return cls(
            structural_regularity_score=data.get('structural_regularity_score', 5),
            observations=data.get('observations', []),
            uncertainty_note=data.get('uncertainty_note', ''),
            raw_response=data.get('raw_response', '')
        )


async def assess_structural_regularity(
    answer_text: str,
    question_text: str,
    session_history: List[str],
    openai_client=None,
    model: str = "gpt-4o-mini",
    max_retries: int = 2
) -> Optional[LLMAssessment]:
    """
    Use LLM to assess structural regularity of an answer.
    
    CRITICAL SAFETY FEATURES:
    - This is OPTIONAL and SECONDARY to deterministic analysis
    - Failures return None (system continues with deterministic only)
    - Output is capped to 25% influence maximum
    - No authorship claims, no AI detection claims
    - Strictly structural/rhetorical analysis only
    
    Args:
        answer_text: The answer to analyze
        question_text: The question that was asked
        session_history: Previous answers in this session
        openai_client: OpenAI client instance (optional, creates if None)
        model: LLM model to use
        max_retries: Number of retry attempts on failure
    
    Returns:
        LLMAssessment object or None if analysis fails
    """
    
    # Import here to avoid circular dependency
    from .llm_prompts import LLM_SYSTEM_PROMPT, build_llm_user_prompt
    
    # Safety check: Don't analyze very short answers (not enough structure)
    if len(answer_text.split()) < 20:
        logger.info("Answer too short for LLM structural analysis, skipping")
        return None
    
    # Create client if not provided
    if openai_client is None:
        try:
            from openai import OpenAI
            import os
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                logger.warning("OPENAI_API_KEY not found, skipping LLM analysis")
                return None
            openai_client = OpenAI(api_key=api_key)
        except Exception as e:
            logger.error(f"Failed to create OpenAI client: {e}")
            return None
    
    # Build prompt
    user_prompt = build_llm_user_prompt(answer_text, question_text, session_history)
    
    # Attempt LLM call with retries
    for attempt in range(max_retries):
        try:
            response = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,  # Low temperature for consistency
                max_tokens=500,
                response_format={"type": "json_object"}  # Force JSON output
            )
            
            raw_response = response.choices[0].message.content
            
            # Parse JSON response
            parsed = json.loads(raw_response)
            
            # Validate required fields
            if 'structural_regularity_score' not in parsed:
                raise ValueError("Missing required field: structural_regularity_score")
            if 'observations' not in parsed:
                raise ValueError("Missing required field: observations")
            if 'uncertainty_note' not in parsed:
                raise ValueError("Missing required field: uncertainty_note")
            
            # Validate score range
            score = parsed['structural_regularity_score']
            if not isinstance(score, int) or score < 0 or score > 10:
                raise ValueError(f"Invalid score: {score}. Must be integer 0-10")
            
            # Validate observations is list
            if not isinstance(parsed['observations'], list):
                raise ValueError("observations must be a list")
            
            # Validate uncertainty note is not empty
            if not parsed['uncertainty_note'] or not parsed['uncertainty_note'].strip():
                raise ValueError("uncertainty_note cannot be empty")
            
            # CRITICAL: Check for prohibited language
            prohibited_terms = [
                'ai-generated', 'ai generated', 'plagiarized', 'plagiarism',
                'cheating', 'copied', 'dishonest', 'fake'
            ]
            
            full_text = (
                str(parsed['observations']) + ' ' + 
                parsed['uncertainty_note']
            ).lower()
            
            for term in prohibited_terms:
                if term in full_text:
                    logger.error(f"LLM output contains prohibited term: {term}")
                    raise ValueError(f"Prohibited language detected: {term}")
            
            # Success - return assessment
            assessment = LLMAssessment(
                structural_regularity_score=score,
                observations=parsed['observations'][:5],  # Cap at 5 observations
                uncertainty_note=parsed['uncertainty_note'],
                raw_response=raw_response
            )
            
            logger.info(
                f"LLM structural analysis complete: score={score}/10, "
                f"{len(parsed['observations'])} observations"
            )
            
            return assessment
            
        except json.JSONDecodeError as e:
            logger.warning(f"LLM returned invalid JSON (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return None
                
        except Exception as e:
            logger.warning(f"LLM assessment failed (attempt {attempt+1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return None
    
    return None


def convert_llm_score_to_delta(
    llm_score: int,
    deterministic_delta: float
) -> float:
    """
    Convert LLM structural regularity score to a capped confidence adjustment.
    
    CRITICAL CONSTRAINTS:
    - LLM can contribute at most 25% of total adjustment
    - LLM cannot override deterministic signals
    - High regularity (7-10) suggests possible concern (small negative)
    - Low regularity (0-3) is neutral or slightly positive
    - Mid regularity (4-6) is neutral
    
    Args:
        llm_score: Structural regularity score (0-10)
        deterministic_delta: The existing deterministic confidence delta
    
    Returns:
        Small adjustment (-0.0625 to +0.025) to add to deterministic delta
    """
    
    # Map LLM score to adjustment
    # High structural rigidity (7-10) → small negative adjustment
    # Low structural rigidity (0-3) → small positive adjustment
    # Mid range (4-6) → near zero
    
    if llm_score >= 9:
        # Very high rigidity → maximum concern
        llm_adjustment = -0.0625  # -0.25 * 0.25 (25% max influence)
    elif llm_score >= 7:
        # High rigidity → moderate concern
        llm_adjustment = -0.0375  # -0.15 * 0.25
    elif llm_score >= 4:
        # Mid range → neutral
        llm_adjustment = 0.0
    elif llm_score >= 1:
        # Low rigidity → slight positive
        llm_adjustment = 0.0125  # +0.05 * 0.25
    else:  # score == 0
        # Very low rigidity → positive signal
        llm_adjustment = 0.025  # +0.10 * 0.25
    
    # CRITICAL: Ensure we don't over-adjust
    # If deterministic is already negative, don't pile on
    if deterministic_delta < -0.15 and llm_adjustment < 0:
        llm_adjustment *= 0.5  # Reduce impact by half
    
    # If deterministic is positive, don't let LLM negate it strongly
    if deterministic_delta > 0.05 and llm_adjustment < -0.03:
        llm_adjustment = -0.03  # Cap negative influence
    
    logger.info(
        f"LLM adjustment: score={llm_score}/10 → delta={llm_adjustment:.4f} "
        f"(deterministic={deterministic_delta:.2f})"
    )
    
    return llm_adjustment
