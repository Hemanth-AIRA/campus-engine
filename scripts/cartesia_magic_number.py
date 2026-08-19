#!/usr/bin/env python3
"""
cartesia_magic_number.py — Find the maximum concurrent Cartesia streams.

Fires N simultaneous Cartesia WebSocket TTS requests with the same test
sentence and plays each stream through the local speakers.
Measures TTFA, total duration, and whether each stream succeeded or failed.

Usage:
    python cartesia_magic_number.py          # prompts for N
    python cartesia_magic_number.py 5        # test 5 concurrent streams
    python cartesia_magic_number.py 10 --no-audio  # test without playback
"""

import asyncio
import base64
import os
import sys
import time
import uuid
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config from .env (same as cartesia_tts.py) ───────────────────────────────
CARTESIA_API_KEY   = os.environ.get("CARTESIA_API_KEY", "")
CARTESIA_VOICE_ID  = os.environ.get("CARTESIA_VOICE_ID", "b7d50908-b17c-442d-ad8d-810c63997ed9")
CARTESIA_MODEL     = os.environ.get("CARTESIA_MODEL", "sonic-2")
CARTESIA_SAMPLE_RATE = 24_000
CARTESIA_SPEED     = os.environ.get("CARTESIA_SPEED", "slow")

TEST_TEXT = (
    "Hi Aira, this is Hemanth. How are you doing today? "
    "I hope you are doing well. Let's get this interview started!"
)

_OUTPUT_FORMAT = {
    "container":   "raw",
    "encoding":    "pcm_s16le",
    "sample_rate": CARTESIA_SAMPLE_RATE,
}

def _voice_spec():
    spec = {"mode": "id", "id": CARTESIA_VOICE_ID}
    if CARTESIA_SPEED and CARTESIA_SPEED != "normal":
        spec["__experimental_controls"] = {"speed": CARTESIA_SPEED}
    return spec


# ── Result dataclass ──────────────────────────────────────────────────────────
class StreamResult:
    def __init__(self, idx):
        self.idx        = idx
        self.ttfa_ms    = None   # time to first audio
        self.total_ms   = None   # total wall-clock duration
        self.bytes_recv = 0      # total PCM bytes received
        self.success    = False
        self.error      = None
        self.started_at = None


# ── Single stream task ────────────────────────────────────────────────────────
async def run_one_stream(idx: int, play_audio: bool, result: StreamResult):
    """
    Open one Cartesia WS, stream the test text, collect PCM chunks.
    Optionally plays audio via sounddevice.
    Mirrors the exact logic in cartesia_tts.py (cartesia_speak_text_streaming).
    """
    from cartesia import AsyncCartesia

    result.started_at = time.time()
    t0 = result.started_at
    first_audio = False
    pcm_chunks = []

    # ── Audio output setup ────────────────────────────────────────────────────
    stream = None
    if play_audio:
        try:
            import sounddevice as sd
            stream = sd.RawOutputStream(
                samplerate=CARTESIA_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=1024,
            )
            stream.start()
        except Exception as e:
            logger.warning("[%02d] sounddevice unavailable: %s — buffering only", idx, e)
            stream = None

    try:
        client = AsyncCartesia(api_key=CARTESIA_API_KEY)
        voice  = _voice_spec()

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                async with client.tts.websocket_connect() as ws:
                    ctx = ws.context(
                        context_id=str(uuid.uuid4()),
                        model_id=CARTESIA_MODEL,
                        voice=voice,
                        output_format=_OUTPUT_FORMAT,
                    )

                    await ctx.send(
                        model_id=CARTESIA_MODEL,
                        transcript=TEST_TEXT,
                        voice=voice,
                        output_format=_OUTPUT_FORMAT,
                        continue_=False,
                    )

                    async for event in ctx.receive():
                        if event.type == "done":
                            break
                        if event.type == "error":
                            raise RuntimeError(f"Cartesia error event: {event}")

                        raw = getattr(event, "data", None)
                        if not raw:
                            continue

                        pcm_chunk = base64.b64decode(raw)
                        result.bytes_recv += len(pcm_chunk)

                        if not first_audio:
                            first_audio = True
                            result.ttfa_ms = (time.time() - t0) * 1000
                            logger.info("⚡ [%02d] TTFA: %.0f ms", idx, result.ttfa_ms)

                        pcm_chunks.append(pcm_chunk)

                        # Play chunk immediately if stream is open
                        if stream:
                            try:
                                stream.write(pcm_chunk)
                            except Exception:
                                pass

                break  # success

            except (asyncio.TimeoutError, TimeoutError, OSError) as ws_err:
                if attempt < max_retries:
                    logger.warning(
                        "⚠️  [%02d] WS attempt %d/%d failed: %s — retrying in 1s",
                        idx, attempt, max_retries, ws_err,
                    )
                    await asyncio.sleep(1)
                else:
                    raise

        result.success   = True
        result.total_ms  = (time.time() - t0) * 1000
        logger.info(
            "✅ [%02d] Done: %.0f ms total | %.1f KB received",
            idx, result.total_ms, result.bytes_recv / 1024,
        )

    except Exception as e:
        result.error    = str(e)
        result.total_ms = (time.time() - t0) * 1000
        logger.error("❌ [%02d] FAILED (%.0f ms): %s", idx, result.total_ms, e)

    finally:
        if stream:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


