from .models import (AnalysisOutput, ExpectationAlignment, TemporalAnalysis, 
                    ContinuityAnalysis, AnalysisInput, TextFeatures)
from typing import Dict, List,Optional


def aggregate_signals(analysis_input: AnalysisInput,
                     expectations: ExpectationAlignment,
                     temporal: TemporalAnalysis,
                     continuity: ContinuityAnalysis,
                     features: TextFeatures,
                     llm_assessment=True ) -> AnalysisOutput:
    """
    Aggregate all signals including optional LLM interpretive layer.
    
    CRITICAL: LLM is secondary and capped at 25% influence.
    Deterministic signals are primary (75% minimum weight).
    """
    
    # Compute genericness independently (existing logic)
    genericness = _compute_genericness(features, continuity)
    
    signals = {
        'specificity': expectations.specificity_met,
        'personal_context': expectations.personal_context_met,
        'effort_alignment': temporal.effort_plausibility,
        'continuity': continuity.style_continuity,
        'genericness': genericness
    }
    
    # Calculate deterministic confidence delta (existing logic)
    deterministic_delta = _calculate_confidence_delta(signals, continuity)
    
    # Cap deterministic delta (existing logic)
    deterministic_delta = max(deterministic_delta, -0.25)
    deterministic_delta = min(deterministic_delta, 0.25)
    
    # OPTIONAL: Add LLM interpretive adjustment (BOUNDED)
    llm_adjustment = 0.0
    llm_enabled = False
    llm_dict = None
    
    if llm_assessment is not None:
        from .llm_interpreter import convert_llm_score_to_delta
        
        llm_enabled = True
        llm_adjustment = convert_llm_score_to_delta(
            llm_assessment.structural_regularity_score,
            deterministic_delta
        )
        
        # Add LLM score to signals (for transparency)
        signals['llm_structural_regularity'] = llm_assessment.structural_regularity_score / 10.0
        
        # Store LLM assessment for audit trail
        llm_dict = llm_assessment.to_dict()
    
    # FINAL CONFIDENCE DELTA: Deterministic (primary) + LLM (secondary, capped)
    confidence_delta = deterministic_delta + llm_adjustment
    
    # Final safety cap (should rarely trigger, but ensures bounds)
    confidence_delta = max(confidence_delta, -0.25)
    confidence_delta = min(confidence_delta, 0.25)
    
    confidence_band = _determine_confidence_band(confidence_delta)
    
    # Generate explanations (pass LLM assessment for additional context)
    explanations = _generate_explanations(
        analysis_input, 
        signals, 
        continuity, 
        temporal,
        llm_assessment
    )
    
    return AnalysisOutput(
        session_id=analysis_input.session_id,
        question_id=analysis_input.question_id,
        authorship_confidence=confidence_band,
        confidence_delta=round(confidence_delta, 2),
        explanations=explanations,
        signals=signals,
        llm_assessment=llm_dict,
        llm_enabled=llm_enabled,
        llm_adjustment=round(llm_adjustment, 4)
    )


def _generate_explanations(analysis_input: AnalysisInput,
                          signals: Dict[str, float],
                          continuity: ContinuityAnalysis,
                          temporal: TemporalAnalysis,
                          llm_assessment=True) -> List[str]:
    """
    Generate human-readable explanations including optional LLM observations.
    """
    
    explanations = []
    
    # Existing deterministic explanations (keep all existing logic)
    if signals['specificity'] < 0.4:
        if analysis_input.question_type == 'resume':
            explanations.append("Resume-based question lacked project-specific details")
        else:
            explanations.append("Answer lacked concrete examples or specific information")
    
    if continuity.complexity_drift > 0.5:
        explanations.append("Answer complexity increased compared to earlier responses")
    
    if temporal.effort_plausibility < 0.4:
        explanations.append("Response time appears inconsistent with answer complexity")
    
    if signals['genericness'] > 0.7:
        explanations.append("Answer became more generic compared to prior responses")
    
    if signals['continuity'] < 0.4:
        explanations.append("Writing style differs from earlier answers in the session")
    
    # NEW: Add LLM observations if available (max 2, prefixed for clarity)
    if llm_assessment and llm_assessment.observations:
        # Only include if LLM detected meaningful patterns (score >= 7)
        if llm_assessment.structural_regularity_score >= 7:
            for obs in llm_assessment.observations[:2]:  # Max 2 observations
                # Prefix to make clear this is interpretive, not deterministic
                explanations.append(f"Structural note: {obs}")
    
    if not explanations:
        explanations.append("Answer patterns consistent with session baseline")
    
    return explanations


