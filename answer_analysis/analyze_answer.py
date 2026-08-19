from typing import Dict
import os
import logging

logger = logging.getLogger(__name__)

USE_SEMANTIC_LLM = os.getenv("ENABLE_SEMANTIC_LLM", "true").lower() == "true"


async def analyze_answer(input_data: Dict) -> Dict:
    """
    Main analysis entry point.

    PURPOSE:
    - Compute a per-answer assistance signal based on semantic self-consistency
    - Store signals for session-level probability aggregation
    - NO authorship judgment
    - NO AI detection claims
    """

    # -----------------------------
    # Imports (local to avoid cycles)
    # -----------------------------
    from .models import AnalysisInput, AnswerAssistanceSignal
    from .feature_extractor import extract_features
    from .semantic_analyzer import analyze_semantic_deviation
    from .answer_scoring import compute_answer_signal
    from .anomaly_guards import detect_anomalies
    from .session_store import load_session, append_signal

    # -----------------------------
    # Parse input
    # -----------------------------
    analysis_input = AnalysisInput(**input_data)

    # -----------------------------
    # Load session context
    # -----------------------------
    session = load_session(analysis_input.session_id)

    prior_answers = session.get("answers", [])
    prior_signals = session.get("signals", [])

    # -----------------------------
    # Feature extraction (deterministic, secondary)
    # -----------------------------
    features = extract_features(analysis_input.answer_text)

    # -----------------------------
    # Pre-check: Can we run semantic analysis?
    # -----------------------------
    word_count = len(analysis_input.answer_text.split())
    skip_reason = None

    if not prior_answers:
        skip_reason = "first_answer_no_baseline"
    elif word_count < 20:
        skip_reason = "answer_too_short"

    # -----------------------------
    # Semantic analysis (PRIMARY SIGNAL)
    # -----------------------------
    semantic_result = None

    if USE_SEMANTIC_LLM and skip_reason is None:
        try:
            semantic_result = await analyze_semantic_deviation(
                question=analysis_input.question_text,
                current_answer=analysis_input.answer_text,
                prior_answers=prior_answers,
                resume_text=analysis_input.resume_text  # Added for resume verification
            )

            if semantic_result:
                logger.info(
                    "Semantic analysis complete | "
                    f"self_consistency={semantic_result.self_consistency:.2f}, "
                    f"novelty={semantic_result.conceptual_novelty:.2f}, "
                    f"alignment={semantic_result.semantic_alignment:.2f}"
                )
            else:
                skip_reason = "llm_returned_no_result"
                logger.info("Semantic analysis returned no result")

        except Exception as e:
            skip_reason = f"llm_error"
            logger.warning(f"Semantic analyzer failed (non-fatal): {e}")
            semantic_result = None
    elif skip_reason:
        logger.info(f"Semantic analysis skipped: {skip_reason}")

    # -----------------------------
    # Fallback baseline signal
    # -----------------------------
    if semantic_result is None:
        from .models import AnswerAssistanceSignal
        from .session_store import append_signal

        baseline_signal = AnswerAssistanceSignal(
            answer_signal=0.0,              # NEUTRAL
            semantic_result=None,
            anomaly_flags=[skip_reason or "semantic_unavailable"]
        )

        append_signal(
            session_id=analysis_input.session_id,
            signal=baseline_signal,
            answer_text=analysis_input.answer_text,
            answer_duration=analysis_input.answer_duration_sec
        )

        return {
            "session_id": analysis_input.session_id,
            "question_id": analysis_input.question_id,
            "answer_signal": 0.0,
            "note": "Baseline signal recorded due to unavailable semantic analysis"
        }


    # -----------------------------
    # Compute per-answer assistance signal
    # -----------------------------
    answer_signal = compute_answer_signal(semantic_result)

    # -----------------------------
    # Deterministic anomaly guards (SECONDARY)
    # -----------------------------
    session_avg_duration = session.get("avg_duration", 0.0)

    anomaly_flags = detect_anomalies(
        features=features,
        answer_duration=analysis_input.answer_duration_sec,
        session_avg_duration=session_avg_duration
    )

    # -----------------------------
    # Package signal
    # -----------------------------
    signal = AnswerAssistanceSignal(
        answer_signal=answer_signal,
        semantic_result=semantic_result,
        anomaly_flags=anomaly_flags
    )

    # -----------------------------
    # Persist to session store
    # -----------------------------
    append_signal(
        session_id=analysis_input.session_id,
        signal=signal,
        answer_text=analysis_input.answer_text,
        answer_duration=analysis_input.answer_duration_sec
    )

    # -----------------------------
    # Return PER-ANSWER diagnostic output
    # (Internal use only — not recruiter-facing)
    # -----------------------------
    return {
        "session_id": analysis_input.session_id,
        "question_id": analysis_input.question_id,
        "answer_signal": round(answer_signal, 3),
        "semantic_breakdown": {
            "self_consistency": round(semantic_result.self_consistency, 3),
            "conceptual_novelty": round(semantic_result.conceptual_novelty, 3),
            "semantic_alignment": round(semantic_result.semantic_alignment, 3),
        },
        "observations": semantic_result.observations,
        "anomaly_flags": anomaly_flags,
        "note": (
            "This signal reflects deviation from the candidate’s own prior "
            "response patterns and does not determine authorship or intent."
        )
    }
