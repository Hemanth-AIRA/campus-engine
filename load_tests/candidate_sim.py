"""
load_tests/candidate_sim.py
Simulates a candidate joining a LiveKit room and publishing audio.

This is the core "fake candidate" used by the full simulation test.
It connects to the LiveKit SFU using a token fetched from the Connect server,
publishes a synthetic audio track (speech-like PCM), and stays in the room
until told to leave.

The bot running in the same room will:
  - Subscribe to the candidate's audio track
  - Stream it to Deepgram
  - Generate responses via GPT
  - Speak via Cartesia TTS
  - Complete the interview flow end-to-end
"""
import asyncio, logging, time, threading
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass, field
from typing import Optional

import httpx
from livekit import rtc
from livekit.rtc import AudioFrame, AudioSource, LocalAudioTrack, Room

from .audio_gen  import stream_wav_to_source, SAMPLE_RATE, CHANNELS, FRAME_SAMPLES
from .config     import CONNECT_BASE_URL
from .video_gen  import stream_face_video
from .recording  import InterviewRecorder

logger = logging.getLogger("candidate_sim")

SAMPLE_TEXT = "speech-like audio"


@dataclass
class CandidateResult:
    room_id:         str
    candidate_name:  str
    connected:       bool         = False
    track_published: bool         = False
    bot_joined:      bool         = False
    bot_audio_heard: bool         = False
    connected_at_ms: float        = 0.0
    duration_s:      float        = 0.0
    error:           Optional[str] = None
    recording_path:  Optional[str] = None
    timeline:        list          = field(default_factory=list)

    def event(self, msg: str):
        self.timeline.append(f"{time.strftime('%H:%M:%S')} | {msg}")
        logger.info("[%s] %s", self.room_id, msg)


async def fetch_livekit_token(room_id: str, name: str, role: str = "candidate") -> dict:
    """Ask the Connect server for a LiveKit token."""
    identity = f"{role}-{room_id}-sim"
    url = f"{CONNECT_BASE_URL}/api/livekit-token"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json={
            "roomId":   room_id,
            "identity": identity,
            "role":     role,
            "name":     name,
        })
        resp.raise_for_status()
        return resp.json()   # {token, url}


def _candidate_socketio_presence(base_url: str, candidate_join_url: str,
                                  room_id: str, candidate_name: str,
                                  stop_event: threading.Event):
    """
    Register the simulated candidate with the Connect server via Socket.IO.
    The engine's wait_for_participant() polls GET /api/rooms/{room_id} which only
    shows participants who emitted 'join' via Socket.IO — LiveKit SDK alone is invisible.
    Runs in a background daemon thread.
    """
    try:
        import socketio as sio_lib
    except ImportError:
        logger.warning("[%s] python-socketio not installed — candidate won't appear in Connect room", room_id)
        return

    # Extract JWT from candidate_join_url (?t=<token>)
    try:
        params = parse_qs(urlparse(candidate_join_url).query)
        token = params.get("t", [""])[0]
        if not token:
            logger.warning("[%s] No ?t= JWT in candidate_join_url — skipping Socket.IO presence", room_id)
            return
    except Exception as e:
        logger.warning("[%s] Could not parse candidate_join_url: %s", room_id, e)
        return

    sio = sio_lib.Client(reconnection=False, logger=False, engineio_logger=False)

    @sio.event
    def connect():
        logger.info("[%s] 🔌 Candidate Socket.IO connected — emitting 'join'", room_id)
        sio.emit("join", {"roomId": room_id, "name": candidate_name, "consent": True})

    @sio.on("join-ok")
    def on_join_ok(data):
        logger.info("[%s] ✅ Candidate 'join-ok' — visible to engine as role=candidate", room_id)

    @sio.on("join-denied")
    def on_join_denied(data):
        logger.warning("[%s] ❌ Candidate 'join-denied': %s", room_id, data)

    @sio.event
    def connect_error(err):
        logger.warning("[%s] ❌ Candidate Socket.IO error: %s", room_id, err)

    try:
        sio.connect(base_url, auth={"t": token}, transports=["websocket"], wait_timeout=10)
        while not stop_event.is_set():
            stop_event.wait(timeout=1)
    except Exception as e:
        logger.warning("[%s] Candidate Socket.IO exception: %s", room_id, e)
    finally:
        try:
            sio.disconnect()
        except Exception:
            pass


