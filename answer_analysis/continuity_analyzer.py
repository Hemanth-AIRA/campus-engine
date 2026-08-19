from .models import ContinuityAnalysis, TextFeatures, AnalysisInput
from .utils import safe_divide
from typing import Dict, List
import statistics


def analyze_continuity(analysis_input: AnalysisInput,
                       current_features: TextFeatures,
                       session_context: Dict) -> ContinuityAnalysis:
    """
    FIX: Load prior features from correct key, reconstruct from dicts.
    """
    prior_features = _extract_prior_features(session_context)
    
    if not prior_features:
        return ContinuityAnalysis(
            style_continuity=0.5,
            complexity_drift=0.0,
            genericness_drift=0.0,
            specificity_consistency=0.5
        )
    
    style_cont = _calculate_style_continuity(current_features, prior_features)
    complexity_drift = _calculate_complexity_drift(current_features, prior_features)
    genericness_drift = _calculate_genericness_drift(current_features, prior_features)
    specificity_cons = _calculate_specificity_consistency(current_features, prior_features)
    
    return ContinuityAnalysis(
        style_continuity=style_cont,
        complexity_drift=complexity_drift,
        genericness_drift=genericness_drift,
        specificity_consistency=specificity_cons
    )


def _extract_prior_features(session_context: Dict) -> List[TextFeatures]:
    """
    FIX: Load from correct key and reconstruct TextFeatures objects.
    """
    prior_dicts = session_context.get('prior_features', [])
    
    # FIX: Reconstruct TextFeatures from stored dicts
    prior_features = []
    for feat_dict in prior_dicts:
        try:
            prior_features.append(TextFeatures.from_dict(feat_dict))
        except Exception:
            # Skip malformed entries
            continue
    
    return prior_features


def _calculate_style_continuity(current: TextFeatures, prior: List[TextFeatures]) -> float:
    if not prior:
        return 0.5
    
    avg_lexical_div = statistics.mean([f.lexical_diversity for f in prior])
    current_div = current.lexical_diversity
    
    diff = abs(current_div - avg_lexical_div)
    
    if diff < 0.1:
        return 0.8
    elif diff < 0.2:
        return 0.6
    else:
        return 0.4  # FIX: Less punitive (was 0.3)


def _calculate_complexity_drift(current: TextFeatures, prior: List[TextFeatures]) -> float:
    if not prior:
        return 0.0
    
    avg_complexity = statistics.mean([f.avg_sentence_length for f in prior])
    current_complexity = current.avg_sentence_length
    
    # FIX: Reduced severity
    if current_complexity > avg_complexity * 1.5:
        return 0.5  # FIX: Reduced from 0.6
    elif current_complexity < avg_complexity * 0.5:
        return -0.3  # FIX: Reduced magnitude
    else:
        return 0.0


def _calculate_genericness_drift(current: TextFeatures, prior: List[TextFeatures]) -> float:
    """
    FIX: Genericness is now computed independently, not as inverse of specificity.
    """
    if not prior:
        return 0.0
    
    avg_abstraction = statistics.mean([f.abstraction_score for f in prior])
    avg_personal = statistics.mean([f.personal_markers for f in prior])
    avg_numeric = statistics.mean([f.numeric_presence for f in prior])
    
    current_abstraction = current.abstraction_score
    current_personal = current.personal_markers
    current_numeric = current.numeric_presence
    
    # FIX: Genericness = high abstraction + low personal + low numeric
    genericness_increase = 0.0
    
    if current_abstraction > avg_abstraction * 1.3:
        genericness_increase += 0.3
    
    if current_personal < avg_personal * 0.5 and avg_personal > 0:
        genericness_increase += 0.2
    
    if current_numeric < avg_numeric * 0.5 and avg_numeric > 0:
        genericness_increase += 0.2
    
    return min(0.7, genericness_increase)  # FIX: Cap at 0.7


def _calculate_specificity_consistency(current: TextFeatures, prior: List[TextFeatures]) -> float:
    if not prior:
        return 0.5
    
    avg_numeric = statistics.mean([f.numeric_presence for f in prior])
    current_numeric = current.numeric_presence
    
    diff_ratio = safe_divide(abs(current_numeric - avg_numeric), max(avg_numeric, 1), 0.0)
    
    if diff_ratio < 0.5:
        return 0.8
    else:
        return 0.5  # FIX: Less punitive (was 0.4)

