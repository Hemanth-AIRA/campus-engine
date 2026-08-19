"""
load_tests/recording.py
Client-side interview recording — full audio+video MP4.

Records both sides of the conversation:
  - Candidate audio (what we send — from the WAV file)
  - Bot audio (what AIRA speaks — received via LiveKit AudioStream)

Channel layout:
  - Both sides present  → stereo (L=candidate, R=bot), normalized
  - Candidate only      → mono,  normalized
  - Bot only            → mono,  normalized

Saved to load_tests/recordings/interview_{id}_{room}.mp4
"""
import asyncio, logging, struct, wave, subprocess, time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("recording")

RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

FACE_SAMPLES_DIR = Path(__file__).parent / "face_samples"

SAMPLE_RATE = 48_000


class InterviewRecorder:
    """
    Accumulates PCM frames from both candidate and bot audio streams,
    then produces an MP4 video (face image + normalized audio).
    """

    def __init__(self, room_id: str, interview_id: int):
        self.room_id      = room_id
        self.interview_id = interview_id
        self._candidate_frames: list[bytes] = []  # 16-bit PCM mono
        self._bot_frames: list[bytes]       = []  # 16-bit PCM mono
        self._recording = False
        self._bot_stream_task: Optional[asyncio.Task] = None

    def start(self):
        """Start accumulating frames."""
        self._recording = True
        logger.info("🔴 [%s] Recording started", self.room_id)

    def stop(self):
        """Stop accumulating frames."""
        self._recording = False
        if self._bot_stream_task and not self._bot_stream_task.done():
            self._bot_stream_task.cancel()
        logger.info("⏹  [%s] Recording stopped", self.room_id)

    def add_candidate_frame(self, pcm_bytes: bytes):
        """Called from audio_gen streaming — captures what we send."""
        if self._recording:
            self._candidate_frames.append(pcm_bytes)

    def add_bot_frame(self, pcm_bytes: bytes):
        """Called from bot audio stream — captures what AIRA speaks."""
        if self._recording:
            self._bot_frames.append(pcm_bytes)

    async def stream_bot_audio(self, track):
        """
        Subscribe to the bot's audio track and record incoming frames.
        This runs as a background asyncio.Task for the duration of the interview.
        """
        try:
            from livekit import rtc
            audio_stream = rtc.AudioStream(
                track,
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                frame_size_ms=20,
            )
            logger.info("🎧 [%s] Bot audio stream opened for recording", self.room_id)

            async for event in audio_stream:
                if not self._recording:
                    break
                raw = bytes(event.frame.data)
                self._bot_frames.append(raw)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("⚠️  [%s] Bot audio stream error: %s", self.room_id, e)

    def _get_face_image(self) -> Optional[Path]:
        """Get the face image for this interview (same one streamed to LiveKit)."""
        faces = sorted(FACE_SAMPLES_DIR.glob("face_*.jpg"))
        if not faces:
            return None
        return faces[self.interview_id % len(faces)]

    def save(self) -> Optional[str]:
        """
        Mix candidate + bot audio, combine with face image → MP4 video.

        Channel layout:
          - Both sides recorded  → stereo  (L=candidate, R=bot), normalized
          - Only candidate audio → mono    (candidate, normalized)
          - Only bot audio       → mono    (bot, normalized)
          - No audio at all      → skip, returns None
        """
        has_candidate = bool(self._candidate_frames)
        has_bot       = bool(self._bot_frames)

        if not has_candidate and not has_bot:
            logger.warning("[%s] No audio captured — skipping save", self.room_id)
            return None

        try:
            base_name = f"interview_{self.interview_id}_{self.room_id}"
            wav_path  = RECORDINGS_DIR / f"{base_name}.wav"
            mp4_path  = RECORDINGS_DIR / f"{base_name}.mp4"

            # ── 1. Decode PCM ───────────────────────────────────────────
            def decode(frames: list) -> list:
                raw = b"".join(frames)
                if not raw:
                    return []
                return list(struct.unpack(f"<{len(raw)//2}h", raw))

            cand_samples = decode(self._candidate_frames)
            bot_samples  = decode(self._bot_frames)

            # ── 2. Peak-normalize to -0.5 dBFS (target ~29900) ─────────
            def normalize(samples: list, target: int = 29900) -> list:
                if not samples:
                    return samples
                peak = max(abs(s) for s in samples)
                if peak == 0:
                    return samples
                gain = target / peak
                return [max(-32768, min(32767, int(s * gain))) for s in samples]

            cand_samples = normalize(cand_samples)
            bot_samples  = normalize(bot_samples)

            # ── 3. Build WAV (mono or stereo) ───────────────────────────
            if has_candidate and has_bot:
                max_len = max(len(cand_samples), len(bot_samples))
                cand_samples += [0] * (max_len - len(cand_samples))
                bot_samples  += [0] * (max_len - len(bot_samples))
                interleaved = []
                for c, b in zip(cand_samples, bot_samples):
                    interleaved.extend([c, b])
                pcm_bytes  = struct.pack(f"<{len(interleaved)}h", *interleaved)
                n_channels = 2
                n_frames   = max_len
                mode       = "stereo (L=candidate, R=bot)"
            elif has_candidate:
                pcm_bytes  = struct.pack(f"<{len(cand_samples)}h", *cand_samples)
                n_channels = 1
                n_frames   = len(cand_samples)
                mode       = "mono (candidate only)"
            else:
                pcm_bytes  = struct.pack(f"<{len(bot_samples)}h", *bot_samples)
                n_channels = 1
                n_frames   = len(bot_samples)
                mode       = "mono (bot only)"

            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(n_channels)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm_bytes)

            duration_s = n_frames / SAMPLE_RATE
            logger.info("🎵 [%s] WAV saved: %s (%.1fs, %s)",
                        self.room_id, wav_path.name, duration_s, mode)

            # Free memory
            self._candidate_frames.clear()
            self._bot_frames.clear()

            # ── 4. Mux face image + audio → MP4 ────────────────────────
            face_img = self._get_face_image()
            if face_img and face_img.exists():
                try:
                    cmd = [
                        "ffmpeg", "-y",
                        "-loop", "1",
                        "-i", str(face_img),
                        "-i", str(wav_path),
                        "-c:v", "libx264",
                        "-tune", "stillimage",
                        "-c:a", "aac",
                        "-b:a", "128k",
                        "-pix_fmt", "yuv420p",
                        "-shortest",
                        "-movflags", "+faststart",
                        str(mp4_path),
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if result.returncode == 0 and mp4_path.exists():
                        size_mb = mp4_path.stat().st_size / (1024 * 1024)
                        logger.info("🎬 [%s] MP4 recording saved: %s (%.1fs, %.1fMB)",
                                    self.room_id, mp4_path.name, duration_s, size_mb)
                        wav_path.unlink(missing_ok=True)
                        return str(mp4_path)
                    else:
                        logger.warning("⚠️  [%s] ffmpeg failed (rc=%d): %s",
                                       self.room_id, result.returncode, result.stderr[:300])
                        return str(wav_path)
                except subprocess.TimeoutExpired:
                    logger.warning("⚠️  [%s] ffmpeg timed out — keeping WAV", self.room_id)
                    return str(wav_path)
                except FileNotFoundError:
                    logger.warning("⚠️  [%s] ffmpeg not found — keeping WAV", self.room_id)
                    return str(wav_path)
            else:
                logger.info("💾 [%s] No face image — WAV only: %s", self.room_id, wav_path.name)
                return str(wav_path)

        except Exception as e:
            logger.error("❌ [%s] Failed to save recording: %s", self.room_id, e)
            return None


def recording_path(room_id: str, interview_id: int) -> str:
    """Return the expected MP4 file path for a recording."""
    return str(RECORDINGS_DIR / f"interview_{interview_id}_{room_id}.mp4")
