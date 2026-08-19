"""
ack_audio.py — Pre-rendered acknowledgement bank (Task 2).

The interviewer's acknowledgement between questions is content-neutral (it thanks
the candidate and transitions — it never quotes the answer), so it is fully
reusable across candidates. Instead of generating an ack with GPT and synthesizing
it live on Cartesia every turn, we pre-render a bank of warm acks per intent /
answer-quality bucket OFFLINE (scripts/generate_cached_audio.py) and play the right
one straight from memory — zero live GPT, zero live TTS.

Buckets (chosen by the nano intent+quality classifier in reflex_engine.py):
    answer_good     — candidate answered well            → affirming
    answer_partial  — partial / half answer              → encouraging, scaffolding
    answer_wrong    — incorrect answer                   → gentle, keeps morale up
    skip            — candidate skipped / "I don't know" → reassuring
    repeat          — asked to repeat / meta question    → oblige, no judgement
    rephrase        — asked to rephrase                  → oblige
    exit            — candidate wants to end             → polite close
    neutral         — low classifier confidence fallback → safe, quality-neutral

Every bucket falls back to `neutral`, and a missing bank falls back to live TTS,
so a cache miss is never an interview failure.
"""
from __future__ import annotations

import os
from typing import Optional

from pcm_clip_bank import PcmClipBank

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "acks")

# Base ack buckets (transition to the NEXT question).
_BASE_BUCKETS = (
    "answer_good", "answer_partial", "answer_wrong",
    "skip", "repeat", "rephrase", "exit", "neutral",
)
# "*_final" variants acknowledge the answer then transition to the FINAL question
# ("...let's move on to your final question"). repeat/rephrase (re-ask same question)
# and exit (terminal) have no final variant.
_FINAL_BUCKETS = (
    "answer_good_final", "answer_partial_final", "answer_wrong_final",
    "skip_final", "neutral_final",
)
ACK_BUCKETS = _BASE_BUCKETS + _FINAL_BUCKETS


def _base_bucket(intent: str, quality: str) -> str:
    """intent+quality → base bucket (no final suffix)."""
    if intent in ("others", "answer", "answered"):
        if quality in ("good", "strong", "correct"):
            return "answer_good"
        if quality in ("partial", "half", "incomplete"):
            return "answer_partial"
        if quality in ("wrong", "incorrect", "bad"):
            return "answer_wrong"
        if quality in ("skip", "none"):
            return "skip"
        return "neutral"
    if intent in ("skip", "repeat", "rephrase", "exit"):
        return intent
    # audio/video/other clarifications behave like a benign repeat/oblige.
    if intent in ("audio", "video"):
        return "repeat"
    return "neutral"


def bucket_for(intent: str, quality: Optional[str], confidence: Optional[float] = None,
               min_confidence: float = 0.5, is_last: bool = False) -> str:
    """Map the classifier's {intent, quality, confidence} to an ack bucket.

    Answer intents split by quality (good/partial/wrong); everything else maps to
    its intent bucket. Low confidence routes to `neutral` so a misread answer never
    gets a wrongly-toned ack. When `is_last` and the base bucket transitions to a
    next question, use the "*_final" variant so the ack leads into the final question.
    """
    intent = (intent or "others").lower().strip()
    quality = (quality or "").lower().strip()
    base = "neutral" if (confidence is not None and confidence < min_confidence) \
        else _base_bucket(intent, quality)
    if is_last and f"{base}_final" in _FINAL_BUCKETS:
        return f"{base}_final"
    return base


class AckBank(PcmClipBank):
    """Pre-rendered acknowledgement PCM clips, picked per bucket with no-immediate-repeat."""

    group_key = "bucket"
    kind = "ack"
    # base bucket → [self, neutral]; final bucket → [self, base, neutral_final, neutral]
    fallback = {
        **{b: [b, "neutral"] for b in _BASE_BUCKETS},
        **{f"{b}_final": [f"{b}_final", b, "neutral_final", "neutral"]
           for b in ("answer_good", "answer_partial", "answer_wrong", "skip", "neutral")},
    }

    def __init__(self, assets_dir: str = _ASSETS_DIR):
        super().__init__(assets_dir)


# Process-wide singleton — one bank per interview process, loaded lazily on first use.
_BANK: Optional[AckBank] = None


def get_ack_bank() -> AckBank:
    """Return the shared AckBank for this process (created on first call)."""
    global _BANK
    if _BANK is None:
        _BANK = AckBank()
    return _BANK
