from typing import Dict, List, Optional, Any
from .session_probability import compute_session_probability
from .models import AnswerAssistanceSignal


def generate_session_report(session: Dict) -> Dict:
    signals = [
        AnswerAssistanceSignal(
            answer_signal=s["answer_signal"],
            semantic_result=None,
            anomaly_flags=s["anomaly_flags"]
        )
        for s in session.get("signals", [])
    ]

    prob = compute_session_probability(signals)

    band = (
        "Low" if prob < 0.3 else
        "Moderate" if prob < 0.6 else
        "Elevated" if prob < 0.8 else
        "High"
    )

    return {
        "assistance_likelihood": round(prob, 2),
        "confidence_band": band,
        "disclaimer": (
            "This score reflects deviation from the candidate’s own response patterns "
            "and does not determine authorship or intent."
        )
    }
