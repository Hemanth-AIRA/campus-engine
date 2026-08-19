"""
test_01_deepgram.py  —  Deepgram concurrent WebSocket connection test.

Tests N simultaneous Deepgram streaming sessions by:
  1. Opening N WebSocket connections
  2. Streaming synthetic PCM audio for `stream_s` seconds
  3. Measuring: connect time, TTFA (time to first transcript), errors

Run:
    python -m load_tests.test_01_deepgram --n 20 --stream-s 10
"""
import asyncio, argparse, logging, time
import websockets

from .config  import DEEPGRAM_API_KEY, DEEPGRAM_WS_URL, REPORTS_DIR, cleanup_previous_outputs
from .metrics import MetricCollector
from .audio_gen import speech_like_bytes, FRAME_MS

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [DG-TEST] %(message)s")
logger = logging.getLogger("test_deepgram")


async def one_deepgram_session(session_id: int, stream_s: float,
                               col_connect: MetricCollector,
                               col_transcript: MetricCollector):
    label = f"session-{session_id:03d}"
    ws_url = DEEPGRAM_WS_URL
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}
    transcript_received = asyncio.Event()
    first_transcript_ms = None

    t_connect_start = time.perf_counter()
    try:
        async with websockets.connect(ws_url, additional_headers=headers,
                                      open_timeout=15) as ws:
            connect_ms = (time.perf_counter() - t_connect_start) * 1000
            col_connect.record(label, connect_ms, True)
            logger.info("[%s] Connected in %.0fms", label, connect_ms)

            t_stream_start = time.perf_counter()

            async def send_audio():
                frame_s = FRAME_MS / 1000.0
                n_frames = int(stream_s / frame_s)
                for _ in range(n_frames):
                    await ws.send(speech_like_bytes())
                    await asyncio.sleep(frame_s)
                # Send close stream signal
                await ws.send(json_close := '{"type":"CloseStream"}')

            async def recv_transcripts():
                nonlocal first_transcript_ms
                import json
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if (data.get("type") == "Results"
                                and data.get("channel", {})
                                        .get("alternatives", [{}])[0]
                                        .get("transcript", "")):
                            if first_transcript_ms is None:
                                first_transcript_ms = (time.perf_counter() - t_stream_start) * 1000
                                col_transcript.record(label, first_transcript_ms, True)
                                logger.info("[%s] First transcript in %.0fms",
                                            label, first_transcript_ms)
                                transcript_received.set()
                    except Exception:
                        pass

            send_task = asyncio.create_task(send_audio())
            recv_task = asyncio.create_task(recv_transcripts())
            await send_task
            # Give a moment for final transcripts
            await asyncio.sleep(1)
            recv_task.cancel()

            if first_transcript_ms is None:
                # No transcript received — record as latency=stream_s (timeout)
                col_transcript.record(label, stream_s * 1000, False,
                                      "No transcript received")
                logger.warning("[%s] No transcript received", label)

    except Exception as e:
        connect_ms = (time.perf_counter() - t_connect_start) * 1000
        col_connect.record(label, connect_ms, False, str(e))
        col_transcript.record(label, connect_ms, False, f"WS error: {e}")
        logger.error("[%s] ❌ %s", label, e)


async def run(n: int, stream_s: float):
    col_connect    = MetricCollector(f"Deepgram WS Connect (n={n})")
    col_transcript = MetricCollector(f"Deepgram First Transcript (n={n})")

    logger.info("Starting %d concurrent Deepgram sessions (stream=%.0fs each)…", n, stream_s)
    t0 = time.perf_counter()

    tasks = [
        one_deepgram_session(i, stream_s, col_connect, col_transcript)
        for i in range(n)
    ]
    await asyncio.gather(*tasks)

    elapsed = time.perf_counter() - t0
    logger.info("All sessions done in %.1fs", elapsed)

    col_connect.print_summary()
    col_transcript.print_summary()
    p1 = col_connect.save_report(REPORTS_DIR)
    p2 = col_transcript.save_report(REPORTS_DIR)
    logger.info("Reports saved:\n  %s\n  %s", p1, p2)
    return col_connect.summary(), col_transcript.summary()


def main():
    ap = argparse.ArgumentParser(description="Deepgram concurrency test")
    cleanup_previous_outputs(__file__)

    ap.add_argument("--n",        type=int,   default=10, help="Concurrent sessions")
    ap.add_argument("--stream-s", type=float, default=10, help="Seconds of audio per session")
    args = ap.parse_args()
    asyncio.run(run(args.n, args.stream_s))


if __name__ == "__main__":
    main()
