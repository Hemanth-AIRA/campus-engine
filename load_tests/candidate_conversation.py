"""
load_tests/candidate_conversation.py
A CONVERSATIONAL fake candidate that actually takes turns with the bot.

Why this exists
───────────────
The original candidate_sim.py streamed a random WAV continuously for the whole
duration. The bot's Deepgram endpointing detects end-of-turn by the candidate
going SILENT — but a non-stop wall of audio never goes silent, so turns never
resolve and the "interview" is gibberish. It load-tested the plumbing, not the
interview flow.

This module listens to the BOT's audio track and behaves like a real person:
  1. Stay silent while the bot is talking (asking a question).
  2. When the bot goes quiet for ~1s → its turn ended → play ONE pre-generated
     answer clip from load_tests/audio_samples/.
  3. Go silent again and wait for the bot's NEXT question.
  4. Repeat until the bot ends the interview (no new question for a while),
     the room disconnects, or max_answers / max_duration is hit.

It still publishes a face-image video track (load_tests/video_gen) and registers
Socket.IO presence with Connect (reused from candidate_sim) so the engine sees
it as a real candidate.
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
from livekit import rtc
from livekit.rtc import AudioFrame, AudioSource, LocalAudioTrack, Room

from .audio_gen import (
    SAMPLE_RATE, CHANNELS, FRAME_MS, FRAME_SAMPLES,
    _list_wav_files, _read_wav_as_pcm16_48k,
)
from .candidate_sim import fetch_livekit_token, _candidate_socketio_presence
from .config import CONNECT_BASE_URL
from .video_gen import stream_face_video

logger = logging.getLogger("candidate_conversation")

# ── Turn-taking tuning ───────────────────────────────────────────────────────
BOT_RMS_THRESHOLD   = 0.012   # above this on the bot track = bot is speaking
BOT_SILENCE_MS      = 1100    # bot quiet this long after speaking = turn ended
ANSWER_LEAD_MS      = 450     # small human-like pause before answering
NO_QUESTION_TIMEOUT = 30.0    # no new bot speech this long after an answer = interview over
FRAME_S             = FRAME_MS / 1000.0


@dataclass
class ConversationResult:
    room_id:         str
    candidate_name:  str
    connected:       bool = False
    bot_joined:      bool = False
    bot_audio_heard: bool = False
    answers_given:   int = 0
    questions_heard: int = 0
    duration_s:      float = 0.0
    ended_reason:    str = ""
    error:           Optional[str] = None
    timeline:        list = field(default_factory=list)

    def event(self, msg: str):
        self.timeline.append(f"{time.strftime('%H:%M:%S')} | {msg}")
        logger.info("[%s] %s", self.room_id, msg)


def _rms(pcm16: bytes) -> float:
    if not pcm16:
        return 0.0
    a = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a * a)))


class _BotVoiceMonitor:
    """Tracks whether the bot is currently speaking by watching its audio RMS."""

    def __init__(self, room_id: str):
        self.room_id = room_id
        self._last_voice_t = 0.0       # monotonic time of last loud bot frame
        self._spoke_since_reset = False
        self._task: Optional[asyncio.Task] = None
        self._stop = False

    def start(self, track):
        self._task = asyncio.create_task(self._run(track))

    async def _run(self, track):
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=CHANNELS)
        try:
            async for ev in stream:
                if self._stop:
                    break
                if _rms(bytes(ev.frame.data)) >= BOT_RMS_THRESHOLD:
                    self._last_voice_t = time.monotonic()
                    self._spoke_since_reset = True
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("[%s] bot voice monitor error: %s", self.room_id, e)
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass

    def silent_for_ms(self) -> float:
        if self._last_voice_t == 0.0:
            return 0.0
        return (time.monotonic() - self._last_voice_t) * 1000.0

    @property
    def spoke_since_reset(self) -> bool:
        return self._spoke_since_reset

    def reset_turn(self):
        """Call after we answer: wait for the bot's NEXT utterance afresh."""
        self._spoke_since_reset = False
        self._last_voice_t = 0.0

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()


