"""
local_recorder.py

Wall-clock-synchronized interview recorder — NO LiveKit Egress required.

Design goal: ONE timeline. A single monotonic clock (t0) is set the moment
the bot joins. A dedicated *pacer* thread is the only writer to the output
streams; on every tick it writes exactly enough data to keep each track locked
to real elapsed time:

  • Video  → exactly TARGET_FPS frames/sec. Holds the last camera frame, or
             emits BLACK when the candidate's camera is off / not yet joined /
             has left. Encoded live by FFmpeg (raw RGBA → VP8 WebM).
  • Candidate audio → 48 kHz mono PCM16. Real frames when present, SILENCE
             padding whenever the candidate track is absent.
  • Bot audio       → 24 kHz mono PCM16. Real frames while the bot speaks,
             SILENCE padding in the gaps between utterances.

Because nothing is ever "appended freely" — every byte sits at its true time
offset against one clock — the three tracks are aligned *by construction*.
Participants joining or leaving (candidate leaves early, recruiter joins, etc.)
only changes whether a tick writes real media or silence/black; it can never
shift the timeline. The final container assembly at stop() is therefore just
packaging equal-length, co-anchored tracks — not alignment guesswork.

Lifecycle (driven by the orchestrator):
    recorder = LocalVideoRecorder(room_id, interview_id, process_id)
    recorder.start()                       # call when the BOT joins
    recorder.write_video_frame(rgba, w, h) # candidate camera frames
    recorder.write_candidate(pcm_bytes)    # 48 kHz mono PCM16
    recorder.write_bot(pcm_bytes)          # 24 kHz mono PCM16
    output_path = recorder.stop()          # call when the BOT exits

Public API is unchanged from the previous version, so callers
(aira_bot_client.py, aira_3_webrtc_orchestration.py) need no changes.
"""

import logging
import os
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("local_recorder")

RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "recordings")
# Recording FPS. Lower = less scale+encode CPU per interview (the dominant cost
# under many concurrent interviews). Default 10 — plenty for interview review and
# ~1.5x cheaper than 15. Override with REC_FPS.
TARGET_FPS = int(os.environ.get("REC_FPS", "10"))
# Fixed output resolution. Incoming frames of any size are scaled to this so the
# FFmpeg rawvideo input geometry never changes mid-stream (which rawvideo can't
# handle) and so we can emit black frames before the first real frame arrives.
# Default 320x240: the candidate camera is ~240x180, so 640x480 just upscaled it
# and paid ~4x the pixels in scale+encode CPU for no real detail. Override with
# REC_WIDTH / REC_HEIGHT.
REC_WIDTH = int(os.environ.get("REC_WIDTH", "320"))
REC_HEIGHT = int(os.environ.get("REC_HEIGHT", "240"))


CAND_RATE = 48000   # candidate audio sample rate (LiveKit AudioStream)
BOT_RATE = 24000    # bot audio sample rate (Cartesia TTS output)

_BLACK_FRAME = bytes(REC_WIDTH * REC_HEIGHT * 4)  # RGBA all-zero = black
 

# ─────────────────────────────────────────────────────────────────────────────
# DELETION TRACER (diagnostic)
# Prod evidence shows a recording's _video.webm + .fflog vanish MID-interview
# while ffmpeg is still alive (poll=None) — i.e. their directory entries are
# removed out from under a running encoder. This wraps every in-process file
# removal/rename (os.remove/unlink/replace/rename — which pathlib.Path.unlink
# and shutil.rmtree also route through) and, when the target is a recordings
# file, logs the FULL stack + pid so we see exactly who deletes it. If a
# recording is lost but NOTHING here logs, the deleter is OUT of this process
# (another PVC mounter / cronjob / the Sentinel). Toggle: AIRA_TRACE_REC_DELETES.
# ─────────────────────────────────────────────────────────────────────────────
def _install_recording_deletion_tracer() -> None:
    if os.environ.get("AIRA_TRACE_REC_DELETES", "true").lower() != "true":
        return
    if getattr(os, "_aira_rec_tracer_installed", False):
        return
    os._aira_rec_tracer_installed = True

    import traceback as _traceback
    _rec_dir = str(Path(RECORDINGS_DIR).absolute())

    def _is_rec(path) -> bool:
        try:
            p = os.path.abspath(os.fspath(path))
        except Exception:
            return False
        if not p.startswith(_rec_dir):
            return False
        return p.endswith((".webm", ".wav", ".fflog"))

    def _log(op, path, extra=""):
        stack = "".join(_traceback.format_stack()[:-1][-10:])
        logger.error(
            "🔴 REC-DELETE-TRACE pid=%s op=%s target=%s %s\n%s",
            os.getpid(), op, path, extra, stack,
        )

    def _wrap1(mod, name):
        orig = getattr(mod, name)
        def wrapper(path, *a, **k):
            if _is_rec(path):
                _log(name, path)
            return orig(path, *a, **k)
        setattr(mod, name, wrapper)

    def _wrap2(mod, name):
        orig = getattr(mod, name)
        def wrapper(src, dst, *a, **k):
            if _is_rec(src) or _is_rec(dst):
                _log(name, src, extra=f"-> {dst}")
            return orig(src, dst, *a, **k)
        setattr(mod, name, wrapper)

    for nm in ("remove", "unlink"):
        _wrap1(os, nm)
    for nm in ("replace", "rename"):
        _wrap2(os, nm)

    # Python 3.11's pathlib binds os.unlink at import, so patching os.unlink does
    # NOT catch Path.unlink() (used at :740/:835). Wrap the Path methods too.
    _orig_path_unlink = Path.unlink
    def _path_unlink(self, *a, **k):
        if _is_rec(self):
            _log("Path.unlink", str(self))
        return _orig_path_unlink(self, *a, **k)
    Path.unlink = _path_unlink

    for pm in ("rename", "replace"):
        _orig = getattr(Path, pm)
        def _mk(_orig, pm):
            def _pw(self, target, *a, **k):
                if _is_rec(self) or _is_rec(target):
                    _log(f"Path.{pm}", str(self), extra=f"-> {target}")
                return _orig(self, target, *a, **k)
            return _pw
        setattr(Path, pm, _mk(_orig, pm))

    logger.info("🔍 [local_recorder] recording-deletion tracer installed (dir=%s)", _rec_dir)


