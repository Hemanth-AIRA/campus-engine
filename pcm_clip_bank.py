"""
pcm_clip_bank.py — Shared loader for pre-rendered PCM audio banks.

WHY THIS EXISTS
───────────────
AIRA masks Cartesia (TTS) cost + slot saturation by playing pre-rendered speech
straight from memory instead of synthesizing it live. The filler bank pioneered
this pattern; Task 2 generalizes it to acknowledgements, JD/bank questions, and
fixed scripted lines. They all share the same shape:

    a manifest.json listing clips  →  raw pcm_s16le mono @ N Hz on disk
    grouped into buckets           →  pick one (no-immediate-repeat) at runtime

so the load/pick machinery lives here once and the concrete banks (FillerBank,
AckBank, …) just declare their bucket field name + fallback chains.

Layout on disk (produced by scripts/generate_cached_audio.py):
    <assets_dir>/manifest.json            # {sample_rate, voice_id, clips: [...]}
    <assets_dir>/<bucket>/<slug>.pcm      # raw pcm_s16le, 24kHz mono

Every bank degrades gracefully: if the manifest or clips are missing, pick()
returns None and the caller falls back to live TTS (the pre-cache behaviour).
"""
from __future__ import annotations

import json
import logging
import os
import random
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("pcm_clip_bank")

DEFAULT_SAMPLE_RATE = 24_000


class PcmClipBank:
    """Loads pre-rendered PCM clips from a manifest and picks them per bucket with
    no-immediate-repeat. Subclasses set `group_key` (the manifest field that names
    a clip's bucket) and `fallback` (bucket -> ordered list of buckets to borrow
    from when the requested one is empty)."""

    #: manifest field naming a clip's bucket (e.g. "phase" for fillers, "bucket" for acks)
    group_key: str = "group"
    #: bucket -> ordered fallback buckets to try when the requested bucket is empty
    fallback: Dict[str, List[str]] = {}
    #: kind label for logging
    kind: str = "clip"

    def __init__(self, assets_dir: str):
        self.assets_dir = assets_dir
        self.manifest_path = os.path.join(assets_dir, "manifest.json")
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self._by_group: Dict[str, List[dict]] = {}
        self._pcm: Dict[str, bytes] = {}   # clip_id -> raw pcm bytes
        self._loaded = False

    # ── loading (lazy, once) ─────────────────────────────────────────────────
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True  # only attempt once; degrade silently on failure
        try:
            with open(self.manifest_path) as f:
                manifest = json.load(f)
        except FileNotFoundError:
            logger.warning("🔇 [%s] no manifest at %s — %s disabled (live TTS fallback)",
                           self.kind, self.manifest_path, self.kind)
            return
        except Exception as e:
            logger.warning("🔇 [%s] manifest load failed: %s — %s disabled", self.kind, e, self.kind)
            return

        self.sample_rate = int(manifest.get("sample_rate", DEFAULT_SAMPLE_RATE))
        loaded = 0
        for clip in manifest.get("clips", []):
            cid = clip.get("id")
            rel = clip.get("path")
            group = clip.get(self.group_key, "neutral")
            if not cid or not rel:
                continue
            try:
                with open(os.path.join(self.assets_dir, rel), "rb") as pf:
                    self._pcm[cid] = pf.read()
            except Exception as e:
                logger.warning("🔇 [%s] clip %s unreadable (%s) — skipping", self.kind, cid, e)
                continue
            self._by_group.setdefault(group, []).append(clip)
            loaded += 1
        logger.info("🔊 [%s] loaded %d clip(s) across %d bucket(s) @ %dHz",
                    self.kind, loaded, len(self._by_group), self.sample_rate)

    @property
    def available(self) -> bool:
        self._ensure_loaded()
        return bool(self._pcm)

    # ── selection ────────────────────────────────────────────────────────────
    def _candidates_for(self, group: str) -> List[dict]:
        """First non-empty bucket along the group's fallback chain, else any clip."""
        for g in self.fallback.get(group, [group, "neutral"]):
            got = self._by_group.get(g, [])
            if got:
                return got
        return [c for clips in self._by_group.values() for c in clips]

    def pick(self, group: str, recent=None) -> Optional[Tuple[bytes, str]]:
        """Return (pcm_bytes, clip_id) for `group`, avoiding ids in `recent`.

        Returns None when no clips are available so the caller can fall back to
        live TTS.
        """
        self._ensure_loaded()
        if not self._pcm:
            return None
        candidates = self._candidates_for(group)
        if not candidates:
            return None
        recent_ids = set(recent or ())
        fresh = [c for c in candidates if c.get("id") not in recent_ids]
        chosen = random.choice(fresh if fresh else candidates)
        cid = chosen.get("id")
        return self._pcm.get(cid), cid

    def get(self, clip_id: str) -> Optional[bytes]:
        """Return PCM bytes for a specific clip id (keyed lookup), or None."""
        self._ensure_loaded()
        return self._pcm.get(clip_id)
