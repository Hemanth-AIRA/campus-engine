from statistics import mean
from typing import List
from .models import AnswerAssistanceSignal


def compute_session_probability(signals: List[AnswerAssistanceSignal]) -> float:
    if len(signals) < 2:
        return 0.0  # No baseline

    weights = []
    values = []

    for i, s in enumerate(signals):
        w = 0.3 if i == 0 else min(1.0, 0.5 + i * 0.1)
        weights.append(w)
        values.append(s.answer_signal * w)

    base = sum(values) / sum(weights)

    # cluster penalty
    high_spikes = sum(1 for s in signals if s.answer_signal > 0.7)
    if high_spikes >= 2:
        base += 0.08

    # recovery bonus
    if signals[-1].answer_signal < signals[-2].answer_signal * 0.7:
        base -= 0.05

    return max(0.0, min(1.0, base))