_install_recording_deletion_tracer()


class LocalVideoRecorder:
    """Wall-clock-synchronized recorder: candidate video + both audio → WebM."""

    def __init__(self, room_id: str, interview_id, process_id: int = 0):
        self.room_id = room_id
        self.interview_id = interview_id
        self.process_id = process_id

        # Resolve to an ABSOLUTE dir: the ffmpeg subprocess inherits its cwd at
        # spawn time, while stop()'s existence checks run in the (possibly
        # different) cwd of that moment. A relative "recordings" would then point
        # the encoder and the checker at different directories — the file gets
        # written somewhere stop() never looks, and we wrongly fall back to
        # audio-only. Absolute paths make the recorder cwd-independent.
        self._dir = Path(RECORDINGS_DIR).absolute()
        self._dir.mkdir(parents=True, exist_ok=True)

        # Final filename matches the expected format:
        #   {room_id}_{interview_id}_{UTC-ISO-with-millis}Z.webm
        # e.g. AIRA-20260617-1024-FA93_185_2026-06-18T06-00-11-880Z.webm
        # (':' and '.' are filesystem-unfriendly, so ISO separators use '-'.)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
        base = f"{room_id}_{interview_id}"
        self._candidate_audio_path = self._dir / f"{base}_candidate.wav"
        self._bot_audio_path = self._dir / f"{base}_bot.wav"
        # H.264/MP4: libx264 is ~10x cheaper than libvpx VP8 to encode, which is the
        # dominant per-interview CPU cost (VP8's cost floor doesn't shrink with
        # resolution). Container is MP4 accordingly.
        self._video_only_path = self._dir / f"{base}_video.mp4"
        self._final_path = self._dir / f"{base}_{ts}.mp4"

        # Audio writers
        self._candidate_wf: Optional[wave.Wave_write] = None
        self._bot_wf: Optional[wave.Wave_write] = None

        # FFmpeg video process
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        # ffmpeg stderr is redirected to a file, never an unread PIPE: over a
        # long interview an unread stderr PIPE fills its ~64KB OS buffer and
        # BLOCKS ffmpeg mid-encode (video freezes partway). A file has no such cap.
        self._ffmpeg_stderr_fh = None
        self._ffmpeg_stderr_path: Optional[str] = None

        # The single clock. Set in start().
        self._t0 = 0.0

        # Pacer thread — the ONLY writer to all output streams.
        self._pacer: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._frame_interval = 1.0 / TARGET_FPS

        # Latest camera frame (already scaled to REC_WIDTH x REC_HEIGHT).
        self._latest_frame = _BLACK_FRAME
        self._have_real_frame = False

        # Counters (in SAMPLES / FRAMES) tracking what's been written so far.
        self._cand_samples = 0
        self._bot_samples = 0
        self._frames_written = 0

        # Byte tallies of REAL (non-silence) audio — used to decide whether a
        # track has any content worth muxing, and for stats.
        self._candidate_bytes = 0
        self._bot_bytes = 0

        # Locks: audio counters/writers vs. the shared latest-frame buffer.
        self._lock = threading.Lock()
        self._video_lock = threading.Lock()
        self._active = False
        # Set by end_capture() when the conversation ends: freezes all writing +
        # the pacer WITHOUT finalizing, so the post-interview analysis/cleanup
        # window isn't recorded as a blank tail. stop() still does the mux.
        self._capture_frozen = False

        # ffmpeg health monitor — check every 30s during recording
        self._monitor_interval = TARGET_FPS * 30  # every 30s worth of frames
        self._last_monitor_frame = 0

    # ────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ────────────────────────────────────────────────────────────────────

    def start(self):
        """Open outputs, start FFmpeg, and launch the pacer. Call at bot join."""
        with self._lock:
            if self._active:
                return

            # Candidate audio: 48 kHz mono 16-bit
            self._candidate_wf = wave.open(str(self._candidate_audio_path), "wb")
            self._candidate_wf.setnchannels(1)
            self._candidate_wf.setsampwidth(2)
            self._candidate_wf.setframerate(CAND_RATE)

            # Bot audio: 24 kHz mono 16-bit
            self._bot_wf = wave.open(str(self._bot_audio_path), "wb")
            self._bot_wf.setnchannels(1)
            self._bot_wf.setsampwidth(2)
            self._bot_wf.setframerate(BOT_RATE)

            self._cand_samples = 0
            self._bot_samples = 0
            self._frames_written = 0
            self._candidate_bytes = 0
            self._bot_bytes = 0
            self._latest_frame = _BLACK_FRAME
            self._have_real_frame = False
            self._capture_frozen = False

            self._start_ffmpeg()

            self._active = True
            self._stop_evt.clear()
            self._t0 = time.monotonic()

        # Pacer started outside the lock; it acquires locks per tick.
        self._pacer = threading.Thread(
            target=self._pace_loop, name=f"recorder-pacer-{self.room_id}", daemon=True
        )
        self._pacer.start()

        logger.info(
            "🎬 [process id: %s][room: %s] Synchronized recording STARTED "
            "(%dx%d @ %dfps, single wall-clock) → %s",
            self.process_id, self.room_id, REC_WIDTH, REC_HEIGHT, TARGET_FPS, self._dir,
        )

        # Startup verification: after 1s, confirm ffmpeg is alive and writing stderr
        def _verify_startup():
            time.sleep(1.0)
            if self._ffmpeg_proc:
                rc = self._ffmpeg_proc.poll()
                fflog_sz = 0
                try:
                    if self._ffmpeg_stderr_path and os.path.exists(self._ffmpeg_stderr_path):
                        fflog_sz = os.path.getsize(self._ffmpeg_stderr_path)
                except OSError:
                    pass
                if rc is not None:
                    logger.error(
                        "❌ [process id: %s][room: %s] FFmpeg DIED within 1s of start! "
                        "pid=%s rc=%s fflog_size=%d",
                        self.process_id, self.room_id, self._ffmpeg_proc.pid, rc, fflog_sz,
                    )
                elif fflog_sz == 0:
                    logger.warning(
                        "⚠️ [process id: %s][room: %s] FFmpeg alive but stderr EMPTY "
                        "after 1s — pid=%s (stderr may not be connected to fflog)",
                        self.process_id, self.room_id, self._ffmpeg_proc.pid,
                    )
                else:
                    logger.info(
                        "✅ [process id: %s][room: %s] FFmpeg startup OK | pid=%s "
                        "fflog_size=%d (banner written)",
                        self.process_id, self.room_id, self._ffmpeg_proc.pid, fflog_sz,
                    )
        threading.Thread(target=_verify_startup, daemon=True).start()

    def _start_ffmpeg(self):
        """Start the FFmpeg encoder for the fixed-geometry raw video pipe."""
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgba",
            "-s", f"{REC_WIDTH}x{REC_HEIGHT}",
            "-r", str(TARGET_FPS),
            "-i", "pipe:0",
            # H.264 via libx264, ultrafast — far cheaper than libvpx VP8 (the CPU
            # hog). zerolatency suits realtime pipe input. Fragmented-MP4 flags make
            # the file playable even if ffmpeg is killed mid-interview (no trailing
            # moov needed), so the salvage/sweep path still recovers video.
            "-c:v", "libx264",
            "-preset", os.environ.get("REC_X264_PRESET", "ultrafast"),
            "-tune", "zerolatency",
            "-b:v", os.environ.get("REC_VIDEO_BITRATE", "1M"),
            "-pix_fmt", "yuv420p",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            str(self._video_only_path),
        ]
        # Redirect stderr to a file (NOT a PIPE) — an unread PIPE deadlocks a
        # long-running ffmpeg once its 64KB buffer fills, freezing the encode.
        self._ffmpeg_stderr_path = str(self._video_only_path) + ".fflog"
        try:
            self._ffmpeg_stderr_fh = open(self._ffmpeg_stderr_path, "wb")
        except OSError:
            self._ffmpeg_stderr_fh = None
        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=(self._ffmpeg_stderr_fh or subprocess.DEVNULL),
            )
            logger.info(
                "🎥 [process id: %s][room: %s] FFmpeg encoder started "
                "| pid=%s out=%s fflog_open=%s cwd=%s",
                self.process_id, self.room_id, self._ffmpeg_proc.pid,
                str(self._video_only_path), bool(self._ffmpeg_stderr_fh), os.getcwd(),
            )
        except FileNotFoundError:
            logger.error(
                "❌ [process id: %s][room: %s] FFmpeg not found! Video disabled "
                "(audio-only fallback will be used). Install ffmpeg.",
                self.process_id, self.room_id,
            )
            self._ffmpeg_proc = None
        except Exception as e:
            logger.error(
                "❌ [process id: %s][room: %s] FFmpeg start failed: %s",
                self.process_id, self.room_id, e,
            )
            self._ffmpeg_proc = None

    # ────────────────────────────────────────────────────────────────────
    # The pacer — sole writer, keeps every track locked to wall-clock time
    # ────────────────────────────────────────────────────────────────────

    def _pace_loop(self):
        next_tick = time.monotonic()
        while not self._stop_evt.is_set():
            self._tick()
            next_tick += self._frame_interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                # Wake early if stop() is signalled.
                self._stop_evt.wait(sleep_for)
            else:
                # We're behind schedule (slow encode / GIL); don't sleep, catch up.
                next_tick = time.monotonic()

    def _tick(self):
        # Once stop() is signalled, never write again — _stop_ffmpeg() is about to
        # close stdin (which cleanly EOFs ffmpeg). Skipping here keeps the pacer
        # from re-entering a blocking write right as we're trying to shut down.
        if self._stop_evt.is_set():
            return

        elapsed = time.monotonic() - self._t0

        # ── Video: write enough frames to reach round(elapsed * FPS) ──
        target_frames = int(elapsed * TARGET_FPS)
        if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
            with self._video_lock:
                frame = self._latest_frame
            deficit = target_frames - self._frames_written
            if deficit > 0:
                # Write the FULL deficit so _frames_written tracks wall-clock
                # (target_frames = elapsed*FPS) — this keeps the video length equal
                # to the audio length (capping the total would leave a permanent
                # backlog → seconds of A/V drift). BUT do it in bounded chunks: a
                # single `frame * deficit` for a big backlog is one large `bytes*N`
                # memcpy, and bytes*N does NOT release the GIL mid-op — a long hold
                # starves the bot's real-time audio loop in THIS process and breaks
                # the voice under load. Chunks cap each allocation to ~1s of frames,
                # and the blocking write between chunks releases the GIL for audio.
                # NB: NOT under _video_lock — _stop_ffmpeg() closes stdin under that
                # lock to unblock a stalled write (BrokenPipeError below); locking
                # would deadlock stop() against a stuck encoder.
                try:
                    remaining = deficit
                    while remaining > 0 and not self._stop_evt.is_set():
                        n = TARGET_FPS if remaining > TARGET_FPS else remaining
                        self._ffmpeg_proc.stdin.write(frame if n == 1 else frame * n)
                        self._frames_written += n
                        remaining -= n
                except (BrokenPipeError, OSError) as pipe_err:
                    logger.warning(
                        "⚠️ [process id: %s][room: %s] FFmpeg pipe broke at frame %d — "
                        "video stopped | err=%s",
                        self.process_id, self.room_id, self._frames_written, pipe_err,
                    )
                    self._ffmpeg_proc = None

            # ── Periodic ffmpeg health check (every ~30s) ──
            if (self._ffmpeg_proc and
                    self._frames_written - self._last_monitor_frame >= self._monitor_interval):
                self._last_monitor_frame = self._frames_written
                try:
                    rc = self._ffmpeg_proc.poll()
                    out_exists = self._video_only_path.exists()
                    out_sz = self._video_only_path.stat().st_size if out_exists else 0
                    fflog_sz = 0
                    if self._ffmpeg_stderr_path and os.path.exists(self._ffmpeg_stderr_path):
                        fflog_sz = os.path.getsize(self._ffmpeg_stderr_path)
                    logger.info(
                        "🩺 [process id: %s][room: %s] FFmpeg health @ frame %d (%.0fs) "
                        "| pid=%s poll=%s out=%s/%dB fflog=%dB",
                        self.process_id, self.room_id, self._frames_written,
                        elapsed, self._ffmpeg_proc.pid, rc,
                        out_exists, out_sz, fflog_sz,
                    )
                    if rc is not None:
                        logger.error(
                            "❌ [process id: %s][room: %s] FFmpeg DEAD mid-recording! "
                            "pid=%s rc=%s — video will be lost",
                            self.process_id, self.room_id, self._ffmpeg_proc.pid, rc,
                        )
                except Exception:
                    pass

        # ── Audio: pad silence up to the wall-clock target on each track ──
        with self._lock:
            self._pad_silence_locked(
                self._candidate_wf, CAND_RATE, elapsed, is_bot=False
            )
            self._pad_silence_locked(
                self._bot_wf, BOT_RATE, elapsed, is_bot=True
            )

    def _pad_silence_locked(self, wf, rate, elapsed, is_bot):
        """Fill the track with silence up to round(elapsed * rate) samples.
        Caller must hold self._lock."""
        if wf is None:
            return
        target = int(elapsed * rate)
        written = self._bot_samples if is_bot else self._cand_samples
        deficit = target - written
        if deficit <= 0:
            return
        try:
            wf.writeframes(b"\x00\x00" * deficit)
        except Exception:
            return
        if is_bot:
            self._bot_samples += deficit
        else:
            self._cand_samples += deficit

    # ────────────────────────────────────────────────────────────────────
    # Capture inputs (called from the bot client's async callbacks)
    # ────────────────────────────────────────────────────────────────────

    def write_video_frame(self, rgba_data: bytes, width: int, height: int):
        """Store the latest camera frame (scaled to fixed geometry). The pacer
        thread is what actually feeds FFmpeg, so this never blocks on I/O."""
        if not self._active or self._capture_frozen:
            return
        scaled = self._scale_frame(rgba_data, width, height)
        if scaled is None:
            return
        with self._video_lock:
            self._latest_frame = scaled
            self._have_real_frame = True

    def _scale_frame(self, rgba_data: bytes, width: int, height: int) -> Optional[bytes]:
        """Scale an arbitrary-size RGBA frame to REC_WIDTH x REC_HEIGHT."""
        if width == REC_WIDTH and height == REC_HEIGHT:
            if len(rgba_data) == len(_BLACK_FRAME):
                return rgba_data
            return None
        try:
            import numpy as np
            import cv2
            arr = np.frombuffer(rgba_data, dtype=np.uint8)
            if arr.size != width * height * 4:
                return None
            arr = arr.reshape((height, width, 4))
            resized = cv2.resize(arr, (REC_WIDTH, REC_HEIGHT), interpolation=cv2.INTER_AREA)
            return resized.tobytes()
        except Exception:
            # No cv2/numpy or bad data — drop this frame; pacer holds last/black.
            return None

    def write_candidate(self, pcm_bytes: bytes):
        """Write candidate PCM16 audio (48 kHz mono) at its true time offset."""
        if not self._active or self._capture_frozen:
            return
        with self._lock:
            if self._candidate_wf:
                try:
                    self._candidate_wf.writeframes(pcm_bytes)
                    self._cand_samples += len(pcm_bytes) // 2
                    self._candidate_bytes += len(pcm_bytes)
                except Exception:
                    pass

    def write_bot(self, pcm_bytes: bytes):
        """Write bot PCM16 audio (24 kHz mono) at its true time offset."""
        if not self._active or self._capture_frozen:
            return
        with self._lock:
            if self._bot_wf:
                try:
                    self._bot_wf.writeframes(pcm_bytes)
                    self._bot_samples += len(pcm_bytes) // 2
                    self._bot_bytes += len(pcm_bytes)
                except Exception:
                    pass

    # ────────────────────────────────────────────────────────────────────
    # Stop & finalize
    # ────────────────────────────────────────────────────────────────────

    def end_capture(self) -> None:
        """Freeze the recording at the END OF THE CONVERSATION, without finalizing.

        Call this the moment the interview conversation is over (before the ~30s
        of post-interview analysis / report / email / cleanup). It stops the pacer
        and suppresses further writes, so that dead tail is NOT recorded as a blank
        (black + silence) segment — the recording ends at the real conversation
        end. FFmpeg and the WAV writers stay open; stop() (called later in the
        orchestrator's finally) still closes them and muxes the frozen content.
        Safe to call once; stop() works whether or not this ran first.
        """
        if not self._active or self._capture_frozen:
            return
        self._capture_frozen = True
        self._stop_evt.set()
        pacer = self._pacer
        if pacer:
            pacer.join(timeout=10)
            self._pacer = None
        logger.info(
            "🎬 [process id: %s][room: %s] Capture frozen at conversation end "
            "(video=%d frames, %.1fs) — post-interview tail will not be recorded",
            self.process_id, self.room_id, self._frames_written,
            self._frames_written / TARGET_FPS if self._frames_written else 0,
        )

    def stop(self) -> Optional[str]:
        """Stop the pacer, close outputs, and assemble the final WebM.
        Call when the BOT exits. Returns the final media path, or None."""
        with self._lock:
            if not self._active:
                return None
            self._active = False

        # Stop the pacer first so nothing else writes after this point.
        self._stop_evt.set()
        if self._pacer:
            self._pacer.join(timeout=10)
            self._pacer = None

        with self._lock:
            if self._candidate_wf:
                self._candidate_wf.close()
                self._candidate_wf = None
            if self._bot_wf:
                self._bot_wf.close()
                self._bot_wf = None

        self._stop_ffmpeg()

        elapsed = time.monotonic() - self._t0
        logger.info(
            "🎬 [process id: %s][room: %s] Recording STOPPED | wall=%.0fs | "
            "video=%d frames (%.1fs) | candidate_audio=%.1fs | bot_audio=%.1fs",
            self.process_id, self.room_id, elapsed,
            self._frames_written, self._frames_written / TARGET_FPS if self._frames_written else 0,
            self._cand_samples / CAND_RATE, self._bot_samples / BOT_RATE,
        )

        # _stop_ffmpeg() now polls for the file to actually appear (handles the
        # asyncio child-watcher race), so by the time we reach here the file
        # state is definitive — no settle loop needed.
        _v_exists = self._video_only_path.exists()
        _v_size = self._video_only_path.stat().st_size if _v_exists else 0

        # Assemble video + stereo audio (co-anchored, equal-length → aligned).
        if _v_exists and _v_size > 0 and self._frames_written > 0:
            try:
                final = self._mux_final_webm()
                if final:
                    size_mb = os.path.getsize(final) / 1e6
                    logger.info(
                        "✅ [process id: %s][room: %s] Final recording: %s (%.1f MB)",
                        self.process_id, self.room_id, final, size_mb,
                    )
                    return str(final)
            except Exception as e:
                logger.error(
                    "❌ [process id: %s][room: %s] Final mux failed: %s",
                    self.process_id, self.room_id, e,
                )

        # Fallback: audio-only stereo WAV (still wall-clock aligned).
        try:
            audio_path = self._merge_audio_only()
            if audio_path:
                logger.info(
                    "🔊 [process id: %s][room: %s] Audio-only fallback: %s",
                    self.process_id, self.room_id, audio_path,
                )
                return str(audio_path)
        except Exception as e:
            logger.error(
                "❌ [process id: %s][room: %s] Audio merge failed: %s",
                self.process_id, self.room_id, e,
            )

        return None

    def _stop_ffmpeg(self):
        """Stop ffmpeg and wait for the output file to actually appear.

        IMPORTANT: We cannot trust Popen.wait() return value in the live
        interview process.  The bot runs a background asyncio event loop whose
        child-watcher reaps ffmpeg's SIGCHLD before subprocess can.  Python's
        _try_wait() then returns sts=0 while ffmpeg is still flushing its WebM
        trailer — we'd see out_exists=False, fall back to audio-only, and the
        .webm appears moments later as an orphan.

        Fix: close stdin (sends EOF → ffmpeg starts finalizing), then poll for
        the OUTPUT FILE to appear with non-zero size.  That's the only reliable
        signal that ffmpeg actually finished writing.
        """
        with self._video_lock:
            if not self._ffmpeg_proc:
                return

            proc = self._ffmpeg_proc
            pid = proc.pid

            # 1. Close stdin → sends EOF to ffmpeg → triggers WebM trailer write
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass

            # 2. Close our fflog file handle so ffmpeg's stderr writes hit disk
            try:
                if self._ffmpeg_stderr_fh:
                    self._ffmpeg_stderr_fh.flush()
                    self._ffmpeg_stderr_fh.close()
            except Exception:
                pass
            self._ffmpeg_stderr_fh = None

            # 3. Poll for BOTH: process exit AND output file on disk.
            #    We give ffmpeg up to 60s to finalize (WebM trailer flush).
            #    Don't trust wait()/poll() alone — check the FILE.
            deadline = time.monotonic() + 60
            file_found = False
            process_done = False

            while time.monotonic() < deadline:
                # Check process state (non-blocking)
                if not process_done:
                    try:
                        rc = proc.poll()
                        if rc is not None:
                            process_done = True
                    except Exception:
                        process_done = True

                # Check file state
                try:
                    if (self._video_only_path.exists()
                            and self._video_only_path.stat().st_size > 0):
                        file_found = True
                except OSError:
                    pass

                if file_found:
                    # File exists — ffmpeg has flushed.  Wait a tiny bit more
                    # for the process to fully exit (avoid zombie).
                    if not process_done:
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                    break

                if process_done and not file_found:
                    # Process exited but no file yet — could be asyncio reap.
                    # Keep polling the filesystem for a few more seconds.
                    pass

                time.sleep(0.3)

            # 4. If we timed out with no file, force-kill
            if not file_found:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass

            # 5. Read fflog for diagnostics (file is on disk now, ffmpeg is done)
            stderr_tail = ""
            fflog_exists = False
            fflog_size = 0
            try:
                if self._ffmpeg_stderr_path:
                    fflog_exists = os.path.exists(self._ffmpeg_stderr_path)
                    if fflog_exists:
                        fflog_size = os.path.getsize(self._ffmpeg_stderr_path)
                        with open(self._ffmpeg_stderr_path, "r", errors="replace") as _fh:
                            stderr_tail = _fh.read()[-600:]
            except Exception as fflog_err:
                logger.warning(
                    "⚠️ [process id: %s][room: %s] fflog read error: %s",
                    self.process_id, self.room_id, fflog_err,
                )

            # 6. Re-check file state for logging
            try:
                _exists = self._video_only_path.exists()
                _size = self._video_only_path.stat().st_size if _exists else 0
            except OSError:
                _exists, _size = False, 0

            elapsed = time.monotonic() - (deadline - 60)  # how long we waited

            if file_found:
                logger.info(
                    "🎥 [process id: %s][room: %s] FFmpeg finalized | "
                    "file appeared after %.1fs | pid=%s out_exists=%s out_size=%d "
                    "out=%s | stderr_tail=%s",
                    self.process_id, self.room_id, elapsed, pid,
                    _exists, _size, str(self._video_only_path), stderr_tail,
                )
            else:
                # List actual directory contents to see if the file IS there
                # but Path.exists() somehow can't find it
                try:
                    dir_listing = os.listdir(str(self._dir))
                except Exception as _le:
                    dir_listing = f"ls error: {_le}"
                logger.error(
                    "❌ [process id: %s][room: %s] FFmpeg output MISSING after 60s! "
                    "pid=%s process_done=%s | out=%s | fflog_exists=%s fflog_size=%d "
                    "| dir_contents=%s | stderr_tail=%s",
                    self.process_id, self.room_id, pid, process_done,
                    str(self._video_only_path), fflog_exists, fflog_size,
                    dir_listing, stderr_tail,
                )

            self._ffmpeg_proc = None

            # Only remove .fflog if the video file was produced successfully.
            # On failure, keep it for debugging.
            if file_found:
                try:
                    if self._ffmpeg_stderr_path and os.path.exists(self._ffmpeg_stderr_path):
                        os.remove(self._ffmpeg_stderr_path)
                except OSError:
                    pass

    def _mux_final_webm(self) -> Optional[Path]:
        """Mux the VP8 video-only WebM + the two audio WAVs into the final WebM.
        Bot → left channel, candidate → right channel. All three are the same
        wall-clock length, so a straight mux is already in sync.

        A single finicky filter/codec must never cost us the whole recording, so
        we try a series of mux strategies (best quality first). If every strategy
        fails, we PRESERVE THE VIDEO by promoting the video-only WebM to the final
        path (its audio WAVs are kept on disk for manual recovery) rather than
        silently degrading to audio-only.
        """
        has_candidate_audio = self._candidate_audio_path.exists() and self._cand_samples > 0
        has_bot_audio = self._bot_audio_path.exists() and self._bot_samples > 0

        vid = str(self._video_only_path)
        bot = str(self._bot_audio_path)
        cand = str(self._candidate_audio_path)
        out = str(self._final_path)

        # Video: prefer stream-copy (video is already H.264). Re-encode (libx264,
        # cheap) is a fallback if `-c:v copy` chokes on the source's timestamps.
        # Audio → AAC for the MP4 container. -movflags +faststart puts the moov up
        # front so the file plays without downloading fully.
        vcopy = ["-c:v", "copy"]
        vreenc = ["-c:v", "libx264", "-preset", os.environ.get("REC_X264_PRESET", "ultrafast"),
                  "-tune", "zerolatency", "-pix_fmt", "yuv420p"]

        def _cmd(inputs, filt, vopt, acodec):
            return (["ffmpeg", "-y"] + inputs + filt + vopt
                    + ["-c:a", acodec, "-b:a", "128k", "-movflags", "+faststart", out])

        strategies = []  # list of (label, cmd), best first
        if has_candidate_audio and has_bot_audio:
            ins = ["-i", vid, "-i", bot, "-i", cand]
            join = ["-filter_complex",
                    "[1:a]aresample=48000[b];"
                    "[b][2:a]join=inputs=2:channel_layout=stereo[a]",
                    "-map", "0:v", "-map", "[a]"]
            # amerge is more tolerant than join when input layouts/lengths differ.
            amerge = ["-filter_complex",
                      "[1:a]aresample=48000[b];[2:a]aresample=48000[c];"
                      "[b][c]amerge=inputs=2[a]",
                      "-map", "0:v", "-map", "[a]"]
            strategies += [
                ("join+aac+copy",     _cmd(ins, join,   vcopy,  "aac")),
                ("join+aac+reencode", _cmd(ins, join,   vreenc, "aac")),
                ("amerge+aac+copy",   _cmd(ins, amerge, vcopy,  "aac")),
                ("join+aac+reencode2", _cmd(ins, join,  vreenc, "aac")),
            ]
        elif has_candidate_audio or has_bot_audio:
            aud = cand if has_candidate_audio else bot
            ins = ["-i", vid, "-i", aud]
            m = ["-map", "0:v", "-map", "1:a"]
            strategies += [
                ("single+aac+copy",     _cmd(ins, m, vcopy,  "aac")),
                ("single+aac+reencode", _cmd(ins, m, vreenc, "aac")),
            ]
        else:
            ins = ["-i", vid]
            m = ["-map", "0:v"]
            strategies += [
                ("video-copy",     ["ffmpeg", "-y"] + ins + m + vcopy + ["-movflags", "+faststart", out]),
                ("video-reencode", ["ffmpeg", "-y"] + ins + m + vreenc + ["-movflags", "+faststart", out]),
            ]

        for label, cmd in strategies:
            logger.info(
                "🔄 [process id: %s][room: %s] Assembling final WebM (%s)...",
                self.process_id, self.room_id, label,
            )
            try:
                result = subprocess.run(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120
                )
            except Exception as e:
                logger.warning(
                    "⚠️ [process id: %s][room: %s] Mux '%s' errored: %s",
                    self.process_id, self.room_id, label, e,
                )
                continue

            if (result.returncode == 0 and self._final_path.exists()
                    and self._final_path.stat().st_size > 0):
                for p in [self._video_only_path, self._candidate_audio_path, self._bot_audio_path]:
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
                logger.info(
                    "✅ [process id: %s][room: %s] Mux OK via '%s'",
                    self.process_id, self.room_id, label,
                )
                return self._final_path

            logger.warning(
                "⚠️ [process id: %s][room: %s] Mux '%s' failed (code %s): %s",
                self.process_id, self.room_id, label, result.returncode,
                result.stderr.decode(errors="replace")[-300:],
            )

        # Every mux strategy failed — keep the VIDEO instead of losing it.
        # Promote the video-only WebM to the final path (audio absent, but the
        # raw WAVs stay on disk for recovery). One file ships, video preserved.
        logger.error(
            "❌ [process id: %s][room: %s] All mux strategies failed — shipping "
            "video-only WebM as final (audio WAVs kept on disk for recovery)",
            self.process_id, self.room_id,
        )
        try:
            if self._video_only_path.exists() and self._video_only_path.stat().st_size > 0:
                os.replace(str(self._video_only_path), str(self._final_path))
                return self._final_path
        except Exception as e:
            logger.error(
                "❌ [process id: %s][room: %s] video-only promotion failed: %s",
                self.process_id, self.room_id, e,
            )
        return None

    def _merge_audio_only(self) -> Optional[Path]:
        """Fallback when video is unavailable: merge the two wall-clock WAVs
        into a single stereo WAV (left=bot, right=candidate)."""
        import array

        has_cand = self._candidate_audio_path.exists() and self._cand_samples > 0
        has_bot = self._bot_audio_path.exists() and self._bot_samples > 0
        if not has_cand and not has_bot:
            return None
        if not has_bot:
            return self._candidate_audio_path
        if not has_cand:
            return self._bot_audio_path

        with wave.open(str(self._candidate_audio_path), "rb") as wf:
            cand_rate = wf.getframerate()
            cand_data = wf.readframes(wf.getnframes())
        with wave.open(str(self._bot_audio_path), "rb") as wf:
            bot_rate = wf.getframerate()
            bot_data = wf.readframes(wf.getnframes())

        cand_samples = array.array("h"); cand_samples.frombytes(cand_data)
        bot_samples = array.array("h"); bot_samples.frombytes(bot_data)

        # Upsample bot (24k) → candidate rate (48k) so both share a sample clock.
        if bot_rate != cand_rate and bot_rate > 0:
            ratio = cand_rate / bot_rate
            new_len = int(len(bot_samples) * ratio)
            upsampled = array.array("h", [0] * new_len)
            for i in range(new_len):
                src = i / ratio
                idx = int(src)
                if idx >= len(bot_samples) - 1:
                    upsampled[i] = bot_samples[-1] if bot_samples else 0
                else:
                    frac = src - idx
                    upsampled[i] = int(
                        bot_samples[idx] * (1 - frac) + bot_samples[idx + 1] * frac
                    )
            bot_samples = upsampled

        max_len = max(len(cand_samples), len(bot_samples))
        while len(cand_samples) < max_len:
            cand_samples.append(0)
        while len(bot_samples) < max_len:
            bot_samples.append(0)

        stereo = array.array("h", [0] * (max_len * 2))
        for i in range(max_len):
            stereo[i * 2] = bot_samples[i]
            stereo[i * 2 + 1] = cand_samples[i]

        audio_fallback = self._dir / f"{self.room_id}_{self.interview_id}.wav"
        with wave.open(str(audio_fallback), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(cand_rate)
            wf.writeframes(stereo.tobytes())

        for p in [self._candidate_audio_path, self._bot_audio_path]:
            try:
                p.unlink()
            except OSError:
                pass

        return audio_fallback

    # ────────────────────────────────────────────────────────────────────

    def salvage(self) -> Optional[str]:
        """Assemble a final file from EXISTING intermediates left by a crashed run
        (no active recording). A fresh LocalVideoRecorder(room_id, interview_id) points
        at the same intermediate paths, so this re-muxes them. Runtime sample counters
        are unknown here, so audio presence is derived from the files on disk. Returns
        the final path (str) or None if nothing salvageable.
        """
        def _has(p) -> bool:
            try:
                return p.exists() and p.stat().st_size > 1024   # > header + a little
            except OSError:
                return False

        # Reconstruct the audio-presence signals _mux/_merge gate on.
        self._cand_samples = 1 if _has(self._candidate_audio_path) else 0
        self._bot_samples = 1 if _has(self._bot_audio_path) else 0

        if _has(self._video_only_path):
            try:
                final = self._mux_final_webm()   # deletes intermediates on success
                if final:
                    logger.info("🩹 [process id: %s][room: %s] Salvaged final recording: %s",
                                self.process_id, self.room_id, final)
                    return str(final)
            except Exception as e:
                logger.error("❌ [process id: %s][room: %s] Salvage mux failed: %s",
                             self.process_id, self.room_id, e)

        # Fallback: audio-only, if any audio survived.
        try:
            audio = self._merge_audio_only()
            if audio:
                logger.info("🩹🔊 [process id: %s][room: %s] Salvaged audio-only: %s",
                            self.process_id, self.room_id, audio)
                return str(audio)
        except Exception as e:
            logger.error("❌ [process id: %s][room: %s] Salvage audio-merge failed: %s",
                         self.process_id, self.room_id, e)
        return None

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def merged_path(self) -> str:
        return str(self._final_path)


# ── Backward compatibility alias ──
LocalRecorder = LocalVideoRecorder
#dummy push