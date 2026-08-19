"""
livekit_egress.py

Server-side recording via LiveKit Egress API.

LiveKit Cloud has Egress built-in — no extra infrastructure needed.
A single API call starts recording the entire room (all participants)
to an MP4 file stored in LiveKit's managed S3 or your own bucket.

Usage (called from aira_3_webrtc_orchestration.py):

    from livekit_egress import start_room_recording, stop_room_recording

    # When interview starts:
    egress_id = await start_room_recording(room_id, interview_id)

    # When interview ends:
    file_path = await stop_room_recording(egress_id, room_id)

Storage options (set via .env):
    EGRESS_S3_BUCKET   = "aira-recordings"       # S3 bucket name
    EGRESS_S3_PREFIX   = "interviews/"            # key prefix
    EGRESS_S3_REGION   = "ap-south-1"            # bucket region
    AWS_ACCESS_KEY_ID  = ...
    AWS_SECRET_ACCESS_KEY = ...

    If S3 not configured → falls back to LiveKit-managed storage
    (recordings available via LiveKit Dashboard).
"""

import asyncio
import logging
import os
import time

logger = logging.getLogger("livekit_egress")

# ── Config ────────────────────────────────────────────────────────────────────
LIVEKIT_URL        = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY    = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")

# S3 storage (optional — leave blank to use LiveKit-managed storage)
EGRESS_S3_BUCKET   = os.environ.get("EGRESS_S3_BUCKET", "")
EGRESS_S3_PREFIX   = os.environ.get("EGRESS_S3_PREFIX", "interviews/")
EGRESS_S3_REGION   = os.environ.get("EGRESS_S3_REGION", "ap-south-1")
AWS_ACCESS_KEY_ID  = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")

# Local fallback path (if neither S3 nor managed storage configured)
RECORDINGS_DIR = os.environ.get("RECORDINGS_DIR", "recordings")


def _lk_http_url() -> str:
    """Convert wss:// LiveKit URL → https:// for the REST API."""
    url = LIVEKIT_URL
    if url.startswith("wss://"):
        url = "https://" + url[6:]
    elif url.startswith("ws://"):
        url = "http://" + url[5:]
    return url.rstrip("/")


def _build_output(room_id: str, interview_id) -> tuple:
    """
    Build the Egress output spec and return (s3_obj | None, filepath_hint).
    """
    filename = f"{room_id}_{interview_id}"

    if EGRESS_S3_BUCKET and AWS_ACCESS_KEY_ID:
        from livekit.protocol import egress as egress_proto
        s3 = egress_proto.S3Upload(
            access_key=AWS_ACCESS_KEY_ID,
            secret=AWS_SECRET_ACCESS_KEY,
            region=EGRESS_S3_REGION,
            bucket=EGRESS_S3_BUCKET,
        )
        key = EGRESS_S3_PREFIX + filename + ".mp4"
        logger.info("📦 [egress] Recording → s3://%s/%s", EGRESS_S3_BUCKET, key)
        return s3, f"s3://{EGRESS_S3_BUCKET}/{key}"
    else:
        os.makedirs(RECORDINGS_DIR, exist_ok=True)
        local_path = os.path.join(RECORDINGS_DIR, filename + ".mp4")
        logger.info("📦 [egress] Recording → %s (LiveKit managed)", filename)
        return None, local_path


async def start_room_recording(room_id: str, interview_id, process_id: int = 0) -> str | None:
    """
    Start a RoomComposite egress for the given room.

    Records ALL participants (bot voice + candidate voice + optional recruiter
    video) mixed into a single MP4.

    Returns egress_id (str) on success, None on failure.
    """
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        logger.warning("[egress] LIVEKIT_API_KEY/SECRET not set — skipping recording")
        return None

    try:
        from livekit.api import LiveKitAPI
        from livekit.api.egress_service import RoomCompositeEgressRequest
        from livekit.protocol import egress as egress_proto

        s3_output, path_hint = _build_output(room_id, interview_id)

        # Build file output spec
        # LiveKit Cloud requires cloud storage — local file paths are NOT supported.
        # Option A: Explicit S3 creds from env vars → include s3 in request.
        # Option B: Dashboard-managed storage → use file_outputs WITHOUT s3/gcp
        #           (LiveKit Cloud uses whatever you configured in Dashboard → Settings → Egress).
        file_output = egress_proto.EncodedFileOutput(
            file_type=egress_proto.EncodedFileType.MP4,
            filepath=f"{room_id}_{interview_id}",
        )
        if s3_output:
            file_output.s3.CopyFrom(s3_output)

        async with LiveKitAPI(
            url=_lk_http_url(),
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        ) as lk:
            req = RoomCompositeEgressRequest(
                room_name=room_id,
                layout="speaker",
                audio_only=False,
                file_outputs=[file_output],
            )
            resp = await lk.egress.start_room_composite_egress(req)
            egress_id = resp.egress_id
            logger.info(
                "🎬 [process id: %s][room: %s] Egress STARTED | egress_id=%s | output=%s",
                process_id, room_id, egress_id, path_hint,
            )
            return egress_id

    except Exception as e:
        logger.error(
            "❌ [process id: %s][room: %s] Failed to start egress: %s",
            process_id, room_id, e, exc_info=True,
        )
        return None