# ── Main ──────────────────────────────────────────────────────────────────────
async def run_test(n: int, play_audio: bool):
    if not CARTESIA_API_KEY:
        print("❌ CARTESIA_API_KEY not set in .env")
        sys.exit(1)

    print(f"\n{'═'*65}")
    print(f"  🔬 CARTESIA CONCURRENCY TEST")
    print(f"{'─'*65}")
    print(f"  API Key    : {CARTESIA_API_KEY[:8]}{'*'*20}")
    print(f"  Voice ID   : {CARTESIA_VOICE_ID}")
    print(f"  Model      : {CARTESIA_MODEL}")
    print(f"  Speed      : {CARTESIA_SPEED}")
    print(f"  Concurrent : {n}")
    print(f"  Play audio : {'Yes (sounddevice)' if play_audio else 'No (measure only)'}")
    print(f"  Text       : \"{TEST_TEXT[:60]}...\"")
    print(f"{'═'*65}\n")

    results = [StreamResult(i + 1) for i in range(n)]

    # Fire all N streams simultaneously
    wall_start = time.time()
    await asyncio.gather(*[
        run_one_stream(r.idx, play_audio, r)
        for r in results
    ])
    wall_total = (time.time() - wall_start) * 1000

    # ── Print results table ───────────────────────────────────────────────────
    ok  = [r for r in results if r.success]
    bad = [r for r in results if not r.success]

    print(f"\n{'═'*65}")
    print(f"  RESULTS — {n} concurrent streams")
    print(f"{'─'*65}")
    print(f"  {'#':<4} {'TTFA':>8} {'Total':>8} {'KB':>7} {'Status'}")
    print(f"{'─'*65}")
    for r in results:
        ttfa  = f"{r.ttfa_ms:>7.0f}ms" if r.ttfa_ms else "       —"
        total = f"{r.total_ms:>7.0f}ms" if r.total_ms else "       —"
        kb    = f"{r.bytes_recv/1024:>6.1f}" if r.bytes_recv else "     —"
        status = "✅ OK" if r.success else f"❌ {r.error[:35] if r.error else 'FAIL'}"
        print(f"  {r.idx:<4} {ttfa:>9} {total:>9} {kb:>7} KB  {status}")
    print(f"{'─'*65}")

    if ok:
        avg_ttfa  = sum(r.ttfa_ms for r in ok if r.ttfa_ms) / len(ok)
        max_ttfa  = max(r.ttfa_ms for r in ok if r.ttfa_ms)
        avg_total = sum(r.total_ms for r in ok) / len(ok)
    else:
        avg_ttfa = max_ttfa = avg_total = 0

    print(f"\n  ✅ Succeeded  : {len(ok)}/{n}")
    print(f"  ❌ Failed     : {len(bad)}/{n}")
    print(f"  Avg TTFA     : {avg_ttfa:.0f} ms")
    print(f"  Max TTFA     : {max_ttfa:.0f} ms")
    print(f"  Avg Duration : {avg_total:.0f} ms")
    print(f"  Wall clock   : {wall_total:.0f} ms")
    print(f"{'═'*65}")

    # ── Magic number verdict ──────────────────────────────────────────────────
    print(f"\n  🎯 VERDICT for N={n}:")
    if len(bad) == 0 and avg_ttfa < 600:
        print(f"  ✅ GOOD — All {n} streams succeeded, avg TTFA {avg_ttfa:.0f}ms (< 600ms)")
        print(f"     → Try N={n+1} or N={n+2} next to find the ceiling.")
    elif len(bad) == 0 and avg_ttfa >= 600:
        print(f"  ⚠️  DEGRADED — All {n} succeeded but avg TTFA {avg_ttfa:.0f}ms is high (≥ 600ms)")
        print(f"     → This is near the limit. N={n-1} is likely your magic number.")
    elif len(bad) <= n * 0.1:  # < 10% failure
        print(f"  ⚠️  MARGINAL — {len(bad)} of {n} failed (<10%). Borderline.")
        print(f"     → Magic number is likely N={n-1}.")
    else:
        print(f"  ❌ OVERLOADED — {len(bad)} of {n} failed. Magic number is below N={n}.")
        print(f"     → Try N={max(1, n - n//3)} next.")
    print()


def main():
    play_audio = "--no-audio" not in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    if args:
        try:
            n = int(args[0])
        except ValueError:
            print("❌ Usage: python cartesia_magic_number.py [N] [--no-audio]")
            sys.exit(1)
    else:
        try:
            raw = input("How many concurrent streams to test? ").strip()
            n = int(raw)
        except (ValueError, KeyboardInterrupt):
            print("\n❌ Invalid number.")
            sys.exit(1)

    if n <= 0:
        print("❌ Must be at least 1.")
        sys.exit(1)

    asyncio.run(run_test(n, play_audio))


if __name__ == "__main__":
    main()
