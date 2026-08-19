from .models import SemanticDeviationResult


def compute_answer_signal(semantic: SemanticDeviationResult) -> float:
    """
    Compute AUTHENTICITY score (human-likeness) from semantic analysis.
    
    HIGHER score = MORE likely HUMAN (authentic, natural speech)
    LOWER score  = MORE likely AI-assisted (polished, copy-pasted)
    
    Score ranges:
    - 0.80-1.00: Very clearly human (multiple disfluencies, personal stories)
    - 0.60-0.79: Likely human (some natural markers)
    - 0.40-0.59: Mixed/uncertain signals
    - 0.20-0.39: Suspicious (too polished, few human markers)
    - 0.00-0.19: Very likely AI (perfect structure, no human signals)
    
    Input mapping (from semantic_analyzer.py):
    - self_consistency = 1.0 - ai_probability  (HIGH = human)
    - conceptual_novelty = polish_score        (HIGH = polished = suspicious)
    - semantic_alignment = human_markers       (HIGH = human signals detected)
    """
    # ── Extract the 3 signals ─────────────────────────────────
    # human_likelihood: direct inverse of AI probability (HIGH = human)
    human_likelihood = semantic.self_consistency
    
    # polish_penalty: how polished the answer is (HIGH = suspicious, so invert)
    anti_polish = 1.0 - semantic.conceptual_novelty
    
    # human_markers: direct count of human speech signals (HIGH = human)
    human_markers = semantic.semantic_alignment
    
    # ── Weighted combination ──────────────────────────────────
    # human_markers gets highest weight (most concrete, countable signal)
    # human_likelihood gets moderate weight (GPT's overall judgment)
    # anti_polish gets lower weight (supportive signal)
    authenticity = (
        human_markers * 0.40 +      # Most concrete signal
        human_likelihood * 0.35 +    # Overall AI probability (inverted)
        anti_polish * 0.25           # Polish-based penalty
    )
    
    # ── Boost/penalize based on extremes ──────────────────────
    
    # Strong human signals — boost (disfluencies are hard evidence)
    if human_markers >= 0.75:
        authenticity = min(1.0, authenticity * 1.25)
    elif human_markers >= 0.55:
        authenticity = min(1.0, authenticity * 1.10)
    
    # Very polished with no human markers — penalize
    if semantic.conceptual_novelty >= 0.75 and human_markers <= 0.25:
        authenticity = max(0.0, authenticity * 0.70)
    
    # Very low AI probability — trust it
    if human_likelihood >= 0.80 and human_markers >= 0.50:
        authenticity = max(authenticity, 0.70)
    
    # Very high AI probability — trust it
    if human_likelihood <= 0.25 and human_markers <= 0.30:
        authenticity = min(authenticity, 0.30)
    
    return min(1.0, max(0.0, authenticity))
