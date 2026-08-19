from .models import ExpectationAlignment, TextFeatures, AnalysisInput


def map_expectations(analysis_input: AnalysisInput, features: TextFeatures) -> ExpectationAlignment:
    q_type = analysis_input.question_type.lower()
    
    specificity = _assess_specificity(q_type, features, analysis_input)
    personal_context = _assess_personal_context(q_type, features, analysis_input)
    sequencing = _assess_sequencing(q_type, features)
    tradeoff = _assess_tradeoff(q_type, features)
    
    return ExpectationAlignment(
        specificity_met=specificity,
        personal_context_met=personal_context,
        sequencing_met=sequencing,
        tradeoff_met=tradeoff
    )


def _assess_specificity(q_type: str, features: TextFeatures, analysis_input: AnalysisInput) -> float:
    """
    FIX: Added guardrails - specificity is a weak signal, not absolute.
    """
    if q_type in ['resume', 'implementation']:
        has_specifics = (
            features.numeric_presence > 0 or
            features.personal_markers > 2 or
            features.avg_sentence_length > 15
        )
        # FIX: Less punitive - short answers can still be valid
        return 0.65 if has_specifics else 0.35
    return 0.5


def _assess_personal_context(q_type: str, features: TextFeatures, analysis_input: AnalysisInput) -> float:
    if q_type == 'resume':
        if features.personal_markers < 2:
            return 0.3  # FIX: Less punitive
        if analysis_input.resume_text and features.word_count > 30:
            return 0.8
        return 0.5
    return 0.5


def _assess_sequencing(q_type: str, features: TextFeatures) -> float:
    """
    FIX: Use raw_text from features instead of non-existent _raw_text.
    """
    if q_type == 'troubleshooting':
        sequence_words = ['first', 'then', 'next', 'after', 'finally', 'step']
        text_lower = features.raw_text.lower()  # FIX: Access stored raw text
        sequence_count = sum(1 for w in sequence_words if w in text_lower)
        return min(1.0, sequence_count / 3)
    return 0.5


def _assess_tradeoff(q_type: str, features: TextFeatures) -> float:
    if q_type == 'comparison':
        return 0.7 if features.word_count > 40 else 0.4  # FIX: Less punitive
    return 0.5