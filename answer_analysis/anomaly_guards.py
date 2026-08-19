from typing import List
from .models import TextFeatures


def detect_anomalies(
    features: TextFeatures,
    answer_duration: float,
    session_avg_duration: float
) -> List[str]:

    flags = []

    if features.word_count > 200 and answer_duration < 15:
        flags.append("fast_long_answer")

    if features.avg_sentence_length > 35:
        flags.append("syntactic_spike")

    if session_avg_duration and answer_duration < session_avg_duration * 0.3:
        flags.append("timing_deviation")

    return flags