def _compute_genericness(features: TextFeatures, continuity: ContinuityAnalysis) -> float:
    """
    FIX: Compute genericness independently from specificity.
    Genericness = high abstraction + low concrete details + lack of personal context.
    """
    genericness_score = 0.0
    
    # High abstraction score
    if features.abstraction_score > 0.05:
        genericness_score += features.abstraction_score * 5  # Weight abstraction heavily
    
    # Low personal markers
    if features.personal_markers < 2:
        genericness_score += 0.2
    
    # Low numeric/concrete presence
    if features.numeric_presence < 2:
        genericness_score += 0.2
    
    # Add drift component
    genericness_score += continuity.genericness_drift * 0.5
    
    return min(1.0, genericness_score)


def _calculate_confidence_delta(signals: Dict[str, float], continuity: ContinuityAnalysis) -> float:
    """
    FIX: More balanced - no single signal dominates.
    """
    delta = 0.0
    
    # Weak signals with reduced impact
    if signals['specificity'] < 0.35:
        delta -= 0.10  # FIX: Reduced from 0.15
    
    if signals['effort_alignment'] < 0.35:
        delta -= 0.08  # FIX: Reduced from 0.10
    
    if continuity.complexity_drift > 0.5:
        delta -= 0.08  # FIX: Reduced from 0.12
    
    if signals['continuity'] < 0.4:
        delta -= 0.06  # FIX: Reduced from 0.08
    
    if signals['genericness'] > 0.7:
        delta -= 0.08  # NEW: Separate genericness penalty
    
    # Positive signals
    if signals['personal_context'] > 0.7:
        delta += 0.10
    
    if signals['specificity'] > 0.7:
        delta += 0.08
    
    return round(delta, 2)


def _determine_confidence_band(delta: float) -> str:
    if delta >= -0.05:
        return "High"
    elif delta >= -0.15:
        return "Medium"
    else:
        return "Low"


def _generate_explanations(
    analysis_input: AnalysisInput,
    signals: Dict[str, float],
    continuity: ContinuityAnalysis,
    temporal: TemporalAnalysis,
    llm_assessment=None
) -> List[str]:
    """
    Generate human-readable explanations including optional LLM observations.

    Deterministic explanations are PRIMARY.
    LLM observations are SECONDARY and optional.
    """

    explanations = []

    # -------------------------------
    # Deterministic explanations
    # -------------------------------
    if signals['specificity'] < 0.4:
        if analysis_input.question_type == 'resume':
            explanations.append(
                "Resume-based question lacked project-specific details"
            )
        else:
            explanations.append(
                "Answer lacked concrete examples or specific information"
            )

    if continuity.complexity_drift > 0.5:
        explanations.append(
            "Answer complexity increased compared to earlier responses"
        )

    if temporal.effort_plausibility < 0.4:
        explanations.append(
            "Response time appears inconsistent with answer complexity"
        )

    if signals['genericness'] > 0.7:
        explanations.append(
            "Answer became more generic compared to prior responses"
        )

    if signals['continuity'] < 0.4:
        explanations.append(
            "Writing style differs from earlier answers in the session"
        )

    # -------------------------------
    # LLM interpretive notes (optional)
    # -------------------------------
    if (
        llm_assessment is not None
        and hasattr(llm_assessment, "structural_regularity_score")
        and llm_assessment.structural_regularity_score >= 7
        and llm_assessment.observations
    ):
        for obs in llm_assessment.observations[:2]:
            explanations.append(f"Structural note: {obs}")

    # -------------------------------
    # Fallback
    # -------------------------------
    if not explanations:
        explanations.append(
            "Answer patterns consistent with session baseline"
        )

    return explanations
