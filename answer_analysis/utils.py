import re
import math
from typing import List


def tokenize_words(text: str) -> List[str]:
    return re.findall(r'\b\w+\b', text.lower())


def tokenize_sentences(text: str) -> List[str]:
    sentences = re.split(r'[.!?]+', text)
    return [s.strip() for s in sentences if s.strip()]


def calculate_lexical_diversity(words: List[str]) -> float:
    if not words:
        return 0.0
    unique = len(set(words))
    total = len(words)
    return unique / total if total > 0 else 0.0


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    return numerator / denominator if denominator != 0 else default


def normalize_score(value: float, min_val: float, max_val: float) -> float:
    if max_val == min_val:
        return 0.5
    normalized = (value - min_val) / (max_val - min_val)
    return max(0.0, min(1.0, normalized))
