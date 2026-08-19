"""
Phase 3B — Skill Adaptation Module

Pure functions. No DB. No shared state. No imports from engine modules.
Consumed by webrtc_interview.py between rounds and at interview end.

Responsibilities:
  1. Compute multiplier adaptation from confidence signals
  2. Merge historical baseline with round base difficulty
  3. Exponential moving average for baseline updates
  4. Drift control via confidence variance
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Variance threshold for drift control
_VARIANCE_THRESHOLD = 0.25

# EMA default weight (30% new, 70% history)
_DEFAULT_ALPHA = 0.3


# ═══════════════════════════════════════════════════════════════════════════
# Core Adaptation Functions
# ═══════════════════════════════════════════════════════════════════════════


def merge_baseline_with_round(
    baseline: Optional[dict],
    round_base: int,
    round_max: int = 4,
) -> int:
    """
    Compute initial complexity from historical baseline.

    If no history → use round_base as-is.
    If history exists → nudge up by half the baseline complexity.
    Never skip below round floor. Never exceed round max.

    Returns adjusted starting level.
    """
    if not baseline:
        return round_base

    baseline_complexity = baseline.get("baseline_complexity", 0.0)
    confidence_variance = baseline.get("confidence_variance", 0.0)

    # Drift control: reduce baseline influence if candidate is inconsistent
    if confidence_variance > _VARIANCE_THRESHOLD:
        weight = 0.25  # low trust in noisy baseline
        logger.info(
            "⚠️ High variance (%.3f > %.3f) — reducing baseline weight to %.2f",
            confidence_variance, _VARIANCE_THRESHOLD, weight,
        )
    else:
        weight = 0.5  # normal confidence in baseline

    adjustment = round(baseline_complexity * weight)
    result = max(round_base, min(round_base + adjustment, round_max))

    logger.info(
        "🔀 Baseline merge: base=%d + adjustment=%d (complexity=%.2f × weight=%.2f) → %d",
        round_base, adjustment, baseline_complexity, weight, result,
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Exponential Moving Average
# ═══════════════════════════════════════════════════════════════════════════

def compute_exponential_update(old_val: float, new_val: float, alpha: float = _DEFAULT_ALPHA) -> float:
    """
    Generic EMA: result = alpha * new_val + (1 - alpha) * old_val

    alpha=0.3 → 30% new data, 70% historical.
    Safe against single-interview noise.
    """
    return alpha * new_val + (1 - alpha) * old_val


def compute_variance_update(
    old_variance: float,
    old_mean: float,
    new_observation: float,
    alpha: float = _DEFAULT_ALPHA,
) -> float:
    """
    Running EMA variance estimate.
    delta = observation - old_mean
    new_variance = (1-alpha) * old_variance + alpha * delta^2
    """
    delta = new_observation - old_mean
    return (1 - alpha) * old_variance + alpha * (delta ** 2)


# ═══════════════════════════════════════════════════════════════════════════
# Post-Interview Aggregation
# ═══════════════════════════════════════════════════════════════════════════

def aggregate_interview_results(
    question_rows: List[dict],
) -> Dict[str, dict]:
    """
    Aggregate per-question evaluation data into per-skill summaries.

    Input: list of dicts with keys {skill, confidence_score, effective_level, evaluation_status}
    Output: {skill: {avg_confidence, avg_complexity, count}}

    Only includes 'completed' evaluations.
    """
    skill_data: Dict[str, dict] = {}

    for row in question_rows:
        if row.get("evaluation_status") != "completed":
            continue

        skill = row.get("skill", "General")
        conf = row.get("confidence_score", 0.5)
        level = row.get("effective_level", 1)

        if skill not in skill_data:
            skill_data[skill] = {"total_conf": 0.0, "total_level": 0, "count": 0}

        skill_data[skill]["total_conf"] += conf
        skill_data[skill]["total_level"] += level
        skill_data[skill]["count"] += 1

    result = {}
    for skill, data in skill_data.items():
        count = data["count"]
        if count > 0:
            result[skill] = {
                "avg_confidence": data["total_conf"] / count,
                "avg_complexity": data["total_level"] / count,
                "count": count,
            }

    logger.info(
        "📊 Aggregated %d skills from %d completed evaluations",
        len(result), sum(d["count"] for d in result.values()),
    )
    return result