async def run_simulated_candidate(
    room_id:          str,
    candidate_name:   str,
    audio_duration_s:  float = 60.0,
    wait_for_bot_s:    float = 15.0,
    livekit_url:       str   = None,
    livekit_token:     str   = None,
    candidate_join_url: str  = None,   # Connect server join URL (contains ?t=JWT for presence)
    interview_id:       int  = 0,       # Used to pick deterministic face image for video
) -> CandidateResult:
    """
    Join a LiveKit room as a simulated candidate and publish audio.

    Parameters
    ----------
    room_id           : AIRA room ID (e.g. AIRA-20260617-0527-D4FE)
    candidate_name    : Display name used in the room
    audio_duration_s  : How long to stream audio before leaving
    wait_for_bot_s    : Seconds to wait for the bot to appear before aborting
    livekit_url       : Override LiveKit URL (defaults to Connect server token)
    livekit_token     : Override token (defaults to fetching from Connect server)
    """
    result = CandidateResult(room_id=room_id, candidate_name=candidate_name)
    t_start = time.perf_counter()

    # ── 1. Get token ───────────────────────────────────────────────────
    try:
        if livekit_url and livekit_token:
            # Explicit override — use as-is (e.g. from test_05_livekit)
            url, token = livekit_url, livekit_token
        else:
            # Always fetch a real LiveKit token via the Connect server API.
            # NOTE: candidate_join_url contains a Connect WebRTC JWT (iss=aira,
            # aud=webrtc) — NOT a LiveKit token. It is used only for Socket.IO
            # presence below, never as the LiveKit credential.
            result.event("Fetching LiveKit token from Connect server…")
            tok_data = await fetch_livekit_token(room_id, candidate_name)
            url, token = tok_data["url"], tok_data["token"]
            result.event(f"🔑 LiveKit token fetched (identity: candidate-{room_id}-sim)")
    except Exception as e:
        result.error = f"Token fetch failed: {e}"
        result.event(f"❌ {result.error}")
        return result

    # ── 2. Connect to LiveKit room ─────────────────────────────────────
    room = Room()
    bot_joined_event  = asyncio.Event()
    bot_audio_event   = asyncio.Event()

    @room.on("participant_connected")
    def on_participant(participant):
        identity = participant.identity
        result.event(f"👤 Participant joined: {identity}")
        if identity.startswith("bot"):
            result.bot_joined = True
            bot_joined_event.set()

    # ── Recording setup ──
    recorder = InterviewRecorder(room_id=room_id, interview_id=interview_id)
    recorder.start()

    # Bot audio track stored here by the sync callback; consumed by async task below.
    _bot_audio_track_ref: list = []

    @room.on("track_subscribed")
    def on_track(track, publication, participant):
        result.event(f"🎵 Track subscribed: {track.kind} from {participant.identity}")
        if participant.identity.startswith("bot") and track.kind == "audio":
            result.bot_audio_heard = True
            bot_audio_event.set()
            _bot_audio_track_ref.append(track)   # picked up by async code below
            result.event("🎧 Bot audio track captured")

    @room.on("disconnected")
    def on_disconnected(reason):
        result.event(f"🔌 Disconnected: {reason}")

    try:
        result.event(f"Connecting to LiveKit: {url}")
        await room.connect(url, token)
        result.connected = True
        result.connected_at_ms = (time.perf_counter() - t_start) * 1000
        result.event(f"✅ Connected in {result.connected_at_ms:.0f}ms")
    except Exception as e:
        result.error = f"LiveKit connect failed: {e}"
        result.event(f"❌ {result.error}")
        return result

    # ── Scan already-present participants (bot may have joined before us) ──
    # If the bot was already in the room, track_subscribed already fired on
    # their existing tracks. Grab them directly from remote_participants.
    for rp in room.remote_participants.values():
        if rp.identity.startswith("bot"):
            result.bot_joined = True
            bot_joined_event.set()
            for pub in rp.track_publications.values():
                if pub.track and pub.track.kind.name.lower() == "audio":
                    if pub.track not in _bot_audio_track_ref:
                        _bot_audio_track_ref.append(pub.track)
                        bot_audio_event.set()
                        result.event("🎧 Bot audio track found on existing participant")

    # ── 2b. Register candidate presence with Connect server (Socket.IO) ───
    # Engine polls GET /api/rooms/{room_id} — only Socket.IO participants show up.
    _presence_stop = threading.Event()
    if candidate_join_url:
        _presence_thread = threading.Thread(
            target=_candidate_socketio_presence,
            args=(CONNECT_BASE_URL, candidate_join_url, room_id, candidate_name, _presence_stop),
            daemon=True,
            name=f"candidate-presence-{room_id}",
        )
        _presence_thread.start()
        result.event("🔗 Candidate Socket.IO presence thread started")
    else:
        _presence_stop.set()  # nothing to clean up

    # ── 3. Publish audio track ─────────────────────────────────────────
    try:
        audio_source = AudioSource(sample_rate=SAMPLE_RATE, num_channels=CHANNELS)
        audio_track  = LocalAudioTrack.create_audio_track("mic", audio_source)
        await room.local_participant.publish_track(audio_track)
        result.track_published = True
        result.event("🎤 Audio track published")
    except Exception as e:
        result.error = f"Track publish failed: {e}"
        result.event(f"❌ {result.error}")
        await room.disconnect()
        return result

    # ── 3b. Publish video track (static face image) ────────────────────
    try:
        from livekit.rtc import VideoSource, LocalVideoTrack
        video_source = VideoSource(width=640, height=480)
        video_track  = LocalVideoTrack.create_video_track("camera", video_source)
        await room.local_participant.publish_track(video_track)
        result.event(f"📹 Video track published (face for interview_id={interview_id})")
    except Exception as e:
        video_source = None
        result.event(f"⚠️  Video track skipped: {e}")

    # ── 4. Stream audio + video concurrently ───────────────────────────
    result.event(f"🔊 Streaming real speech WAV for {audio_duration_s}s…")
    try:
        audio_task = asyncio.create_task(
            _stream_wav_with_recording(audio_source, recorder, audio_duration_s)
        )
        video_task = asyncio.create_task(
            stream_face_video(video_source, interview_id=interview_id, duration_s=audio_duration_s)
        ) if video_source else None

        # Wait for bot's AUDIO TRACK (not just participant join).
        # track_subscribed fires after participant_connected, so waiting for
        # bot_audio_event guarantees the track is in _bot_audio_track_ref.
        try:
            await asyncio.wait_for(asyncio.shield(bot_audio_event.wait()), wait_for_bot_s)
            result.event("🤖 Bot audio track confirmed")
        except asyncio.TimeoutError:
            result.event(f"⚠️  Bot audio track not seen after {wait_for_bot_s}s")

        # Start bot audio recording — track is guaranteed present if event fired
        if _bot_audio_track_ref:
            recorder._bot_stream_task = asyncio.create_task(
                recorder.stream_bot_audio(_bot_audio_track_ref[0])
            )
            result.event("🎧 Bot audio recording task started")
        else:
            result.event("⚠️  No bot audio track — recording candidate-only")

        # Wait for both streams to finish
        tasks_to_wait = [audio_task] + ([video_task] if video_task else [])
        await asyncio.gather(*tasks_to_wait, return_exceptions=True)
        result.event("✅ Audio + video streaming complete")

    except asyncio.CancelledError:
        result.event("⚠️  Audio task cancelled")
    except Exception as e:
        result.error = f"Audio stream error: {e}"
        result.event(f"❌ {result.error}")

    # ── 5. Save recording & leave ──────────────────────────────────────
    result.duration_s = time.perf_counter() - t_start
    recorder.stop()
    rec_path = recorder.save()
    if rec_path:
        result.recording_path = rec_path
        result.event(f"💾 Recording saved: {rec_path}")

    result.event(f"👋 Leaving room after {result.duration_s:.1f}s")
    _presence_stop.set()  # signal Socket.IO presence thread to exit
    try:
        await room.disconnect()
    except Exception:
        pass

    return result


