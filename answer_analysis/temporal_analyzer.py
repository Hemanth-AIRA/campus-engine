from .models import TemporalAnalysis, TextFeatures, AnalysisInput
from .utils import safe_divide, normalize_score
from typing import Dict


def analyze_temporal(analysis_input: AnalysisInput, 
                    features: TextFeatures,
                    session_context: Dict) -> TemporalAnalysis:
    
    duration = analysis_input.answer_duration_sec
    word_count = features.word_count
    
    expected_duration = estimate_expected_duration(word_count, features)
    effort_plausibility = calculate_effort_plausibility(duration, expected_duration)
    
    session_avg_duration = _get_session_avg_duration(session_context)
    latency_alignment = calculate_latency_alignment(duration, session_avg_duration, features)
    
    return TemporalAnalysis(
        effort_plausibility=effort_plausibility,
        latency_complexity_alignment=latency_alignment
    )


def estimate_expected_duration(word_count: int, features: TextFeatures) -> float:
    """
    FIX: More realistic baseline - assume thinking pauses and normal typing.
    """
    base_seconds = word_count * 0.5
    
    if features.numeric_presence > 3:
        base_seconds *= 1.2
    if features.avg_sentence_length > 20:
        base_seconds *= 1.15
    
    # FIX: Higher floor to account for thinking time
    return max(base_seconds, 15.0)


def calculate_effort_plausibility(actual: float, expected: float) -> float:
    """
    FIX: Less aggressive - fast responses aren't inherently suspicious.
    """
    if actual < expected * 0.3:
        return 0.3  # FIX: Less punitive
    elif actual < expected * 0.6:
        return 0.6  # FIX: Raised from 0.5
    elif actual > expected * 2.0:
        return 0.5  # FIX: Raised from 0.4
    else:
        return 0.8


def calculate_latency_alignment(duration: float, session_avg: float, features: TextFeatures) -> float:
    if session_avg == 0:
        return 0.5
    
    ratio = safe_divide(duration, session_avg, 1.0)
    complexity_ratio = features.word_count / 50.0
    
    # FIX: Less aggressive thresholds
    if complexity_ratio > 1.5 and ratio < 0.7:
        return 0.4  # FIX: Raised from 0.3
    elif complexity_ratio < 0.5 and ratio > 1.5:
        return 0.5  # FIX: Raised from 0.4
    else:
        return 0.7


def _get_session_avg_duration(session_context: Dict) -> float:
    timings = session_context.get('timings', [])
    if not timings:
        return 0.0
    return sum(timings) / len(timings)