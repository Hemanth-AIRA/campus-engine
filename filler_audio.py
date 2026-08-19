"""
filler_audio.py — Pre-rendered filler-clip bank for masking Cartesia key-pool waits.

Fillers CANNOT be synthesized on demand: Cartesia (the TTS) is exactly what's
saturated when we need a filler. So we pre-render a bank of short, content-neutral
interviewer lines OFFLINE (scripts/generate_cached_audio.py) into raw PCM and play
them straight from memory while a Cartesia slot is unavailable.

Layout on disk:
    assets/fillers/manifest.json          # list of clips + sample_rate
    assets/fillers/<phase>/<slug>.pcm     # raw pcm_s16le, 24kHz mono

The bank degrades gracefully: if the manifest or clips are missing, pick() returns
None and callers fall back to a silent wait (the current behaviour).

The load/pick machinery now lives in pcm_clip_bank.PcmClipBank, shared with the
ack/question/script banks (Task 2); this module only declares the filler-specific
bucket field ("phase"), fallback chains, and the question-position → phase mapping.
"""
from __future__ import annotations

import os
from typing import Optional

from pcm_clip_bank import PcmClipBank

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fillers")


class FillerBank(PcmClipBank):
    """Pre-rendered filler PCM clips, picked per interview phase with no-immediate-repeat."""

    group_key = "phase"
    kind = "filler"
    # If a phase bucket is empty, borrow clips from these buckets in order.
    fallback = {
        "opening": ["opening", "neutral", "mid"],
        "mid":     ["mid", "neutral"],
        "closing": ["closing", "neutral", "mid"],
        "neutral": ["neutral", "mid"],
        "wait":    ["wait", "neutral", "mid"],
        # Pre-interview holding pattern. Falls back to the brief "wait" clips until
        # the dedicated "waiting" clips are rendered.
        "waiting": ["waiting", "wait", "neutral", "mid"],
    }

    def __init__(self, assets_dir: str = _ASSETS_DIR):
        super().__init__(assets_dir)

    @staticmethod
    def phase_for(question_no: Optional[int], total: Optional[int]) -> str:
        """Map question position → filler phase (opening / mid / closing)."""
        if question_no is None:
            return "mid"
        if question_no <= 1:
            return "opening"
        if total and question_no >= total - 1:
            return "closing"
        return "mid"


# Process-wide singleton — one bank per interview process, loaded lazily on first use.
_BANK: Optional[FillerBank] = None


def get_filler_bank() -> FillerBank:
    """Return the shared FillerBank for this process (created on first call)."""
    global _BANK
    if _BANK is None:
        _BANK = FillerBank()
    return _BANK