async def _stream_wav_with_recording(audio_source, recorder: InterviewRecorder,
                                      duration_s: float):
    """
    Stream WAV audio to LiveKit while also capturing frames for the recorder.
    Wraps stream_wav_to_source() by intercepting capture_frame.
    """
    from .audio_gen import _list_wav_files, _read_wav_as_pcm16_48k, FRAME_MS
    import random

    wav_files = _list_wav_files()
    if not wav_files:
        await stream_wav_to_source(audio_source, duration_s)
        return

    chosen = random.choice(wav_files)
    pcm_data = _read_wav_as_pcm16_48k(chosen)

    frame_duration_s  = FRAME_MS / 1000.0
    total_frames      = int(duration_s / frame_duration_s)
    samples_per_frame = SAMPLE_RATE * FRAME_MS // 1000
    total_pcm_samples = len(pcm_data) // 2

    for frame_idx in range(total_frames):
        start = (frame_idx * samples_per_frame) % total_pcm_samples
        end   = start + samples_per_frame

        if end <= total_pcm_samples:
            frame_bytes = pcm_data[start*2 : end*2]
        else:
            tail = pcm_data[start*2:]
            head = pcm_data[: (end - total_pcm_samples)*2]
            frame_bytes = tail + head

        if len(frame_bytes) < samples_per_frame * 2:
            frame_bytes = frame_bytes.ljust(samples_per_frame * 2, b"\x00")

        # Record candidate audio
        recorder.add_candidate_frame(frame_bytes)

        # Send to LiveKit
        from livekit.rtc import AudioFrame
        frame = AudioFrame(
            data=frame_bytes,
            sample_rate=SAMPLE_RATE,
            num_channels=CHANNELS,
            samples_per_channel=samples_per_frame,
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(frame_duration_s)
