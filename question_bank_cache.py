"""
question_bank_cache.py — Redis-backed cached JD-question audio (Task 2, Pillar 2).

Cached JD questions are shared by every candidate for a job, so their audio is
pre-rendered ONCE and stored in the shared Redis the key-pool already runs
(REDIS_URL on the LiveKit VM — see api_key_pool.py / docs/KEYPOOL_REDIS_VM_DEPLOYMENT).
This keeps the /claim payload tiny (it carries only question text + Redis keys) and
puts the audio in infra that is already multi-pod and always-on.

Storage
───────
    key   = qbank:{job_id}:{group}:{qid}         (defined by the producer)
    value = MP3 bytes (compressed ~10-20x vs raw PCM, to spare Redis memory)

At interview time the engine GETs the MP3 by key, decodes it to pcm_s16le/24k/mono
ONCE (memoized in-process), and plays it with AiraBotClient.play_cached_clip — zero
live TTS, zero Cartesia slot. Everything degrades gracefully: Redis down, key missing,
or decode failure → get_pcm() returns None and the caller falls back to live TTS.

The claim response's `question_bank` field (see QuestionBankPlan) is job-authored by
the campus backend; scripts/generate_cached_audio.py --banks --redis is the reference
producer that renders + uploads it.
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("question_bank_cache")

REDIS_URL = (os.getenv("REDIS_URL", "") or "").strip()
QBANK_SAMPLE_RATE = 24_000

try:
    import redis as _redis
    from redis.exceptions import RedisError as _RedisError
except Exception:  # redis not installed → cache disabled, live TTS fallback
    _redis = None
    class _RedisError(Exception):
        pass


class QuestionAudioCache:
    """Fetches (and, for the producer, stores) cached question audio in Redis.

    Binary-safe client (decode_responses=False) separate from api_key_pool's string
    client. In-process PCM memo so a clip decodes at most once per interview process.
    """

    def __init__(self, url: str = None):
        self.url = (url or REDIS_URL or "").strip()
        self._client = None
        self._pcm_memo: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self._down = False   # sticky after a hard failure; get_pcm just returns None

    @property
    def available(self) -> bool:
        return bool(self.url and _redis is not None and not self._down)

    def _c(self):
        if self._client is None:
            self._client = _redis.from_url(
                self.url, decode_responses=False,
                socket_timeout=2, socket_connect_timeout=2, health_check_interval=30,
            )
        return self._client

    # ── consumer ─────────────────────────────────────────────────────────────
    def get_pcm(self, audio_key: str) -> "bytes | None":
        """Return pcm_s16le/24k/mono bytes for `audio_key`, or None (→ live TTS).

        MP3 in Redis → decoded once and memoized. Any error (no Redis, miss, decode
        failure) returns None so the caller synthesizes live instead.
        """
        if not audio_key or not self.available:
            return None
        memo = self._pcm_memo.get(audio_key)
        if memo is not None:
            return memo
        try:
            raw = self._c().get(audio_key)
        except _RedisError as e:
            logger.warning("⚠️ [qbank] Redis GET failed (%s) — live TTS fallback", e)
            return None
        except Exception as e:
            logger.warning("⚠️ [qbank] Redis unavailable (%s) — disabling cache for this process", e)
            self._down = True
            return None
        if not raw:
            return None
        pcm = self._to_pcm(raw)
        if pcm:
            with self._lock:
                self._pcm_memo[audio_key] = pcm
        return pcm

    @staticmethod
    def _to_pcm(data: bytes) -> "bytes | None":
        """Decode stored audio (MP3/other) → pcm_s16le/24k/mono. Passes raw PCM through
        if the producer stored PCM directly (even byte length, no MP3/ID3 header)."""
        # Fast path: looks like raw PCM already (no MP3 frame sync / ID3 tag).
        if data[:3] != b"ID3" and (len(data) < 2 or data[0] != 0xFF):
            if len(data) % 2 == 0:
                return data
        try:
            import io
            from pydub import AudioSegment
            seg = AudioSegment.from_file(io.BytesIO(data))
            seg = seg.set_frame_rate(QBANK_SAMPLE_RATE).set_channels(1).set_sample_width(2)
            return seg.raw_data
        except Exception as e:
            logger.warning("⚠️ [qbank] audio decode failed (%s)", e)
            return None

    # ── producer (reference: scripts/generate_cached_audio.py --banks --redis) ──
    def put_audio(self, audio_key: str, data: bytes, ttl_seconds: int = None) -> bool:
        """Store audio bytes (MP3 recommended) under `audio_key`. Returns True on success."""
        if not audio_key or not data or not self.available:
            return False
        try:
            self._c().set(audio_key, data, ex=ttl_seconds)
            return True
        except Exception as e:
            logger.warning("⚠️ [qbank] Redis SET failed for %s (%s)", audio_key, e)
            return False


class QuestionBankPlan:
    """Sequential consumer of the `question_bank` field on a claimed interview row.

    Schema (job-authored by the campus backend; see the reference producer
    scripts/generate_cached_audio.py --banks --redis):

        {
          "sample_rate": 24000,
          "job_id": "...",
          "groups": [
            {"key": "python", "skills": ["Python"], "questions": [
                {"id": "q1", "text": "...", "audio_key": "qbank:JOB:python:q1", "rubric": "..."},
                ...
            ]},
            {"key": "python+sql", "skills": ["Python","SQL"], "questions": [...]},
            ...
          ]
        }

    Groups are asked SEQUENTIALLY in list order (single-skill groups first, then combos —
    the order the producer emits), one question at a time within each group. A dict shape
    {group_key: {"skills": [...], "questions": [...]}} is also accepted and ordered by
    insertion. next() yields each question dict once; None when exhausted.
    """

    def __init__(self, question_bank):
        self.sample_rate = int((question_bank or {}).get("sample_rate", QBANK_SAMPLE_RATE)) \
            if isinstance(question_bank, dict) else QBANK_SAMPLE_RATE
        self._queue = self._flatten(question_bank)
        self._cursor = 0

    @staticmethod
    def _flatten(qb) -> list:
        if not qb or not isinstance(qb, dict):
            return []
        groups = qb.get("groups")
        if groups is None:
            # tolerate the dict shape {group_key: {skills, questions}}
            groups = [{"key": k, **(v if isinstance(v, dict) else {})} for k, v in qb.items()
                      if isinstance(v, dict) and "questions" in v]
        out = []
        for g in groups or []:
            gkey = g.get("key", "")
            gskills = g.get("skills", [])
            for q in g.get("questions", []) or []:
                if not isinstance(q, dict) or not q.get("text"):
                    continue
                out.append({
                    "id": q.get("id", ""),
                    "text": q["text"],
                    "audio_key": q.get("audio_key", ""),
                    "rubric": q.get("rubric", ""),
                    "group": gkey,
                    "skills": gskills,
                })
        return out

    @property
    def available(self) -> bool:
        return bool(self._queue)

    @property
    def remaining(self) -> int:
        return max(0, len(self._queue) - self._cursor)

    def next(self) -> "dict | None":
        """Return the next question dict (sequential), or None when exhausted."""
        if self._cursor >= len(self._queue):
            return None
        q = self._queue[self._cursor]
        self._cursor += 1
        return q


# Process-wide singleton (one Redis connection per interview process).
_CACHE: QuestionAudioCache | None = None


def get_audio_cache() -> QuestionAudioCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = QuestionAudioCache()
    return _CACHE
