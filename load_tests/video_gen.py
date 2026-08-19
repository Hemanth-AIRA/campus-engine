"""
load_tests/video_gen.py
Stream a static JPEG face image as a LiveKit video track throughout the interview.

- Picks a face deterministically from load_tests/face_samples/ using interview_id % N
- Pushes ARGB frames at ~15 fps so LiveKit sees a valid video track
- Engine's face-detection and video analysis hooks will see a real face
"""
import asyncio, os, logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("video_gen")

FACE_SAMPLES_DIR = Path(__file__).parent / "face_samples"
TARGET_W = 640
TARGET_H = 480
FPS      = 15   # frames per second — enough for a webcam sim


def pick_face_path(interview_id: int) -> Optional[Path]:
    """Deterministically pick a face image based on interview_id."""
    faces = sorted(FACE_SAMPLES_DIR.glob("*.jpg")) + sorted(FACE_SAMPLES_DIR.glob("*.png"))
    if not faces:
        return None
    return faces[interview_id % len(faces)]


def _jpeg_to_argb(path: Path, width: int = TARGET_W, height: int = TARGET_H) -> bytes:
    """Load JPEG → resize → return raw ARGB bytes (4 bytes per pixel, alpha=255)."""
    from PIL import Image
    img = Image.open(path).convert("RGBA").resize((width, height), Image.LANCZOS)
    # LiveKit expects ARGB (not RGBA) — swap R and B channels
    r, g, b, a = img.split()
    argb_img = Image.merge("RGBA", (a, r, g, b))
    return argb_img.tobytes()


async def stream_face_video(video_source, interview_id: int, duration_s: float = 300.0):
    """
    Push static face image frames into a LiveKit VideoSource for duration_s seconds.

    Parameters
    ----------
    video_source : livekit.rtc.VideoSource
    interview_id : int — used to deterministically pick the face image
    duration_s   : float — how long to stream (matches audio_duration_s)
    """
    try:
        from livekit.rtc import VideoFrame, VideoBufferType
    except ImportError:
        logger.warning("livekit.rtc not available — skipping video stream")
        return

    face_path = pick_face_path(interview_id)
    if face_path is None:
        logger.warning("No face images found in %s — skipping video", FACE_SAMPLES_DIR)
        await asyncio.sleep(duration_s)
        return

    logger.info("🎥 Streaming face %s for %ss (interview_id=%d)", face_path.name, duration_s, interview_id)

    try:
        argb_bytes = _jpeg_to_argb(face_path)
    except Exception as e:
        logger.warning("Could not load face image %s: %s — skipping video", face_path, e)
        await asyncio.sleep(duration_s)
        return

    frame_interval = 1.0 / FPS
    total_frames   = int(duration_s * FPS)

    for _ in range(total_frames):
        frame = VideoFrame(
            width=TARGET_W,
            height=TARGET_H,
            type=VideoBufferType.ARGB,
            data=argb_bytes,
        )
        video_source.capture_frame(frame)
        await asyncio.sleep(frame_interval)

    logger.info("🎥 Video stream complete for interview_id=%d", interview_id)
