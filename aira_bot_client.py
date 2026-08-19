"""
aira_bot_client.py

LiveKit-based bot client. Replaces Selenium/Chrome entirely.
The bot joins a LiveKit room as a native participant, receives candidate
audio frames, streams them to Deepgram, and publishes TTS audio back.

No browser. No base64. No JS injection.

Replaces:
  - record_transcribe_webrtc_audio()       (webrtc_interview.py L1050-1178)
  - record_transcribe_webrtc_streaming()   (webrtc_interview.py L1181-1757)
  - play_audio_in_webrtc()                 (webrtc_interview.py L1759-1898)
  - ai_speak()                             (webrtc_interview.py L8989-9068)
  - ai_speak_with_audio()                  (webrtc_interview.py L9071+)
  - ai_speak_streamed_text()               (webrtc_interview.py L8753+)
  - ai_speak_streaming()                   (webrtc_interview.py L8541+)
  - play_ack_with_bargein()                (webrtc_interview.py L8004+)
"""

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ─── Deepgram config ────────────────────────────────────────────
DG_STT_URL = (
    "wss://api.deepgram.com/v1/listen?"
    "model=nova-2&language=en&smart_format=true"
    "&encoding=linear16&sample_rate=48000&channels=1"
)


class AiraBotClient:
    """
    Native LiveKit bot participant. Drop-in replacement for Selenium/Chrome.

    Lifecycle:
        bot = AiraBotClient(livekit_url, room_id, bot_token)
        await bot.connect()

        # Record candidate speech with server-side VAD
        pcm_bytes = await bot.record_audio(silence_ms=3000)

        # Transcribe via Deepgram (direct Python→DG, no browser relay)
        transcript = await bot.stream_to_deepgram(pcm_bytes)

        # Play TTS audio to candidate (via LiveKit track, no base64)
        await bot.play_audio(mp3_bytes, format="mp3")

        # Play with barge-in detection
        result = await bot.play_with_bargein(mp3_bytes, format="mp3")
        if result["interrupted"]:
            # candidate spoke over the bot
            pass

        await bot.close()
    """

    # TTS output rate — 24kHz matches OpenAI TTS-1 output.
    # LiveKit handles Opus encoding and resampling internally.
    PLAYBACK_SAMPLE_RATE = 24000
    PLAYBACK_CHANNELS = 1

    def __init__(self, livekit_url: str, room_id: str, bot_token: str):
        self.livekit_url = livekit_url
        self.room_id = room_id
        self.bot_token = bot_token

        self.room = None            # type: Optional[rtc.Room]
        self._audio_source = None   # type: Optional[rtc.AudioSource]
        self._audio_track = None    # type: Optional[rtc.LocalAudioTrack]

        # ── Incoming audio state ──
        self._incoming_buffer = deque()   # PCM16 byte chunks from candidate
        self._recording = False
        self._rms_history = deque(maxlen=100)  # ~2s of RMS at 20ms frames

        # ── Real-time Deepgram streaming queue ──
        # Frames are pushed here during recording and consumed by the DG WS sender
        self._realtime_dg_queue: Optional[asyncio.Queue] = None

        # ── Pre-warmed Deepgram socket (opened during bot speech) ──
        # Opening the DG WS costs ~800ms. prewarm_dg() opens it while the bot is
        # still talking; record_and_transcribe_realtime() adopts it so the
        # handshake never sits on the candidate's turn.
        self._prewarm_ws = None                  # open DG websocket awaiting adoption
        self._prewarm_keepalive_task = None      # KeepAlive loop while idle
        self._prewarming = False                 # guard against concurrent prewarm
        self._prewarm_lease = None               # keypool lease held by the prewarmed socket

        # ── Playback control ──
        self._stop_playback = asyncio.Event()

        # ── Connection state ──
        self._connected = False
        self._candidate_present = False
        self._audio_consume_task = None   # background task for incoming audio
        self._video_consume_task = None   # background task for incoming video

        # ── Face detector (replaces Sentinel's pico.js) ──
        self._face_detector = None
        self._interview_id = None
        self._process_id = None

        # ── Local recorder (injected by orchestrator) ──
        self._local_recorder = None

        # ── Filler audio (masks Cartesia key-pool waits) ──
        # play_filler() is passed to the keypool as on_wait; it plays a short
        # pre-rendered clip while a Cartesia slot is unavailable. Phase is set by
        # the interview flow before each question (set_filler_phase).
        self._recent_fillers = deque(maxlen=4)   # clip ids, for no-immediate-repeat
        self._filler_phase = "mid"

        # ── Held Cartesia lease (one slot across a turn's ack + question) ──
        # Acquired on the turn's first Cartesia speak and held until the bot starts
        # listening, so a filler can't intrude between the ack and the question.
        self._speak_lease = None
        self._speak_key = None

    # ═══════════════════════════════════════════════════════════
    # CONNECTION
    # ═══════════════════════════════════════════════════════════

    async def connect(self):
        """Connect to the LiveKit room as the bot participant."""
        from livekit import rtc

        self.room = rtc.Room()
        self._connect_t0 = time.time()  # ⏱ wall clock when bot begins connecting

        # Register event handlers BEFORE connecting
        self.room.on("participant_connected", self._on_participant_connected)
        self.room.on("participant_disconnected", self._on_participant_disconnected)
        self.room.on("track_subscribed", self._on_track_subscribed)
        self.room.on("track_unsubscribed", self._on_track_unsubscribed)
        self.room.on("disconnected", self._on_disconnected)

        # AudioSource jitter buffer: at most this many ms of audio is buffered
        # inside LiveKit before capture_frame() blocks. It's what absorbs event-loop
        # scheduling jitter — when many interviews run concurrently the loop gets
        # delayed (CPU contention / GIL), and if a stall exceeds the buffer the voice
        # underruns ("breaking"). 200ms is too tight under load; default higher and
        # make it tunable. Trade-off: a few hundred ms more playback latency, which is
        # fine for an interview. Tune with AIRA_BOT_AUDIO_BUFFER_MS.
        _buf_ms = int(os.environ.get("AIRA_BOT_AUDIO_BUFFER_MS", "400"))
        self._audio_source = rtc.AudioSource(
            sample_rate=self.PLAYBACK_SAMPLE_RATE,
            num_channels=self.PLAYBACK_CHANNELS,
            queue_size_ms=_buf_ms,
        )
        self._audio_track = rtc.LocalAudioTrack.create_audio_track(
            "bot-voice", self._audio_source
        )

        t_connect = time.time()
        await self.room.connect(
            self.livekit_url,
            self.bot_token,
            options=rtc.RoomOptions(auto_subscribe=True),
        )
        connect_ms = (time.time() - t_connect) * 1000

        # Publish the bot's outbound audio track
        await self.room.local_participant.publish_track(
            self._audio_track,
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )

        self._connected = True
        logger.info(
            "⏱ TIMELINE [room %s] [T+0ms] BOT CONNECTED to LiveKit SFU "
            "(connect_rtt=%.0fms) | local_identity=%s",
            self.room_id, connect_ms,
            self.room.local_participant.identity if self.room.local_participant else "?",
        )

        # Check for participants that joined before us
        for participant in self.room.remote_participants.values():
            logger.info(
                "⏱ TIMELINE [room %s] PRE-EXISTING participant in room: %s",
                self.room_id, participant.identity,
            )
            # Mark candidate as present — _on_participant_connected won't fire
            # for pre-existing participants, so we must set this here.
            if not participant.identity.startswith("bot"):
                self._candidate_present = True
                self._candidate_joined_t = time.time()
            for pub in participant.track_publications.values():
                if pub.track and pub.track.kind == rtc.TrackKind.KIND_AUDIO:
                    logger.info(
                        "⏱ TIMELINE [room %s] Pre-existing AUDIO TRACK found — starting consume immediately",
                        self.room_id,
                    )
                    self._audio_consume_task = asyncio.create_task(
                        self._process_incoming_audio(pub.track)
                    )
                elif pub.track and pub.track.kind == rtc.TrackKind.KIND_VIDEO:
                    logger.info(
                        "⏱ TIMELINE [room %s] Pre-existing VIDEO TRACK found — starting capture",
                        self.room_id,
                    )
                    self._video_consume_task = asyncio.create_task(
                        self._process_incoming_video(pub.track)
                    )

    async def close(self):
        """Disconnect from the room and clean up."""
        # Backstop: never leak a held Cartesia lease past the interview.
        self.release_speak_lease()
        if self._audio_consume_task:
            self._audio_consume_task.cancel()
            try:
                await self._audio_consume_task
            except asyncio.CancelledError:
                pass
        if self._video_consume_task:
            self._video_consume_task.cancel()
            try:
                await self._video_consume_task
            except asyncio.CancelledError:
                pass
        # Close any pre-warmed DG socket that was never adopted (e.g. bot spoke
        # a closing line and the interview ended without another record turn).
        if self._prewarm_keepalive_task is not None:
            self._prewarm_keepalive_task.cancel()
            self._prewarm_keepalive_task = None
        if self._prewarm_ws is not None:
            try:
                await self._prewarm_ws.close()
            except Exception:
                pass
            self._prewarm_ws = None
            try:
                from api_key_pool import release_key
                release_key("deepgram", self._prewarm_lease)
            except Exception:
                pass
            self._prewarm_lease = None
        if self.room:
            await self.room.disconnect()
        self._connected = False
        logger.info("🔌 [room %s] Bot disconnected from LiveKit", self.room_id)

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def candidate_present(self) -> bool:
        return self._candidate_present

    @property
    def has_remote_audio(self) -> bool:
        """True if we have an active audio consume task (candidate track subscribed)."""
        return self._audio_consume_task is not None and not self._audio_consume_task.done()

    # ═══════════════════════════════════════════════════════════
    # EVENT HANDLERS
    # ═══════════════════════════════════════════════════════════

    def _on_participant_connected(self, participant):
        elapsed_ms = (time.time() - self._connect_t0) * 1000 if hasattr(self, '_connect_t0') else 0
        is_candidate = not participant.identity.startswith("bot")
        role_label = "CANDIDATE" if is_candidate else "BOT/OTHER"
        logger.info(
            "⏱ TIMELINE [room %s] [T+%.0fms] %s JOINED LiveKit room | identity=%s",
            self.room_id, elapsed_ms, role_label, participant.identity,
        )
        # Any non-bot participant is treated as candidate for audio purposes
        if is_candidate:
            self._candidate_present = True
            self._candidate_joined_t = time.time()  # ⏱ stamp for downstream delay calcs

    def _on_participant_disconnected(self, participant):
        elapsed_ms = (time.time() - self._connect_t0) * 1000 if hasattr(self, '_connect_t0') else 0
        is_candidate = not participant.identity.startswith("bot")
        logger.info(
            "⏱ TIMELINE [room %s] [T+%.0fms] %s LEFT LiveKit room | identity=%s",
            self.room_id, elapsed_ms,
            "CANDIDATE" if is_candidate else "BOT/OTHER",
            participant.identity,
        )
        if is_candidate:
            self._candidate_present = False
            # Unblock DG streaming queue with sentinel — stops WS sender task
            if self._realtime_dg_queue is not None:
                try:
                    self._realtime_dg_queue.put_nowait(None)  # sentinel = stop
                except asyncio.QueueFull:
                    pass

    def _on_track_subscribed(self, track, publication, participant):
        from livekit import rtc
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            elapsed_ms = (time.time() - self._connect_t0) * 1000 if hasattr(self, '_connect_t0') else 0
            delay_since_join = (
                (time.time() - self._candidate_joined_t) * 1000
                if hasattr(self, '_candidate_joined_t') else 0
            )
            logger.info(
                "⏱ TIMELINE [room %s] [T+%.0fms] CANDIDATE AUDIO TRACK SUBSCRIBED "
                "(%.0fms after candidate joined) | identity=%s — DG mic now ready",
                self.room_id, elapsed_ms, delay_since_join, participant.identity,
            )
            self._track_subscribed_t = time.time()  # ⏱ when mic went live
            self._audio_consume_task = asyncio.create_task(
                self._process_incoming_audio(track)
            )
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            logger.info(
                "📹 TIMELINE [room %s] CANDIDATE VIDEO TRACK SUBSCRIBED | identity=%s",
                self.room_id, participant.identity,
            )
            self._video_consume_task = asyncio.create_task(
                self._process_incoming_video(track)
            )

    def _on_track_unsubscribed(self, track, publication, participant):
        logger.info(
            "⏱ TIMELINE [room %s] AUDIO TRACK UNSUBSCRIBED | identity=%s",
            self.room_id, participant.identity,
        )
        # NOTE: Do NOT set _candidate_present = False here.
        # Track unsubscribe fires during normal WebRTC re-negotiation even while
        # the participant is still connected. The remote_participants check has a
        # race condition where the participant is momentarily absent during resub.
        # Real disconnects are handled by _on_participant_disconnected which is
        # the reliable signal that the candidate truly left the room.


    def _on_disconnected(self, reason):
        elapsed_ms = (time.time() - self._connect_t0) * 1000 if hasattr(self, '_connect_t0') else 0
        logger.warning(
            "⏱ TIMELINE [room %s] [T+%.0fms] BOT DISCONNECTED from LiveKit | reason=%s",
            self.room_id, elapsed_ms, reason,
        )
        self._connected = False

    # ═══════════════════════════════════════════════════════════
    # INCOMING VIDEO PROCESSING
    # ═══════════════════════════════════════════════════════════

    async def _process_incoming_video(self, track):
        """
        Continuously receive video frames from the candidate's camera track.
        Converts frames to RGBA and writes them to the LocalVideoRecorder.
        Runs as a background asyncio task for the lifetime of the track.
        """
        from livekit import rtc

        video_stream = rtc.VideoStream(track)
        frame_count = 0

        logger.info(
            "📹 [room %s] Video capture loop started",
            self.room_id,
        )

        try:
            async for frame_event in video_stream:
                if not self._connected:
                    break

                frame: rtc.VideoFrame = frame_event.frame

                # Convert to RGBA for FFmpeg
                try:
                    rgba_frame = frame.convert(rtc.VideoBufferType.RGBA)
                    rgba_data = bytes(rgba_frame.data)
                    width = rgba_frame.width
                    height = rgba_frame.height
                except Exception:
                    continue

                # Forward to recorder (it handles FPS throttling internally)
                if self._local_recorder and self._local_recorder.is_active:
                    self._local_recorder.write_video_frame(rgba_data, width, height)

                # ── Face detection (replaces Sentinel pico.js) ──
                # Runs in-memory only; no DB write needed since detection
                # is now in the same process. Read state via face_detector.is_present.
                if self._face_detector:
                    try:
                        self._face_detector.process_frame(
                            rgba_data, width, height
                        )
                        # Best-face capture — cheap no-op unless a capture window
                        # was opened by request_capture() just after an ack.
                        self._face_detector.maybe_capture_best_face(
                            rgba_data, width, height
                        )
                    except Exception as e:
                        if frame_count % 150 == 0:  # log sparingly
                            logger.warning(
                                "⚠ [room %s] Face detection error: %s",
                                self.room_id, e,
                            )

                frame_count += 1
                if frame_count == 1:
                    logger.info(
                        "📹 [room %s] First video frame captured (%dx%d)",
                        self.room_id, width, height,
                    )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                "❌ [room %s] Video capture error: %s",
                self.room_id, e,
            )
        finally:
            await video_stream.aclose()
            logger.info(
                "📹 [room %s] Video capture loop ended (%d frames total)",
                self.room_id, frame_count,
            )

    # ═══════════════════════════════════════════════════════════
    # FACE DETECTION (replaces Sentinel pico.js)
    # ═══════════════════════════════════════════════════════════

    def init_face_detector(self, interview_id, process_id):
        """
        Initialize the face detector for this interview.
        Called by the orchestrator after bot connects.
        """
        try:
            from face_detector import FaceDetector
            self._interview_id = interview_id
            self._process_id = process_id
            self._face_detector = FaceDetector(
                interview_id=interview_id,
                room_id=self.room_id,
                process_id=process_id,
            )
            logger.info(
                "🛡 [process id: %s][room: %s] Face detector initialized for interview %s",
                process_id, self.room_id, interview_id,
            )
        except Exception as e:
            logger.error(
                "❌ [process id: %s][room: %s] Failed to init face detector: %s",
                process_id, self.room_id, e,
            )
            self._face_detector = None



    # ═══════════════════════════════════════════════════════════
    # INCOMING AUDIO PROCESSING
    # ═══════════════════════════════════════════════════════════

    async def _process_incoming_audio(self, track):
        """
        Continuously receive audio frames from the candidate's track.

        AudioStream SDK defaults: sample_rate=48000, num_channels=1.
        We explicitly set these to ensure Deepgram URL params match.
        frame_size_ms=20 ensures frames arrive in consistent 20ms chunks
        that match our VAD tick rate (frame_ms=20).
        """
        from livekit import rtc
        # Explicit sample_rate=48000 matches Deepgram URL (&sample_rate=48000)
        # frame_size_ms=20 ensures consistent chunk sizes for VAD accuracy
        audio_stream = rtc.AudioStream(
            track,
            sample_rate=48000,
            num_channels=1,
            frame_size_ms=20,
        )

        try:
            async for event in audio_stream:
                frame = event.frame
                raw_bytes = bytes(frame.data)

                # Buffer raw PCM for record_audio()
                if self._recording:
                    self._incoming_buffer.append(raw_bytes)

                # Write to local recorder (candidate side)
                if self._local_recorder:
                    self._local_recorder.write_candidate(raw_bytes)

                # Feed real-time Deepgram streaming queue
                if self._realtime_dg_queue is not None:
                    try:
                        self._realtime_dg_queue.put_nowait(raw_bytes)
                    except asyncio.QueueFull:
                        pass  # drop frame if queue is full — DG WS sender is behind

                # Compute RMS for VAD (used by record_audio + play_with_bargein)
                samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
                if len(samples) > 0:
                    rms = float(np.sqrt(np.mean(samples ** 2)) / 32768.0)
                    self._rms_history.append(rms)
        except asyncio.CancelledError:
            logger.debug("[room %s] Audio consume task cancelled", self.room_id)
        except Exception as e:
            logger.error(
                "❌ [room %s] Audio consume error: %s", self.room_id, e,
                exc_info=True,
            )

    def get_current_rms(self) -> float:
        """Return the most recent RMS value (for external VAD checks)."""
        return self._rms_history[-1] if self._rms_history else 0.0

    # ═══════════════════════════════════════════════════════════
    # RECORDING — replaces record_transcribe_webrtc_*()
    # ═══════════════════════════════════════════════════════════

    async def record_audio(
        self,
        silence_ms: int = 3000,
        max_ms: int = 120_000,
        silence_threshold: float = 0.015,
        min_speech_ms: int = 500,
    ) -> bytes:
        """
        Record candidate audio with server-side VAD.

        Stops when:
        1. silence_ms of continuous silence after at least min_speech_ms of speech
        2. max_ms total time elapsed

        Returns raw PCM16 bytes (at the track's native sample rate).

        This is the CORE latency improvement:
        - No browser JS executing VAD in 200ms polling ticks
        - No base64 encoding of the audio blob
        - No Selenium script timeout management
        - Direct PCM frames from WebRTC via LiveKit
        - 20ms tick resolution (10x better than browser's 200ms)
        """
        # ── Wait for candidate audio track to appear ──
        # The candidate may connect to LiveKit slightly after recording starts.
        # Wait up to 60s for _on_track_subscribed to fire.
        wait_start = time.time()
        TRACK_WAIT_TIMEOUT = 60  # seconds (increased from 30 — gives candidate time to unmute)
        while not self.has_remote_audio:
            elapsed_wait = time.time() - wait_start
            if elapsed_wait > TRACK_WAIT_TIMEOUT:
                logger.error(
                    "❌ [room %s] No candidate audio track after %.0fs — "
                    "candidate may not be in the LiveKit room. "
                    "Ensure candidate URL has ?livekit=1",
                    self.room_id, elapsed_wait,
                )
                return b""  # Return empty — caller handles gracefully
            # Early warning at 10s — mic may be blocked
            if int(elapsed_wait) == 10:
                logger.warning(
                    "⚠️ [room %s] 10s elapsed, still no candidate audio track —"
                    " mic may be blocked or candidate not yet in LiveKit room.",
                    self.room_id,
                )
            if int(elapsed_wait) % 5 == 0 and int(elapsed_wait) > 0:
                remote_ids = [p.identity for p in self.room.remote_participants.values()] if self.room else []
                logger.info(
                    "⏳ [room %s] Waiting for candidate audio track... (%.0fs) "
                    "remote_participants=%s",
                    self.room_id, elapsed_wait, remote_ids,
                )
            await asyncio.sleep(0.5)

        logger.info(
            "✅ [room %s] Candidate audio track detected, starting recording",
            self.room_id,
        )

        self._recording = True
        self._incoming_buffer.clear()

        # Frame timing — LiveKit typically delivers 20ms frames
        frame_ms = 20
        silence_frames_needed = silence_ms // frame_ms
        min_speech_frames = min_speech_ms // frame_ms
        max_frames = max_ms // frame_ms

        silence_frames = 0
        speech_frames = 0
        total_frames = 0
        _vad_speech_started = False  # ⏱ track first speech detect

        start = time.time()
        logger.info(
            "⏱ TIMELINE [room %s] RECORDING STARTED (silence=%dms max=%dms)",
            self.room_id, silence_ms, max_ms,
        )

        while total_frames < max_frames:
            await asyncio.sleep(frame_ms / 1000.0)
            total_frames += 1

            # ── Candidate-left watchdog ──────────────────────────────────────
            # When candidate disconnects from LiveKit, _rms_history stops
            # updating. Without this check the loop reads the stale last RMS
            # forever and never exits (stuck for up to max_ms = 17 minutes).
            if not self._candidate_present:
                total_dur_ms = (time.time() - start) * 1000
                logger.info(
                    "⏱ TIMELINE [room %s] VAD: candidate left — aborting record "
                    "after %.0fms",
                    self.room_id, total_dur_ms,
                )
                break

            if not self._rms_history:
                continue

            rms = self._rms_history[-1]

            if rms >= silence_threshold:
                speech_frames += 1
                silence_frames = 0
                if not _vad_speech_started and speech_frames == 1:
                    _vad_speech_started = True
                    delay_since_rec_start = (time.time() - start) * 1000
                    logger.info(
                        "⏱ TIMELINE [room %s] VAD: CANDIDATE SPEECH DETECTED "
                        "(%.0fms after recording started) | rms=%.4f",
                        self.room_id, delay_since_rec_start, rms,
                    )
            else:
                if speech_frames >= min_speech_frames:
                    silence_frames += 1
                    if silence_frames >= silence_frames_needed:
                        speech_dur_ms = speech_frames * frame_ms
                        silence_dur_ms = silence_frames * frame_ms
                        total_dur_ms = (time.time() - start) * 1000
                        logger.info(
                            "⏱ TIMELINE [room %s] VAD: CANDIDATE STOPPED SPEAKING "
                            "| speech=%dms silence=%dms total=%.0fms — stopping record",
                            self.room_id, speech_dur_ms, silence_dur_ms, total_dur_ms,
                        )
                        break

        self._recording = False
        pcm_bytes = b"".join(self._incoming_buffer)
        elapsed = time.time() - start

        logger.info(
            "⏱ TIMELINE [room %s] RECORDING COMPLETE: %.1fs total | %d bytes "
            "| speech=%dms silence=%dms",
            self.room_id, elapsed, len(pcm_bytes),
            speech_frames * frame_ms, silence_frames * frame_ms,
        )
        return pcm_bytes

    # ═══════════════════════════════════════════════════════════
    # DEEPGRAM STT — replaces JS-side Deepgram WebSocket
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    def _ws_open(ws) -> bool:
        """True if a websocket is connected. Handles both the new asyncio API
        (websockets>=13: .state enum) and the legacy API (.closed bool)."""
        if ws is None:
            return False
        state = getattr(ws, "state", None)
        if state is not None:                       # new asyncio client
            return getattr(state, "name", "") == "OPEN"
        closed = getattr(ws, "closed", None)
        if closed is not None:                      # legacy client
            return not closed
        return True

    def _build_dg_url(self) -> str:
        """Deepgram streaming URL. Shared by record + prewarm so the pre-opened
        socket has identical params to the one record expects."""
        return (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-2&language=en&smart_format=true&punctuate=true"
            "&encoding=linear16&sample_rate=48000&channels=1"
            "&interim_results=true&endpointing=500"   # 500ms after speech → is_final
            "&utterance_end_ms=2500"                  # 2.5s pause = true end of turn
            "&vad_events=true"
            "&keywords=lakh:5,lakhs:5,lpa:5,LPA:5,crore:3,CTC:5"
            ",python:3,javascript:3,react:3,angular:3,nodejs:3"
            ",langchain:4,langgraph:4,pytorch:4,tensorflow:3"
            ",fastapi:3,flask:3,django:3,postgres:3,mongodb:3"
            ",docker:3,kubernetes:3,aws:3,azure:3,gcp:3"
        )

    async def prewarm_dg(self) -> None:
        """Open the Deepgram WS WHILE the bot is still speaking, so the ~800ms
        handshake is paid during playback instead of after it. record_and_
        transcribe_realtime() adopts this socket, making the candidate's turn
        start instantly. Kept alive with KeepAlive (DG idle-closes ~10s) since
        bot utterances can run 10-20s. Best-effort: any failure just means
        record opens a fresh socket as before."""
        if self._prewarming or self._prewarm_ws is not None:
            return  # already warm / warming (e.g. ack + question back-to-back)
        from api_key_pool import acquire_key_async, release_key
        # Reserve a Deepgram key slot for this socket; the lease rides with the
        # socket and is released when record_and_transcribe_realtime closes it
        # (or here/in disconnect if the prewarmed socket is never adopted).
        api_key, dg_lease = await acquire_key_async("deepgram")
        if not api_key:
            release_key("deepgram", dg_lease)
            return
        self._prewarming = True
        try:
            import websockets
            t = time.time()
            ws = await websockets.connect(
                self._build_dg_url(),
                additional_headers={"Authorization": f"Token {api_key}"},
                open_timeout=10,
            )
            self._prewarm_ws = ws
            self._prewarm_lease = dg_lease
            self._prewarm_keepalive_task = asyncio.create_task(self._dg_keepalive(ws))
            logger.info(
                "⏱ TIMELINE [room %s] DG WS: PRE-WARMED during bot speech (rtt=%.0fms) "
                "— handshake now off the critical path",
                self.room_id, (time.time() - t) * 1000,
            )
        except Exception as e:
            logger.warning(
                "⚠️ [room %s] DG prewarm failed (%s) — record will open a fresh socket",
                self.room_id, e,
            )
            self._prewarm_ws = None
            release_key("deepgram", dg_lease)  # release the slot we reserved
        finally:
            self._prewarming = False

    async def _dg_keepalive(self, ws) -> None:
        """Send Deepgram KeepAlive every 5s so a pre-warmed but not-yet-used
        socket doesn't idle-close while the bot is still talking."""
        try:
            while True:
                await asyncio.sleep(5)
                await ws.send(json.dumps({"type": "KeepAlive"}))
        except asyncio.CancelledError:
            pass
        except Exception:
            # Socket died while idle — drop it so record opens a fresh one.
            self._prewarm_ws = None

    async def record_and_transcribe_realtime(
        self,
        silence_ms: int = 3000,
        max_ms: int = 120_000,
        silence_threshold: float = 0.015,
        min_speech_ms: int = 500,
        early_silence_ms: int = 1000,          # DG is_final + this much silence → early_callback
        early_callback=None,                    # callable(partial_transcript: str) fired at 1s silence
    ) -> str:
        """
        Record candidate audio AND stream to Deepgram simultaneously.

        This mirrors the stt-tts-streaming JS approach exactly:
          JS ScriptProcessor → PCM16 chunks → Deepgram WS (real-time)
        Here:
          LiveKit AudioFrame  → PCM16 chunks → Deepgram WS (real-time)

        Deepgram processes audio as it arrives, so the transcript is
        ready ~instantly when VAD detects silence — no post-recording wait.
        """
        import websockets

        from api_key_pool import acquire_key_async, release_key
        # DG key is reserved lazily: inherited from the prewarmed socket if one is
        # ready, else acquired fresh just before connecting. Released in finally.
        _dg_active_lease = None

        # 48kHz mono PCM16 from LiveKit AudioStream.
        #
        # Key params (learned from Echo-1 production):
        #   interim_results=true  — REQUIRED for endpointing to fire reliably
        #   endpointing=300       — fire is_final 300ms after speech ends
        #   utterance_end_ms=1000 — SAFETY NET: if endpointing misses it,
        #                           DG forces a final transcript at 1s
        #                           (handles short/quiet answers)
        #   vad_events=true       — enables SpeechStarted / SpeechEnded events
        #                           from DG server-side VAD
        #   keywords              — boost domain-specific terms (salary, tech stack)
        dg_url = self._build_dg_url()

        transcript_parts = []
        _early_callback_fired = False
        t0 = time.time()
        _dg_open_t = None  # ⏱ when DG WS actually opened
        _first_is_final_t = None  # ⏱ when first is_final arrived
        _speech_start_t = None  # ⏱ when DG SpeechStarted fired

        # ── Start buffering candidate audio BEFORE the DG handshake ──────────
        # The DG WS open costs ~800ms (see "DG WS: OPENED rtt=" logs). If we
        # only begin capturing once it's open, the first ~800ms of the
        # candidate's answer is dropped on the floor — the "bot ignores me for
        # 1-2s right after it finishes speaking" bug (worst on the greeting,
        # which is a cold connection). So create the frame queue NOW, before
        # connecting: _process_incoming_audio starts filling it immediately and
        # the sender flushes everything buffered the instant the socket opens.
        #
        # LOCAL ref so _send_frames always targets THIS queue even after
        # self._realtime_dg_queue is cleared in the main body (the old
        # 'NoneType has no attribute get' race).
        _dg_q = asyncio.Queue(maxsize=2000)   # ~40s @ 20ms frames
        self._realtime_dg_queue = _dg_q

        logger.info(
            "⏱ TIMELINE [room %s] DG WS: OPENING connection... (candidate audio already buffering)",
            self.room_id,
        )
        try:
            t_dg_open = time.time()
            # Adopt the socket pre-warmed during bot speech if one is ready;
            # otherwise open a fresh one. Adoption = ~0ms handshake on the turn.
            prewarmed = self._prewarm_ws
            self._prewarm_ws = None
            if self._prewarm_keepalive_task is not None:
                self._prewarm_keepalive_task.cancel()
                self._prewarm_keepalive_task = None

            _adopted = self._ws_open(prewarmed)
            if not _adopted and prewarmed is not None:
                # Stale prewarmed socket — close it AND free its key slot.
                try:
                    await prewarmed.close()
                except Exception:
                    pass
                release_key("deepgram", self._prewarm_lease)
                self._prewarm_lease = None

            if _adopted:
                dg_ws = prewarmed
                # Inherit the key slot reserved by prewarm_dg.
                _dg_active_lease = self._prewarm_lease
                self._prewarm_lease = None
            else:
                _dg_key, _dg_active_lease = await acquire_key_async("deepgram")
                dg_ws = await websockets.connect(
                    dg_url,
                    additional_headers={"Authorization": f"Token {_dg_key}"},
                    open_timeout=10,
                )

            try:
                _dg_open_t = time.time()
                dg_open_ms = (_dg_open_t - t_dg_open) * 1000
                _buffered_frames = _dg_q.qsize()
                logger.info(
                    "⏱ TIMELINE [room %s] DG WS: %s (rtt=%.0fms) — flushing %d buffered frames "
                    "(%.0fms of candidate audio preserved), now streaming live",
                    self.room_id, "ADOPTED pre-warmed" if _adopted else "OPENED",
                    dg_open_ms, _buffered_frames, _buffered_frames * 20.0,
                )

                # ── Sender task: drain queue → DG WS ──
                async def _send_frames():
                    while True:
                        chunk = await _dg_q.get()   # local ref — safe from None race
                        if chunk is None:  # sentinel — candidate disconnected OR VAD stopped
                            # Grace window: candidate may reconnect within seconds.
                            # If we break immediately, audio frames from the new
                            # track subscription pile up unsent (Bug #2).
                            try:
                                chunk = await asyncio.wait_for(_dg_q.get(), timeout=10.0)
                                if chunk is None:
                                    break  # second sentinel or true stop — exit
                                # Candidate reconnected and audio is flowing again
                                logger.info(
                                    "🔄 [room %s] _send_frames: candidate reconnected — "
                                    "resuming DG stream after disconnect grace",
                                    self.room_id,
                                )
                                await dg_ws.send(chunk)
                                continue
                            except asyncio.TimeoutError:
                                break  # no reconnect within 10s grace period
                        try:
                            await dg_ws.send(chunk)
                        except Exception:
                            break

                # ── Receiver task: collect is_final transcripts ──
                _utterance_end_received = asyncio.Event()

                async def _recv_transcripts():
                    try:
                        async for message in dg_ws:
                            try:
                                data = json.loads(message)
                                if not data:
                                    continue

                                msg_type = data.get("type")

                                # ── UtteranceEnd: DG forces flush after utterance_end_ms ──
                                # This fires when DG detects speech has definitively ended.
                                # Break immediately — no need to wait flush_wait seconds.
                                if msg_type == "UtteranceEnd":
                                    elapsed_ms = (time.time() - t0) * 1000
                                    logger.info(
                                        "⏱ TIMELINE [room %s] DG: UTTERANCE_END received "
                                        "(%.0fms since recording start) — instant flush",
                                        self.room_id, elapsed_ms,
                                    )
                                    _utterance_end_received.set()
                                    break

                                # ── SpeechStarted: DG server-side VAD detects speech ──
                                if msg_type == "SpeechStarted":
                                    logger.debug("🎙️ [room %s] DG SpeechStarted", self.room_id)
                                    continue

                                # ── is_final: committed transcript segment ──
                                if msg_type == "Results" and data.get("is_final"):
                                    text = (
                                        data.get("channel", {})
                                        .get("alternatives", [{}])[0]
                                        .get("transcript", "")
                                    )
                                    if text.strip():
                                        transcript_parts.append(text)
                                        logger.debug(
                                            "📝 [room %s] DG is_final: '%s'",
                                            self.room_id, text[:80],
                                        )

                            except Exception:
                                pass
                    except Exception as e:
                        # WebSocket died (keepalive timeout, network error, etc.)
                        # Signal UtteranceEnd so the VAD loop can exit cleanly
                        # instead of hanging until max_ms timeout.
                        logger.warning(
                            "⏱ TIMELINE [room %s] DG WS: _recv_transcripts connection "
                            "died: %s — signalling UtteranceEnd for clean exit",
                            self.room_id, e,
                        )
                        _utterance_end_received.set()

                send_task = asyncio.create_task(_send_frames())
                recv_task = asyncio.create_task(_recv_transcripts())

                # ── In-flight keepalive: prevent DG idle-close during silence ──
                # The pre-warm keepalive is cancelled at adoption time, and
                # _send_frames only sends when audio is in the queue. During
                # extended candidate silence, DG receives nothing and times out.
                # This task sends KeepAlive every 5s to prevent that.
                async def _recording_keepalive():
                    try:
                        while True:
                            await asyncio.sleep(5)
                            try:
                                await dg_ws.send(json.dumps({"type": "KeepAlive"}))
                            except Exception:
                                break
                    except asyncio.CancelledError:
                        pass
                keepalive_task = asyncio.create_task(_recording_keepalive())

                # ── VAD recording loop (same as record_audio) ──
                wait_start = time.time()
                while not self.has_remote_audio:
                    if time.time() - wait_start > 60:  # increased from 30s
                        logger.error("[room %s] No candidate audio track after 60s", self.room_id)
                        self._realtime_dg_queue = None
                        send_task.cancel()
                        recv_task.cancel()
                        keepalive_task.cancel()
                        return ""
                    elapsed = time.time() - wait_start
                    if int(elapsed) == 10:
                        logger.warning("⚠️ [room %s] 10s elapsed, no candidate audio — mic may be blocked", self.room_id)
                    await asyncio.sleep(0.5)

                self._recording = True
                self._incoming_buffer.clear()

                frame_ms = 20
                silence_frames_needed = silence_ms // frame_ms
                min_speech_frames = min_speech_ms // frame_ms
                max_frames = max_ms // frame_ms
                silence_frames = speech_frames = total_frames = 0

                logger.info(
                    "🎤 [room %s] RealTime recording+streaming started (silence=%dms, max=%dms)",
                    self.room_id, silence_ms, max_ms,
                )

                while total_frames < max_frames:
                    await asyncio.sleep(frame_ms / 1000.0)
                    total_frames += 1

                    # ── DG UtteranceEnd = authoritative stop signal ──────────────
                    # When Deepgram's server-side VAD fires UtteranceEnd, it means
                    # speech is definitively done. Stop the VAD loop immediately
                    # instead of waiting for local RMS silence, which can hang for
                    # 30-60s if background noise keeps RMS above the threshold
                    # (mic bleed, fan, mute-button lag, etc.)
                    if _utterance_end_received.is_set():
                        logger.info(
                            "⏱ TIMELINE [room %s] VAD loop exiting — DG UtteranceEnd received "
                            "(speech=%dms, saved %.0fms vs local VAD)",
                            self.room_id, speech_frames * frame_ms,
                            max(0, (max_frames - total_frames) * frame_ms),
                        )
                        break
                    # ─────────────────────────────────────────────────────────────

                    # ── Candidate-left watchdog ───────────────────────────────────
                    # Candidate disconnected from LiveKit → no more audio frames.
                    # _rms_history stops updating; without this check the loop
                    # reads stale RMS forever and never exits.
                    # Break immediately → return "" → handle_no_answer_block fires
                    # → _get_presence() checks Socket.IO → absent/mute flow runs.
                    if not self._candidate_present:
                        logger.info(
                            "⏱ TIMELINE [room %s] DG VAD: candidate left — "
                            "aborting stream after %.0fms",
                            self.room_id, (time.time() - t0) * 1000,
                        )
                        break
                    # ─────────────────────────────────────────────────────────────

                    if not self._rms_history:
                        continue
                    rms = self._rms_history[-1]
                    if rms >= silence_threshold:
                        speech_frames += 1
                        silence_frames = 0
                    else:
                        # ── No-initial-speech timeout ────────────────────────────
                        # If candidate hasn't spoken at all after 10s, break early.
                        # Without this, ambient mic noise keeps the loop running
                        # for up to max_ms (17 min) with zero speech detected.
                        # Returning "" triggers handle_no_answer_block which prompts
                        # "are you still there?" or repeats the question.
                        # First attempt = 10s; subsequent no-answer retries use 30s.
                        if speech_frames == 0:
                            no_speech_timeout_ms = 10_000
                            if total_frames * frame_ms >= no_speech_timeout_ms:
                                logger.info(
                                    "⏱ TIMELINE [room %s] VAD: no speech detected after %dms "
                                    "— returning empty (will trigger no-answer flow)",
                                    self.room_id, total_frames * frame_ms,
                                )
                                break

                        if speech_frames >= min_speech_frames:
                            silence_frames += 1

                            # ── Early callback at 1s silence + DG is_final ──
                            # DG endpointing=300 fires is_final ~300ms after speech.
                            # When VAD sees 1s silence AND we have a transcript,
                            # fire callback immediately so GPT/Cartesia can start.
                            if (
                                not _early_callback_fired
                                and early_callback is not None
                                and transcript_parts
                                and silence_frames * frame_ms >= early_silence_ms
                            ):
                                _early_callback_fired = True
                                partial = " ".join(transcript_parts)
                                early_cb_ms = (time.time() - t0) * 1000
                                logger.info(
                                    "⏱ TIMELINE [room %s] EARLY CALLBACK FIRED "
                                    "(%.0fms since recording start | %.0fms silence) "
                                    "| %d words | text='%s' — GPT+Cartesia starting",
                                    self.room_id, early_cb_ms,
                                    silence_frames * frame_ms,
                                    len(partial.split()), partial[:80],
                                )
                                asyncio.create_task(
                                    early_callback(partial)
                                    if asyncio.iscoroutinefunction(early_callback)
                                    else asyncio.get_running_loop().run_in_executor(
                                        None, early_callback, partial
                                    )
                                )

                            if silence_frames >= silence_frames_needed:
                                logger.info(
                                    "🎤 [room %s] VAD silence after %dms speech — stopping stream",
                                    self.room_id, speech_frames * frame_ms,
                                )

                                break

                self._recording = False

                # Stop the in-flight keepalive — no longer needed once recording ends
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except (asyncio.CancelledError, Exception):
                    pass

                # Signal sender to stop and drain remaining frames
                await _dg_q.put(None)          # sentinel into the LOCAL queue ref
                await send_task                 # wait for sender to finish before clearing
                self._realtime_dg_queue = None  # safe to clear now — sender is done

                # Tell Deepgram we're done — it will flush remaining audio
                try:
                    await dg_ws.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    pass

                # KEY TIMING:
                # • endpointing=300      → is_final fires 300ms after speech ends
                # • utterance_end_ms=1000 → UtteranceEnd fires ≤1s after speech,
                #                          breaking recv_task by itself
                #
                # Fast path: UtteranceEnd already received → recv_task exited on
                # its own → no waiting needed.
                #
                # Slow path: VAD fired before DG sent UtteranceEnd (short/quiet speech)
                # → wait up to flush_wait. Capped at 1.2s because utterance_end_ms=1000
                # guarantees resolution within 1s.

                if recv_task.done() or _utterance_end_received.is_set():
                    flush_wait = 0.0
                    logger.info(
                        "📝 [room %s] UtteranceEnd received — instant flush (parts=%d)",
                        self.room_id, len(transcript_parts),
                    )
                else:
                    flush_wait = 1.2 if transcript_parts else 2.0
                    logger.info(
                        "📝 [room %s] Waiting %.1fs for DG flush (parts=%d)",
                        self.room_id, flush_wait, len(transcript_parts),
                    )

                if not recv_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(recv_task), timeout=flush_wait)
                    except asyncio.TimeoutError:
                        pass
                    finally:
                        if not recv_task.done():
                            recv_task.cancel()
                            try:
                                await recv_task
                            except (asyncio.CancelledError, Exception):
                                pass

                logger.info(
                    "📝 [room %s] DG flush done — %d transcript parts collected",
                    self.room_id, len(transcript_parts),
                )
            finally:
                # Close the socket (adopted or freshly-opened) — replaces the
                # auto-close the old `async with websockets.connect()` provided.
                try:
                    await dg_ws.close()
                except Exception:
                    pass
                release_key("deepgram", _dg_active_lease)
                _dg_active_lease = None  # prevent double-release in outer except

        except Exception as e:
            logger.warning(
                "⚠️ [room %s] RealTime DG WS failed (%s) — falling back to batch",
                self.room_id, e,
            )
            self._recording = False
            self._realtime_dg_queue = None
            # Release the slot if connect failed before the inner try (no-op if
            # already released by the inner finally).
            release_key("deepgram", _dg_active_lease)
            _dg_active_lease = None

            # Re-use the audio already captured in _incoming_buffer (VAD already fired).
            # DO NOT call record_audio() again — the candidate already spoke and
            # re-recording would just wait silently.
            saved_pcm = b"".join(self._incoming_buffer)
            if saved_pcm:
                logger.info(
                    "🔄 [room %s] Batch fallback: transcribing %d bytes already captured",
                    self.room_id, len(saved_pcm),
                )
                return await self.stream_to_deepgram(saved_pcm)
            else:
                # No audio captured yet — record fresh (edge case: failure before recording)
                logger.info("🔄 [room %s] Batch fallback: no prior audio, re-recording", self.room_id)
                pcm = await self.record_audio(
                    silence_ms=silence_ms, max_ms=max_ms,
                    silence_threshold=silence_threshold,
                    min_speech_ms=min_speech_ms,
                )
                return await self.stream_to_deepgram(pcm)

        transcript = " ".join(transcript_parts)
        elapsed = time.time() - t0
        word_count = len(transcript.split()) if transcript.strip() else 0
        first_is_final_delay = (
            (_first_is_final_t - t0) * 1000 if _first_is_final_t else None
        )
        logger.info(
            "⏱ TIMELINE [room %s] TRANSCRIPT READY: %.0fms total pipeline "
            "| %d words | %d chars | first_is_final=%.0fms | early_cb=%s "
            "| text='%s'",
            self.room_id, elapsed * 1000, word_count, len(transcript),
            first_is_final_delay if first_is_final_delay is not None else -1,
            "yes" if _early_callback_fired else "no",
            transcript[:120] + "..." if len(transcript) > 120 else transcript,
        )
        return transcript


    async def stream_to_deepgram(self, pcm_bytes: bytes) -> str:
        """
        Transcribe PCM16 audio via Deepgram WebSocket streaming.

        Audio is streamed in real-time chunks so Deepgram processes it in
        parallel — transcript is ready almost immediately when VAD stops,
        vs batch which adds 3-9s of extra latency after recording ends.

        Audio: 48kHz mono PCM16 (LiveKit native rate).
        Falls back to batch HTTP if WebSocket fails.
        """
        import websockets

        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY environment variable not set")

        if not pcm_bytes or len(pcm_bytes) < 640:
            logger.warning("[room %s] Skipping Deepgram — too little audio", self.room_id)
            return ""

        t0 = time.time()

        # Must specify encoding + sample_rate for raw PCM.
        # LiveKit records at 48kHz mono PCM16.
        # Do NOT add utterance_end_ms — it causes HTTP 400 without interim_results.
        dg_url = (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-2&language=en&smart_format=true&punctuate=true"
            "&encoding=linear16&sample_rate=48000&channels=1"
        )

        transcript_parts = []

        try:
            async with websockets.connect(
                dg_url,
                additional_headers={"Authorization": f"Token {api_key}"},
                open_timeout=10,
            ) as dg_ws:

                # Stream audio in chunks — Deepgram processes in real-time
                chunk_size = 3840  # 20ms at 48kHz PCM16 mono
                for i in range(0, len(pcm_bytes), chunk_size):
                    await dg_ws.send(pcm_bytes[i : i + chunk_size])

                # Tell Deepgram we're done — it will flush and close
                await dg_ws.send(json.dumps({"type": "CloseStream"}))

                # Collect ALL is_final results until WebSocket closes naturally.
                # CRITICAL: Do NOT break on "Metadata" — it arrives at SESSION
                # START (not end). The old code broke here causing 2-3 word truncation.
                async for message in dg_ws:
                    try:
                        data = json.loads(message)
                        if data.get("type") == "Results":
                            alt = (
                                data.get("channel", {})
                                .get("alternatives", [{}])[0]
                            )
                            text = alt.get("transcript", "")
                            if data.get("is_final") and text.strip():
                                transcript_parts.append(text)
                    except (json.JSONDecodeError, Exception):
                        pass

        except Exception as e:
            logger.warning(
                "⚠️ [room %s] Deepgram WS failed (%s), falling back to batch",
                self.room_id, e,
            )
            return await self._deepgram_batch_fallback(pcm_bytes)

        transcript = " ".join(transcript_parts)
        elapsed = time.time() - t0
        logger.info(
            "📝 [room %s] Deepgram transcript: %.0fms | %d chars | '%s'",
            self.room_id, elapsed * 1000, len(transcript),
            transcript[:200] + "..." if len(transcript) > 200 else transcript,
        )
        return transcript

    async def _deepgram_batch_fallback(self, pcm_bytes: bytes) -> str:
        """Batch HTTP fallback when WebSocket streaming fails."""
        import httpx

        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        url = "https://api.deepgram.com/v1/listen"
        params = {
            "model": "nova-2", "language": "en",
            "smart_format": "true", "punctuate": "true",
            "encoding": "linear16", "sample_rate": "48000", "channels": "1",
        }
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    url, params=params,
                    headers={"Authorization": f"Token {api_key}", "Content-Type": "audio/raw"},
                    content=pcm_bytes,
                )
                if resp.status_code != 200:
                    logger.error("❌ [room %s] Deepgram batch %s: %s", self.room_id, resp.status_code, resp.text[:200])
                    return ""
                data = resp.json()
                transcript = (
                    data.get("results", {}).get("channels", [{}])[0]
                    .get("alternatives", [{}])[0].get("transcript", "")
                )
                elapsed = time.time() - t0
                logger.info("📝 [room %s] Deepgram batch transcript: %.0fms | '%s'",
                            self.room_id, elapsed * 1000,
                            transcript[:200] + "..." if len(transcript) > 200 else transcript)
                return transcript
        except Exception as e:
            logger.error("❌ [room %s] Deepgram batch failed: %s", self.room_id, e)
            return ""


    # ═══════════════════════════════════════════════════════════
    # AUDIO PLAYBACK — replaces play_audio_in_webrtc()
    # ═══════════════════════════════════════════════════════════

    async def play_audio(
        self,
        audio_bytes: bytes,
        format: str = "pcm",
        sample_rate: int = 24000,
    ) -> None:
        """
        Publish audio bytes as a LiveKit track to the candidate.

        audio_bytes: Raw PCM16 bytes (preferred) or MP3 (will be decoded)
        format:      'pcm' or 'mp3'
        sample_rate: Sample rate of the PCM data (default 24kHz for OpenAI TTS)

        The audio is published in 20ms frames to match WebRTC ptime.
        LiveKit handles Opus encoding and transport — no base64, no injection.

        Replaces: play_audio_in_webrtc()      (L1759-1898)
                  ai_speak()                  (L8989-9068)
                  ai_speak_with_audio()       (L9071+)
                  ai_speak_streamed_text()    (L8753+)
                  ai_speak_streaming()        (L8541+)
        """
        from livekit import rtc

        if format == "mp3":
            audio_bytes, sample_rate = self._decode_mp3_to_pcm(audio_bytes)

        self._stop_playback.clear()

        # 20ms frame at the given sample rate, PCM16 mono
        # e.g. 24000 Hz * 2 bytes * 1 channel * 0.02s = 960 bytes per frame
        frame_bytes = int(sample_rate * 2 * self.PLAYBACK_CHANNELS * 0.02)
        total_frames = len(audio_bytes) // frame_bytes
        duration_s = len(audio_bytes) / (sample_rate * 2 * self.PLAYBACK_CHANNELS)

        logger.debug(
            "🔊 [room %s] Playback start: %.1fs | %d frames | sr=%d",
            self.room_id, duration_s, total_frames, sample_rate,
        )

        for i in range(total_frames):
            if self._stop_playback.is_set():
                logger.debug(
                    "🔊 [room %s] Playback stopped early at frame %d/%d",
                    self.room_id, i, total_frames,
                )
                break

            chunk = audio_bytes[i * frame_bytes : (i + 1) * frame_bytes]
            samples_array = np.frombuffer(chunk, dtype=np.int16)

            frame = rtc.AudioFrame(
                data=samples_array.tobytes(),
                sample_rate=sample_rate,
                num_channels=self.PLAYBACK_CHANNELS,
                samples_per_channel=len(samples_array),
            )

            await self._audio_source.capture_frame(frame)
            await asyncio.sleep(0.018)  # slightly under 20ms to prevent underrun

        logger.info(
            "🔊 [room %s] Playback complete: %.1fs",
            self.room_id, duration_s,
        )

    # ═══════════════════════════════════════════════════════════
    # CONTINUOUS AUDIO STREAM (queue-based) — eliminates inter-chunk gaps
    # ═══════════════════════════════════════════════════════════

    async def start_stream_to_livekit(self, sample_rate: int = 24000) -> asyncio.Queue:
        """
        Start a background playback task that drains PCM chunks from a queue
        at a fixed 20ms clock rate.

        WHY: When Cartesia sends audio chunks one-by-one over WebSocket,
        calling play_audio_chunk() per chunk blocks the receiver while playing.
        This creates silence gaps between chunks — causing the "breaking" voice.

        FIX: Push all incoming chunks into a shared queue. A single background
        task drains the queue at exactly 20ms/frame with no blocking receiver.
        Audio plays continuously regardless of chunk timing.

        Usage:
            q = await bot.start_stream_to_livekit()
            q.put_nowait(pcm_chunk)  # from Cartesia receiver
            await bot.stop_stream_to_livekit()   # when done
        """
        from livekit import rtc

        frame_bytes = int(sample_rate * 2 * self.PLAYBACK_CHANNELS * 0.02)  # 960B at 24kHz
        self._stream_buf: asyncio.Queue = asyncio.Queue()
        self._stream_sample_rate = sample_rate
        self._stop_playback.clear()

        async def _drain_loop():
            buf = bytearray()
            while True:
                # ─ 1. Drain all available chunks into local buffer ─
                while True:
                    try:
                        chunk = self._stream_buf.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if chunk is None:          # sentinel: flush + exit
                        while len(buf) >= frame_bytes and not self._stop_playback.is_set():
                            frame_data = bytes(buf[:frame_bytes])
                            del buf[:frame_bytes]
                            arr = np.frombuffer(frame_data, dtype=np.int16)
                            # Write to local recorder (bot side)
                            if self._local_recorder:
                                self._local_recorder.write_bot(frame_data)
                            await self._audio_source.capture_frame(
                                rtc.AudioFrame(
                                    data=arr.tobytes(),
                                    sample_rate=sample_rate,
                                    num_channels=self.PLAYBACK_CHANNELS,
                                    samples_per_channel=len(arr),
                                )
                            )
                            await asyncio.sleep(0.018)
                        return
                    buf.extend(chunk)

                # ─ 2. Push ONE 20ms frame if buffer has enough data ─
                if self._stop_playback.is_set():
                    return

                if len(buf) >= frame_bytes:
                    frame_data = bytes(buf[:frame_bytes])
                    del buf[:frame_bytes]
                    arr = np.frombuffer(frame_data, dtype=np.int16)
                    # Write to local recorder (bot side)
                    if self._local_recorder:
                        self._local_recorder.write_bot(frame_data)
                    await self._audio_source.capture_frame(
                        rtc.AudioFrame(
                            data=arr.tobytes(),
                            sample_rate=sample_rate,
                            num_channels=self.PLAYBACK_CHANNELS,
                            samples_per_channel=len(arr),
                        )
                    )
                    await asyncio.sleep(0.018)  # real-time pacing
                else:
                    # Not enough data yet — tiny sleep to avoid spin-wait
                    await asyncio.sleep(0.002)

        self._stream_task = asyncio.create_task(_drain_loop())
        return self._stream_buf

    async def stop_stream_to_livekit(self) -> None:
        """Signal end-of-stream and wait for the drain task to finish."""
        buf = getattr(self, "_stream_buf", None)
        if buf is not None:
            await buf.put(None)   # sentinel — drains remainder then exits
        task = getattr(self, "_stream_task", None)
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=30)
            except asyncio.TimeoutError:
                task.cancel()
        self._stream_buf = None
        self._stream_task = None

    async def play_audio_chunk(self, pcm_chunk: bytes, sample_rate: int = 24000) -> None:
        """
        Push a raw PCM16 chunk into the active stream queue.
        Non-blocking — the background drain task handles timing.
        Falls back to direct frame publish if no stream is active.
        """
        if not pcm_chunk:
            return

        stream_buf = getattr(self, "_stream_buf", None)
        if stream_buf is not None:
            # Happy path: just enqueue, drain loop handles timing
            stream_buf.put_nowait(pcm_chunk)
            return

        # Fallback: frame-by-frame direct publish (no active stream)
        from livekit import rtc
        frame_bytes = int(sample_rate * 2 * self.PLAYBACK_CHANNELS * 0.02)
        remainder = len(pcm_chunk) % frame_bytes
        if remainder:
            pcm_chunk = pcm_chunk + b'\x00' * (frame_bytes - remainder)
        for i in range(0, len(pcm_chunk), frame_bytes):
            if self._stop_playback.is_set():
                break
            chunk = pcm_chunk[i: i + frame_bytes]
            arr = np.frombuffer(chunk, dtype=np.int16)
            await self._audio_source.capture_frame(
                rtc.AudioFrame(
                    data=arr.tobytes(), sample_rate=sample_rate,
                    num_channels=self.PLAYBACK_CHANNELS, samples_per_channel=len(arr),
                )
            )
            await asyncio.sleep(0.018)

    async def play_cached_clip(self, pcm: bytes, sample_rate: int = 24000, clip_id: str = "") -> bool:
        """Play a pre-rendered PCM clip (cached ack / question) to completion.

        Task 2: the clip comes from memory, so NO Cartesia slot is used — this is the
        whole point of cached audio. Mirrors play_filler's direct capture_frame loop
        (captured in the recording, barge-in aware via _stop_playback). Returns True if
        it played; callers fall back to live TTS on False.
        """
        if not pcm or self._audio_source is None:
            return False
        from livekit import rtc
        frame_bytes = int(sample_rate * 2 * self.PLAYBACK_CHANNELS * 0.02)
        remainder = len(pcm) % frame_bytes
        if remainder:
            pcm = pcm + b"\x00" * (frame_bytes - remainder)
        total_frames = len(pcm) // frame_bytes
        self._stop_playback.clear()
        logger.info("🗣️ [room %s] cached clip '%s' (%.1fs) [no Cartesia slot]",
                    self.room_id, clip_id, total_frames * 0.02)
        for i in range(total_frames):
            if self._stop_playback.is_set():
                break
            chunk = pcm[i * frame_bytes: (i + 1) * frame_bytes]
            arr = np.frombuffer(chunk, dtype=np.int16)
            if self._local_recorder:
                self._local_recorder.write_bot(chunk)
            await self._audio_source.capture_frame(rtc.AudioFrame(
                data=arr.tobytes(),
                sample_rate=sample_rate,
                num_channels=self.PLAYBACK_CHANNELS,
                samples_per_channel=len(arr),
            ))
            await asyncio.sleep(0.018)
        return True

    # ═══════════════════════════════════════════════════════════
    # FILLER AUDIO — plays while waiting for a Cartesia key slot
    # ═══════════════════════════════════════════════════════════

    def set_filler_phase(self, question_no, total=None) -> None:
        """Set which filler bucket to use next, based on where we are in the
        interview. Called by the flow before speaking.

        The Q1 greeting is the point where a mass simultaneous-start pileup hits
        the Cartesia pool (30 interviews all needing "Hi {name}…" at once), so it
        uses the pre-interview "waiting" holding copy; later questions use the
        normal opening / mid / closing lines.
        """
        from filler_audio import FillerBank
        if question_no is not None and question_no <= 1:
            self._filler_phase = "waiting"
        else:
            self._filler_phase = FillerBank.phase_for(question_no, total)

    async def play_filler(self, attempt: int = 0) -> bool:
        """Play ONE short pre-rendered filler clip to the candidate.

        Passed to the keypool as `on_wait`, so it runs only while a Cartesia slot is
        unavailable. `attempt` is how many fillers already played this wait: attempt 0
        uses the phase-appropriate line (opening/mid/closing); attempt >= 1 (a longer
        wait / deeper queue) escalates to an explicit "just a moment, bear with me"
        line so the pause reads as intentional.

        Returns True if a clip played, False if the bank is empty (the keypool then
        falls back to a silent poll). Plays via the direct capture_frame path (the real
        stream isn't active yet) and mirrors the drain loop's write_bot() so the filler
        is captured in the recording.
        """
        from filler_audio import get_filler_bank
        from livekit import rtc

        # Pre-interview "waiting" holding stays on the waiting copy for the whole
        # wait (which can be long under a mass start). Normal turn-fillers escalate
        # to the brief "wait" bucket after the first phase-appropriate line.
        is_waiting = self._filler_phase == "waiting"
        bucket = self._filler_phase if (attempt <= 0 or is_waiting) else "wait"
        picked = get_filler_bank().pick(bucket, self._recent_fillers)
        if not picked:
            return False
        pcm, clip_id = picked
        if not pcm or self._audio_source is None:
            return False
        self._recent_fillers.append(clip_id)

        sample_rate = get_filler_bank().sample_rate
        frame_bytes = int(sample_rate * 2 * self.PLAYBACK_CHANNELS * 0.02)
        total_frames = len(pcm) // frame_bytes
        self._stop_playback.clear()
        logger.info("🗣️ [room %s] filler '%s' (%.1fs, bucket=%s, attempt=%d) while waiting for Cartesia slot",
                    self.room_id, clip_id, total_frames * 0.02, bucket, attempt)

        for i in range(total_frames):
            if self._stop_playback.is_set():
                break
            chunk = pcm[i * frame_bytes : (i + 1) * frame_bytes]
            arr = np.frombuffer(chunk, dtype=np.int16)
            if self._local_recorder:
                self._local_recorder.write_bot(chunk)
            await self._audio_source.capture_frame(rtc.AudioFrame(
                data=arr.tobytes(),
                sample_rate=sample_rate,
                num_channels=self.PLAYBACK_CHANNELS,
                samples_per_channel=len(arr),
            ))
            await asyncio.sleep(0.018)

        # Pre-interview holding pattern: space reassurances out (~clip + gap ≈
        # 15-20s cadence) instead of back-to-back. The gap is interruptible so a
        # stop isn't blocked; lower AIRA_FILLER_WAIT_GAP_S for tighter slot reuse,
        # raise it for a less chatty hold. (No gap for normal turn-fillers.)
        if is_waiting:
            gap_s = float(os.environ.get("AIRA_FILLER_WAIT_GAP_S", "12"))
            waited = 0.0
            while waited < gap_s and not self._stop_playback.is_set():
                await asyncio.sleep(0.25)
                waited += 0.25
        return True

    async def _ensure_speak_lease(self) -> str:
        """Acquire (once) and HOLD a Cartesia key slot for this turn's speech, so the
        ack and the question stream under ONE lease and a filler can't intrude between
        them. If a lease is already held, returns its key without re-acquiring. The
        lease is released by release_speak_lease() when the bot starts listening.
        Filler (with escalation) plays here while all slots are busy.
        """
        if self._speak_lease is not None:
            return self._speak_key
        from api_key_pool import acquire_key_async
        self._speak_key, self._speak_lease = await acquire_key_async(
            "cartesia", on_wait=self.play_filler
        )
        return self._speak_key

    def release_speak_lease(self) -> None:
        """Release the held turn-speech lease (safe to call when none is held).
        Called when the bot starts listening, on close, and as a loop backstop."""
        if self._speak_lease is None:
            return
        from api_key_pool import release_key
        lease = self._speak_lease
        self._speak_lease = None
        self._speak_key = None
        try:
            release_key("cartesia", lease)
        except Exception as e:
            logger.warning("[room %s] release_speak_lease failed: %s", self.room_id, e)

    # ═══════════════════════════════════════════════════════════
    # CARTESIA TTS — streaming synthesis → LiveKit
    # ═══════════════════════════════════════════════════════════

    async def speak_cartesia(self, text: str) -> None:
        """
        Synthesise text via Cartesia WS, stream PCM directly to LiveKit.
        Lower latency than OpenAI TTS (TTFA ~200-300ms vs ~1-2s).
        """
        # Open the Deepgram socket in the background WHILE we speak, so the
        # candidate's turn starts with zero handshake latency.
        asyncio.create_task(self.prewarm_dg())
        from cartesia_tts import cartesia_speak_text_streaming
        await cartesia_speak_text_streaming(text, self)

    async def speak_cartesia_tokens(self, token_iter, on_first_audio=None) -> None:
        """
        Stream GPT tokens word-by-word into Cartesia WS → audio → LiveKit.
        Use this for lowest perceived latency: candidate hears audio
        within 300ms of the first GPT token arriving.
        """
        # Pre-open the Deepgram socket during playback (see speak_cartesia).
        asyncio.create_task(self.prewarm_dg())
        from cartesia_tts import cartesia_speak_tokens
        await cartesia_speak_tokens(token_iter, self, on_first_audio=on_first_audio)


    def _decode_mp3_to_pcm(self, mp3_bytes: bytes) -> tuple:
        """
        Decode MP3 → PCM16 at 24kHz mono.
        Returns (pcm_bytes, sample_rate) tuple.
        """
        import io
        try:
            from pydub import AudioSegment
        except ImportError:
            raise ImportError(
                "pydub required for MP3 playback: pip install pydub"
            )

        audio = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
        audio = (
            audio
            .set_frame_rate(self.PLAYBACK_SAMPLE_RATE)
            .set_channels(self.PLAYBACK_CHANNELS)
            .set_sample_width(2)  # 16-bit
        )
        return audio.raw_data, self.PLAYBACK_SAMPLE_RATE

    def stop_playback(self):
        """Signal to stop any in-progress playback (for barge-in)."""
        self._stop_playback.set()

    # ═══════════════════════════════════════════════════════════
    # BARGE-IN — replaces play_ack_with_bargein()
    # ═══════════════════════════════════════════════════════════

    async def play_with_bargein(
        self,
        audio_bytes: bytes,
        format: str = "pcm",
        bargein_rms_threshold: float = 0.03,
        bargein_sustained_ms: int = 300,
    ) -> dict:
        """
        Play audio to candidate while monitoring for interruption.

        Returns:
            {"interrupted": True}  — candidate spoke over the bot
            {"interrupted": False} — playback completed normally

        The barge-in detector runs concurrently with playback, checking
        incoming RMS every 20ms. If sustained RMS above threshold for
        bargein_sustained_ms, playback is stopped immediately.

        Replaces: play_ack_with_bargein() (L8004+)
        """
        if format == "mp3":
            audio_bytes, _ = self._decode_mp3_to_pcm(audio_bytes)
            format = "pcm"

        self._stop_playback.clear()
        interrupted = False

        async def bargein_monitor():
            nonlocal interrupted
            frames_needed = bargein_sustained_ms // 20
            sustained_frames = 0

            while not self._stop_playback.is_set():
                await asyncio.sleep(0.02)
                if (
                    self._rms_history
                    and self._rms_history[-1] > bargein_rms_threshold
                ):
                    sustained_frames += 1
                    if sustained_frames >= frames_needed:
                        logger.info(
                            "🛑 [room %s] Barge-in detected! "
                            "RMS=%.4f > threshold=%.4f for %dms",
                            self.room_id,
                            self._rms_history[-1],
                            bargein_rms_threshold,
                            sustained_frames * 20,
                        )
                        self._stop_playback.set()
                        interrupted = True
                        return
                else:
                    sustained_frames = 0

        # Run playback and barge-in monitor concurrently.
        # IMPORTANT: play_audio sets _stop_playback on natural completion so the
        # monitor exits on its next 20ms tick instead of looping forever.
        async def _play_then_signal():
            await self.play_audio(audio_bytes, format="pcm")
            self._stop_playback.set()   # ← tells bargein_monitor to exit cleanly

        await asyncio.gather(_play_then_signal(), bargein_monitor())

        return {"interrupted": interrupted}

    # ═══════════════════════════════════════════════════════════
    # REAL-TIME STREAMING STT (optional future upgrade)
    # ═══════════════════════════════════════════════════════════

    async def stream_stt_realtime(self):
        """
        Async generator: yields interim and final transcripts in real-time
        while the candidate is speaking.

        Use this for speculative/reflex pipeline where you want to start
        processing the response before the candidate finishes speaking.

        Yields:
            {
                "transcript": str,
                "is_final": bool,
                "speech_final": bool,
            }
        """
        import websockets

        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            raise ValueError("DEEPGRAM_API_KEY not set")

        dg_url = (
            "wss://api.deepgram.com/v1/listen?"
            "model=nova-2&language=en&smart_format=true"
            "&encoding=linear16&sample_rate=16000&channels=1"
            "&interim_results=true&endpointing=300"
        )

        async with websockets.connect(
            dg_url,
            additional_headers={"Authorization": f"Token {api_key}"},
        ) as dg_ws:

            self._recording = True
            self._incoming_buffer.clear()

            # Forward audio chunks to Deepgram as they arrive
            async def send_audio_loop():
                prev_len = 0
                while self._recording:
                    current = list(self._incoming_buffer)
                    new_chunks = current[prev_len:]
                    for chunk in new_chunks:
                        try:
                            await dg_ws.send(chunk)
                        except Exception:
                            return
                    prev_len = len(current)
                    await asyncio.sleep(0.02)

                # Signal end of stream
                try:
                    await dg_ws.send(json.dumps({"type": "CloseStream"}))
                except Exception:
                    pass

            send_task = asyncio.create_task(send_audio_loop())

            try:
                async for message in dg_ws:
                    try:
                        data = json.loads(message)
                        if data.get("type") == "Results":
                            alt = data["channel"]["alternatives"][0]
                            yield {
                                "transcript": alt.get("transcript", ""),
                                "is_final": data.get("is_final", False),
                                "speech_final": data.get("speech_final", False),
                            }
                        elif data.get("type") == "Metadata":
                            break
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass
            finally:
                self._recording = False
                send_task.cancel()
                try:
                    await send_task
                except asyncio.CancelledError:
                    pass