async def stop_room_recording(egress_id: str, room_id: str, process_id: int = 0) -> str | None:
    """
    Stop the egress and return the recording file path/URL.

    Returns the file path string on success, None on failure.
    """
    if not egress_id:
        return None

    try:
        from livekit.api import LiveKitAPI
        from livekit.api.egress_service import StopEgressRequest

        async with LiveKitAPI(
            url=_lk_http_url(),
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        ) as lk:
            resp = await lk.egress.stop_egress(
                StopEgressRequest(egress_id=egress_id)
            )
            file_path = None
            for out in (resp.file_results or []):
                file_path = getattr(out, 'download_url', None) or getattr(out, 'filename', None)
                break
            logger.info(
                "🎬 [process id: %s][room: %s] Egress STOPPED | egress_id=%s | file=%s",
                process_id, room_id, egress_id, file_path,
            )
            return file_path

    except Exception as e:
        logger.error(
            "❌ [process id: %s][room: %s] Failed to stop egress %s: %s",
            process_id, room_id, egress_id, e, exc_info=True,
        )
        return None


async def get_egress_status(egress_id: str) -> str:
    """Return current egress status string (ACTIVE, COMPLETE, FAILED, etc.)."""
    try:
        from livekit.api import LiveKitAPI
        from livekit.api.egress_service import ListEgressRequest

        async with LiveKitAPI(
            url=_lk_http_url(),
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        ) as lk:
            resp = await lk.egress.list_egress(
                ListEgressRequest(egress_id=egress_id)
            )
            if resp.items:
                status_map = {0: "STARTING", 1: "ACTIVE", 2: "ENDING", 3: "COMPLETE", 4: "FAILED", 5: "ABORTED", 6: "LIMIT_REACHED"}
                return status_map.get(resp.items[0].status, "UNKNOWN")
    except Exception as e:
        logger.error("❌ [egress] get_status failed: %s", e)
    return "UNKNOWN"


# ── Sync wrappers (for use from synchronous interview threads) ─────────────────

def start_recording_sync(room_id: str, interview_id, process_id: int = 0) -> str | None:
    """Synchronous wrapper — runs the coroutine in a new event loop."""
    try:
        return asyncio.run(start_room_recording(room_id, interview_id, process_id))
    except Exception as e:
        logger.error("❌ [egress] start_recording_sync failed: %s", e)
        return None


def stop_recording_sync(egress_id: str, room_id: str, process_id: int = 0) -> str | None:
    """Synchronous wrapper — runs the coroutine in a new event loop."""
    try:
        return asyncio.run(stop_room_recording(egress_id, room_id, process_id))
    except Exception as e:
        logger.error("❌ [egress] stop_recording_sync failed: %s", e)
        return None


# ── Quick test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    async def test():
        room = sys.argv[1] if len(sys.argv) > 1 else "TEST-ROOM-001"
        print(f"Testing egress for room: {room}")
        eid = await start_room_recording(room, interview_id=9999)
        if eid:
            print(f"✅ Egress started: {eid}")
            print("Waiting 5s...")
            await asyncio.sleep(5)
            path = await stop_room_recording(eid, room)
            print(f"✅ Egress stopped | file: {path}")
        else:
            print("❌ Failed to start egress")

    asyncio.run(test())

import httpx

async def wait_and_download_egress(egress_id: str, local_path: str, process_id: int = 0) -> str | None:
    """Poll egress status until complete, then download the file."""
    import asyncio
    from livekit.api import LiveKitAPI
    from livekit.api.egress_service import ListEgressRequest
    
    logger.info("⏳ [process id: %s] Waiting for egress %s to complete...", process_id, egress_id)
    download_url = None
    
    async with LiveKitAPI(
        url=_lk_http_url(),
        api_key=LIVEKIT_API_KEY,
        api_secret=LIVEKIT_API_SECRET,
    ) as lk:
        # Poll every 5 seconds for up to 10 minutes (120 iterations)
        for _ in range(120):
            try:
                resp = await lk.egress.list_egress(ListEgressRequest(egress_id=egress_id))
                if not resp.items:
                    break
                    
                info = resp.items[0]
                status = info.status
                # 3=COMPLETE, 4=FAILED, 5=ABORTED, 6=LIMIT_REACHED
                if status == 3: # COMPLETE
                    for out in (info.file_results or []):
                        download_url = getattr(out, 'location', None) or getattr(out, 'download_url', None)
                        if not download_url and getattr(out, 'filename', None):
                            # Sometimes filename is a URL
                            filename = getattr(out, 'filename', '')
                            if filename.startswith('http'):
                                download_url = filename
                        break
                    break
                elif status in [4, 5, 6]:
                    logger.error("❌ [process id: %s] Egress %s failed with status %s", process_id, egress_id, status)
                    return None
            except Exception as e:
                logger.warning("⚠️ [process id: %s] Polling error: %s", process_id, e)
                
            await asyncio.sleep(5)
            
    if not download_url:
        logger.error("❌ [process id: %s] Egress %s completed but no download URL found", process_id, egress_id)
        return None
        
    logger.info("📥 [process id: %s] Downloading %s to %s", process_id, download_url, local_path)
    try:
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", download_url) as resp:
                resp.raise_for_status()
                with open(local_path, 'wb') as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
        logger.info("✅ [process id: %s] Download complete: %s", process_id, local_path)
        return local_path
    except Exception as e:
        logger.error("❌ [process id: %s] Download failed: %s", process_id, e)
        return None

def wait_and_download_egress_sync(egress_id: str, local_path: str, process_id: int = 0) -> str | None:
    """Synchronous wrapper — runs the coroutine in a new event loop."""
    try:
        return asyncio.run(wait_and_download_egress(egress_id, local_path, process_id))
    except Exception as e:
        logger.error("❌ [egress] wait_and_download_egress_sync failed: %s", e)
        return None
