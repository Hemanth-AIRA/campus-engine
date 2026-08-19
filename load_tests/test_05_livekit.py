"""
test_05_livekit.py  —  LiveKit concurrent room join test.

Tests N simultaneous candidate joins by:
  1. Creating N rooms on the LiveKit SFU (via the Connect server API)
  2. Connecting N Python clients simultaneously
  3. Measuring: token fetch time, connect time, track publish time

Run:
    python -m load_tests.test_05_livekit --n 20 --stay-s 10
"""
import asyncio, argparse, logging, time, uuid

import httpx
from livekit import rtc

from .config   import LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, \
                      CONNECT_BASE_URL, REPORTS_DIR
from .metrics  import MetricCollector
from .audio_gen import stream_speech_like_to_source

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [LK-TEST] %(message)s")
logger = logging.getLogger("test_livekit")


async def fetch_token(room_id: str, name: str, role: str = "candidate") -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{CONNECT_BASE_URL}/api/livekit-token",
            json={"roomId": room_id, "identity": f"{role}-{room_id}-lt",
                  "role": role, "name": name}
        )
        r.raise_for_status()
        return r.json()


def make_room_id():
    return "LT-" + uuid.uuid4().hex[:8].upper()


async def one_livekit_session(session_id: int, stay_s: float,
                              col_token: MetricCollector,
                              col_connect: MetricCollector,
                              col_track: MetricCollector):
    label   = f"session-{session_id:03d}"
    room_id = make_room_id()

    # ── Token fetch ────────────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        tok = await fetch_token(room_id, f"Candidate-{session_id}")
        ms  = (time.perf_counter() - t0) * 1000
        col_token.record(label, ms, True)
        logger.info("[%s] Token: %.0fms", label, ms)
        lk_url, token = tok["url"], tok["token"]
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        col_token.record(label, ms, False, str(e))
        logger.error("[%s] Token error: %s", label, e)
        return

    # ── LiveKit connect ────────────────────────────────────────────────
    room = rtc.Room()
    t0   = time.perf_counter()
    try:
        await room.connect(lk_url, token)
        ms  = (time.perf_counter() - t0) * 1000
        col_connect.record(label, ms, True)
        logger.info("[%s] Connect: %.0fms", label, ms)
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        col_connect.record(label, ms, False, str(e))
        logger.error("[%s] Connect error: %s", label, e)
        return

    # ── Publish audio track ────────────────────────────────────────────
    t0 = time.perf_counter()
    try:
        from .audio_gen import SAMPLE_RATE, CHANNELS
        src   = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
        track = rtc.LocalAudioTrack.create_audio_track("mic", src)
        await room.local_participant.publish_track(track)
        ms = (time.perf_counter() - t0) * 1000
        col_track.record(label, ms, True)
        logger.info("[%s] Track publish: %.0fms", label, ms)

        # Stay in room and stream audio
        await stream_speech_like_to_source(src, duration_s=stay_s)
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        col_track.record(label, ms, False, str(e))
        logger.error("[%s] Track error: %s", label, e)
    finally:
        await room.disconnect()
        logger.info("[%s] Disconnected", label)


async def run(n: int, stay_s: float):
    col_token   = MetricCollector(f"LiveKit Token Fetch (n={n})")
    col_connect = MetricCollector(f"LiveKit Room Connect (n={n})")
    col_track   = MetricCollector(f"LiveKit Track Publish (n={n})")

    logger.info("Launching %d concurrent LiveKit sessions (stay=%.0fs)…", n, stay_s)
    t0 = time.perf_counter()

    await asyncio.gather(*[
        one_livekit_session(i, stay_s, col_token, col_connect, col_track)
        for i in range(n)
    ])

    logger.info("All done in %.1fs", time.perf_counter() - t0)
    for col in [col_token, col_connect, col_track]:
        col.print_summary()
        col.save_report(REPORTS_DIR)

    return col_token.summary(), col_connect.summary(), col_track.summary()


def main():
    ap = argparse.ArgumentParser(description="LiveKit concurrency test")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--n",      type=int,   default=10, help="Concurrent sessions")
    ap.add_argument("--stay-s", type=float, default=10, help="Seconds to stay in room")
    args = ap.parse_args()
    asyncio.run(run(args.n, args.stay_s))


if __name__ == "__main__":
    main()