async def _play_answer(audio_source, pcm: bytes, max_seconds: float = 0.0):
    """Stream one answer clip to the candidate's audio track in real time.
    If max_seconds > 0, only the first max_seconds are streamed (keeps answers
    short → much faster sims, natural pitch, still a clean transcript)."""
    bytes_per_frame = FRAME_SAMPLES * 2  # PCM16 mono
    total = len(pcm)
    if max_seconds and max_seconds > 0:
        cap = int(max_seconds * SAMPLE_RATE) * 2  # PCM16 mono bytes
        total = min(total, cap)
    off = 0
    while off < total:
        chunk = pcm[off:off + bytes_per_frame]
        off += bytes_per_frame
        if len(chunk) < bytes_per_frame:
            chunk = chunk.ljust(bytes_per_frame, b"\x00")
        await audio_source.capture_frame(AudioFrame(
            data=chunk, sample_rate=SAMPLE_RATE,
            num_channels=CHANNELS, samples_per_channel=FRAME_SAMPLES,
        ))
        await asyncio.sleep(FRAME_S)


async def run_conversational_candidate(
    room_id: str,
    candidate_name: str,
    *,
    livekit_url: str = None,
    livekit_token: str = None,
    candidate_join_url: str = None,
    interview_id: int = 0,
    max_answers: int = 25,
    max_duration_s: float = 1200.0,
    wait_for_bot_s: float = 30.0,
    answer_max_s: float = 15.0,   # cap each answer's playback (0 = full clip)
) -> ConversationResult:
    result = ConversationResult(room_id=room_id, candidate_name=candidate_name)
    t0 = time.perf_counter()

    # Pre-load answer clips (cycled, so each turn sounds different).
    wavs = _list_wav_files()
    if not wavs:
        result.error = "No answer WAVs in load_tests/audio_samples/ — run gen_audio_samples.py"
        result.event("❌ " + result.error)
        return result
    answer_pcms: List[bytes] = [_read_wav_as_pcm16_48k(w) for w in wavs]
    result.event(f"📚 Loaded {len(answer_pcms)} answer clips")

    # ── Token ──
    try:
        if livekit_url and livekit_token:
            url, token = livekit_url, livekit_token
        else:
            tok = await fetch_livekit_token(room_id, candidate_name)
            url, token = tok["url"], tok["token"]
    except Exception as e:
        result.error = f"Token fetch failed: {e}"
        result.event("❌ " + result.error)
        return result

    room = Room()
    bot_audio_event = asyncio.Event()
    bot_track_ref: list = []
    disc = {"flag": False}   # set by the disconnected handler (protobuf enum is unreliable)

    @room.on("participant_connected")
    def _on_part(p):
        result.event(f"👤 Participant joined: {p.identity}")
        if p.identity.startswith("bot"):
            result.bot_joined = True

    @room.on("track_subscribed")
    def _on_track(track, pub, p):
        if p.identity.startswith("bot") and track.kind == rtc.TrackKind.KIND_AUDIO:
            result.bot_joined = True
            result.bot_audio_heard = True
            bot_track_ref.append(track)
            bot_audio_event.set()
            result.event("🎧 Bot audio track captured")

    @room.on("disconnected")
    def _on_disc(reason):
        disc["flag"] = True
        result.event(f"🔌 Disconnected: {reason}")

    # ── Connect ──
    try:
        await room.connect(url, token)
        result.connected = True
        result.event(f"✅ Connected ({(time.perf_counter()-t0)*1000:.0f}ms)")
    except Exception as e:
        result.error = f"LiveKit connect failed: {e}"
        result.event("❌ " + result.error)
        return result

    # Bot may already be present.
    for rp in room.remote_participants.values():
        if rp.identity.startswith("bot"):
            result.bot_joined = True
            for pub in rp.track_publications.values():
                if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    bot_track_ref.append(pub.track)
                    bot_audio_event.set()
                    result.bot_audio_heard = True

    # ── Socket.IO presence (so the engine sees us as the candidate) ──
    presence_stop = threading.Event()
    if candidate_join_url:
        threading.Thread(
            target=_candidate_socketio_presence,
            args=(CONNECT_BASE_URL, candidate_join_url, room_id, candidate_name, presence_stop),
            daemon=True, name=f"presence-{room_id}",
        ).start()
        result.event("🔗 Socket.IO presence started")

    # ── Publish audio + video ──
    audio_source = AudioSource(sample_rate=SAMPLE_RATE, num_channels=CHANNELS)
    await room.local_participant.publish_track(
        LocalAudioTrack.create_audio_track("mic", audio_source))
    video_source = None
    try:
        from livekit.rtc import VideoSource, LocalVideoTrack
        video_source = VideoSource(width=640, height=480)
        await room.local_participant.publish_track(
            LocalVideoTrack.create_video_track("camera", video_source))
        result.event("📹 Face video published")
    except Exception as e:
        result.event(f"⚠️  Video skipped: {e}")

    video_task = (asyncio.create_task(
        stream_face_video(video_source, interview_id=interview_id, duration_s=max_duration_s))
        if video_source else None)

    # ── Wait for the bot's audio track, then start the voice monitor ──
    try:
        await asyncio.wait_for(asyncio.shield(bot_audio_event.wait()), wait_for_bot_s)
        result.event("🤖 Bot audio confirmed")
    except asyncio.TimeoutError:
        result.error = f"Bot audio not seen after {wait_for_bot_s}s"
        result.event("⚠️  " + result.error)

    monitor = _BotVoiceMonitor(room_id)
    if bot_track_ref:
        monitor.start(bot_track_ref[0])

    # ── Conversational turn-taking loop ──
    result.event("💬 Turn-taking loop started")
    try:
        while True:
            if (time.perf_counter() - t0) > max_duration_s:
                result.ended_reason = "max_duration"
                break
            if result.answers_given >= max_answers:
                result.ended_reason = "max_answers"
                break
            if disc["flag"]:
                result.ended_reason = "disconnected"
                break

            # Wait for the bot to FINISH its current question:
            # it must have spoken since our last answer, then gone quiet ~1.1s.
            waited = 0.0
            while True:
                await asyncio.sleep(0.1)
                waited += 0.1
                if monitor.spoke_since_reset and monitor.silent_for_ms() >= BOT_SILENCE_MS:
                    break  # bot's turn ended → our cue to answer
                # If the bot never speaks again after we answered, interview is over.
                if not monitor.spoke_since_reset and waited >= NO_QUESTION_TIMEOUT:
                    result.ended_reason = "no_more_questions"
                    break
                if (time.perf_counter() - t0) > max_duration_s:
                    result.ended_reason = "max_duration"
                    break
            if result.ended_reason:
                break

            result.questions_heard += 1
            await asyncio.sleep(ANSWER_LEAD_MS / 1000.0)

            pcm = answer_pcms[result.answers_given % len(answer_pcms)]
            full_s = len(pcm) / 2 / SAMPLE_RATE
            played_s = min(full_s, answer_max_s) if answer_max_s > 0 else full_s
            result.event(f"🗣️  Answering Q{result.questions_heard} "
                         f"(clip {result.answers_given % len(answer_pcms) + 1}, "
                         f"{played_s:.1f}s of {full_s:.1f}s)")
            await _play_answer(audio_source, pcm, max_seconds=answer_max_s)
            result.answers_given += 1
            monitor.reset_turn()  # now wait for the bot's NEXT question afresh

    except asyncio.CancelledError:
        result.ended_reason = result.ended_reason or "cancelled"
    except Exception as e:
        result.error = f"Turn loop error: {e}"
        result.event("❌ " + result.error)

    # ── Teardown ──
    result.duration_s = time.perf_counter() - t0
    result.event(f"👋 Leaving after {result.duration_s:.1f}s "
                 f"| answers={result.answers_given} | reason={result.ended_reason or 'n/a'}")
    monitor.stop()
    if video_task:
        video_task.cancel()
    presence_stop.set()
    try:
        await room.disconnect()
    except Exception:
        pass
    return result
