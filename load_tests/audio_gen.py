"""
load_tests/audio_gen.py  —  Audio generators for load testing.

Primary:  stream_wav_to_source()    — streams a real speech WAV from
          load_tests/audio_samples/ picked at random. Deepgram transcribes
          real speech so the engine's record_transcribe() completes normally.

Fallback: stream_speech_like_to_source() — synthetic PCM (kept for cases
          where audio_samples/ is missing). Deepgram won't transcribe it,
          but the engine will time-out via max_ms and continue.
"""
import math, struct, asyncio, random, wave, os
from pathlib import Path
from typing import Optional

try:
    from livekit.rtc import AudioFrame
    HAS_LIVEKIT = True
except ImportError:
    HAS_LIVEKIT = False

SAMPLE_RATE   = 48_000
CHANNELS      = 1
FRAME_MS      = 10
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 480

AUDIO_SAMPLES_DIR = Path(__file__).parent / "audio_samples"


# ── WAV helpers ───────────────────────────────────────────────────────────────

def _list_wav_files() -> list[Path]:
    """Return all .wav files in audio_samples/, or empty list if missing."""
    if not AUDIO_SAMPLES_DIR.exists():
        return []
    return sorted(AUDIO_SAMPLES_DIR.glob("*.wav"))


def _read_wav_as_pcm16_48k(path: Path) -> bytes:
    """
    Read a WAV file and return raw 16-bit PCM at 48kHz mono.
    Resamples if source rate differs (simple nearest-neighbour — good enough
    for load testing).
    """
    with wave.open(str(path), "rb") as wf:
        src_rate     = wf.getframerate()
        src_channels = wf.getnchannels()
        src_width    = wf.getsampwidth()   # bytes per sample
        raw          = wf.readframes(wf.getnframes())

    # Decode to int16
    if src_width == 2:
        n = len(raw) // 2
        samples = list(struct.unpack(f"<{n}h", raw))
    elif src_width == 3:  # 24-bit
        samples = []
        for i in range(0, len(raw), 3):
            v = int.from_bytes(raw[i:i+3], "little", signed=True)
            samples.append(v >> 8)   # downscale to 16-bit
    elif src_width == 4:  # 32-bit
        n = len(raw) // 4
        vals = struct.unpack(f"<{n}i", raw)
        samples = [v >> 16 for v in vals]
    else:
        raise ValueError(f"Unsupported sample width: {src_width}")

    # Mix down to mono if stereo
    if src_channels == 2:
        samples = [(samples[i] + samples[i+1]) // 2 for i in range(0, len(samples)-1, 2)]
    elif src_channels > 2:
        samples = samples[::src_channels]

    # Resample to 48kHz (nearest neighbour)
    if src_rate != SAMPLE_RATE:
        ratio = SAMPLE_RATE / src_rate
        new_len = int(len(samples) * ratio)
        samples = [samples[min(int(i / ratio), len(samples)-1)] for i in range(new_len)]

    return struct.pack(f"<{len(samples)}h", *samples)


# ── Public streaming functions ────────────────────────────────────────────────

async def stream_wav_to_source(audio_source, duration_s: float = 60.0,
                                wav_path: Optional[Path] = None):
    """
    Stream a real speech WAV into a LiveKit AudioSource.

    Picks a random WAV from load_tests/audio_samples/ (or uses wav_path).
    Loops the audio if it's shorter than duration_s.
    Falls back to synthetic speech if no WAVs are available.
    """
    if not HAS_LIVEKIT:
        raise RuntimeError("livekit.rtc not available")

    wav_files = _list_wav_files()
    if not wav_files:
        # No samples generated yet — fall back to synthetic
        await stream_speech_like_to_source(audio_source, duration_s)
        return

    chosen = wav_path or random.choice(wav_files)
    pcm_data = _read_wav_as_pcm16_48k(chosen)

    frame_duration_s = FRAME_MS / 1000.0
    total_frames     = int(duration_s / frame_duration_s)
    samples_per_frame = FRAME_SAMPLES
    total_pcm_samples = len(pcm_data) // 2   # 2 bytes per int16

    for frame_idx in range(total_frames):
        start = (frame_idx * samples_per_frame) % total_pcm_samples
        end   = start + samples_per_frame

        if end <= total_pcm_samples:
            frame_bytes = pcm_data[start*2 : end*2]
        else:
            # Wrap around
            tail = pcm_data[start*2:]
            head = pcm_data[: (end - total_pcm_samples)*2]
            frame_bytes = tail + head

        # Pad if somehow short
        if len(frame_bytes) < samples_per_frame * 2:
            frame_bytes = frame_bytes.ljust(samples_per_frame * 2, b"\x00")

        frame = AudioFrame(
            data=frame_bytes,
            sample_rate=SAMPLE_RATE,
            num_channels=CHANNELS,
            samples_per_channel=samples_per_frame,
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(frame_duration_s)


# ── Legacy / fallback ─────────────────────────────────────────────────────────

def silence_bytes(duration_ms: int = FRAME_MS) -> bytes:
    n_samples = SAMPLE_RATE * duration_ms // 1000
    return b"\x00\x00" * n_samples


def speech_like_bytes(duration_ms: int = FRAME_MS) -> bytes:
    n_samples = SAMPLE_RATE * duration_ms // 1000
    freqs = [200, 400, 800, 1600, 2400, 3200]
    amps  = [0.15, 0.20, 0.18, 0.12, 0.08, 0.05]
    samples = []
    for i in range(n_samples):
        t   = i / SAMPLE_RATE
        val = sum(a * math.sin(2 * math.pi * f * t) for f, a in zip(freqs, amps))
        val = int(val * 32767)
        samples.append(max(-32768, min(32767, val)))
    return struct.pack(f"<{n_samples}h", *samples)


async def stream_speech_like_to_source(audio_source, duration_s: float = 30.0):
    """Fallback: push synthetic speech-like frames (won't transcribe in Deepgram)."""
    if not HAS_LIVEKIT:
        raise RuntimeError("livekit.rtc not available")

    frame_duration_s = FRAME_MS / 1000.0
    total_frames     = int(duration_s / frame_duration_s)
    n_samples        = FRAME_SAMPLES
    freqs = [200, 400, 800, 1600, 2400, 3200]
    amps  = [0.15, 0.20, 0.18, 0.12, 0.08, 0.05]

    for frame_idx in range(total_frames):
        pcm = []
        for i in range(n_samples):
            t   = (frame_idx * n_samples + i) / SAMPLE_RATE
            val = sum(a * math.sin(2 * math.pi * f * t) for f, a in zip(freqs, amps))
            pcm.append(max(-32768, min(32767, int(val * 32767))))

        data  = struct.pack(f"<{n_samples}h", *pcm)
        frame = AudioFrame(
            data=data, sample_rate=SAMPLE_RATE,
            num_channels=CHANNELS, samples_per_channel=n_samples,
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(frame_duration_s)


async def stream_sine_to_source(audio_source, duration_s: float = 30.0,
                                 freq_hz: float = 440.0):
    """Push sine-wave frames — mostly for unit tests."""
    if not HAS_LIVEKIT:
        raise RuntimeError("livekit.rtc not available")

    frame_duration_s = FRAME_MS / 1000.0
    total_frames     = int(duration_s / frame_duration_s)
    n_samples        = FRAME_SAMPLES

    for frame_idx in range(total_frames):
        pcm = []
        for i in range(n_samples):
            t   = (frame_idx * n_samples + i) / SAMPLE_RATE
            val = int(0.3 * 32767 * math.sin(2 * math.pi * freq_hz * t))
            pcm.append(max(-32768, min(32767, val)))
        data  = struct.pack(f"<{n_samples}h", *pcm)
        frame = AudioFrame(
            data=data, sample_rate=SAMPLE_RATE,
            num_channels=CHANNELS, samples_per_channel=n_samples,
        )
        await audio_source.capture_frame(frame)
        await asyncio.sleep(frame_duration_s)
