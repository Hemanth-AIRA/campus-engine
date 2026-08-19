#!/usr/bin/env python3
"""
cartesia_capacity_test.py
─────────────────────────
Find Cartesia's safe sustained-concurrency limit using the EXACT production
code path that AIRA uses — no synthetic clients, no HTTP benchmarks.

Two modes (mirrors production):
  --mode text    cartesia_speak_text_streaming()   [default]
  --mode tokens  cartesia_speak_tokens()           GPT-token streaming path

Two launch strategies:
  --burst        All N streams fire simultaneously (measures burst capacity)
  --stagger N    Spread launches over N*STAGGER_MS ms (measures sustained capacity) [default]

Usage:
    python cartesia_capacity_test.py 10
    python cartesia_capacity_test.py 50 --burst
    python cartesia_capacity_test.py 100 --csv results.csv
    python cartesia_capacity_test.py --ramp --max 500
    python cartesia_capacity_test.py --ramp --start 1 --step 10 --max 1000 --csv out.csv
    python cartesia_capacity_test.py 50 --mode tokens
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))


import asyncio
import csv
import os
import sys
import time
import logging
import argparse
from dataclasses import dataclass
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("capacity_test")
logger.setLevel(logging.INFO)

# ── Import the production module — use ITS constants & functions ───────────────
import cartesia_tts

TEST_TEXT = (
    "Hi AIRA, this is Hemanth. How are you doing today? "
    "I hope you're doing well."
)

# Fake GPT token stream — mirrors how reflex_engine feeds cartesia_speak_tokens()
TEST_TOKENS = TEST_TEXT.split(" ")   # word-by-word, same as production

# ── Thresholds ────────────────────────────────────────────────────────────────
TTFA_SPIKE_MS       = 1000   # P95 TTFA above this → DEGRADED
FAILURE_THRESHOLD   = 0.05   # >5% of any failures → OVERLOADED
LAUNCH_STAGGER_MS   = 25     # ms between each stream launch (sustained mode)


# ─────────────────────────────────────────────────────────────────────────────
# Minimal bot stub — exact mirror of AiraBotClient stream interface
# ─────────────────────────────────────────────────────────────────────────────
class _LocalBotStub:
    """
    Drop-in for AiraBotClient. Provides start/stop_stream_to_livekit()
    with the same queue+drain pattern as production — just no LiveKit frame.
    """
    PLAYBACK_CHANNELS = 1
    FRAME_MS          = 20

    def __init__(self):
        self._stream_buf:  Optional[asyncio.Queue] = None
        self._stream_task: Optional[asyncio.Task]  = None
        self.bytes_received: int = 0

    async def start_stream_to_livekit(self, sample_rate: int = 24_000) -> asyncio.Queue:
        frame_bytes = int(sample_rate * 2 * self.PLAYBACK_CHANNELS * (self.FRAME_MS / 1000))
        self._stream_buf  = asyncio.Queue()
        self.bytes_received = 0

        async def _drain_loop():
            buf = bytearray()
            while True:
                while True:
                    try:
                        chunk = self._stream_buf.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if chunk is None:
                        while len(buf) >= frame_bytes:
                            del buf[:frame_bytes]
                            await asyncio.sleep(0.018)
                        return
                    self.bytes_received += len(chunk)
                    buf.extend(chunk)

                if len(buf) >= frame_bytes:
                    del buf[:frame_bytes]
                    await asyncio.sleep(0.018)
                else:
                    await asyncio.sleep(0.002)

        self._stream_task = asyncio.create_task(_drain_loop())
        return self._stream_buf

    async def stop_stream_to_livekit(self) -> None:
        if self._stream_buf is not None:
            await self._stream_buf.put(None)
        if self._stream_task is not None:
            try:
                await asyncio.wait_for(self._stream_task, timeout=30)
            except asyncio.TimeoutError:
                self._stream_task.cancel()
        self._stream_buf  = None
        self._stream_task = None


# ─────────────────────────────────────────────────────────────────────────────
# Per-stream result
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class StreamResult:
    stream_id:        int
    start_time:       float = 0.0
    first_audio_time: Optional[float] = None
    completion_time:  Optional[float] = None
    ttfa_ms:          Optional[float] = None
    duration_ms:      Optional[float] = None
    bytes_received:   int   = 0
    success:          bool  = False
    is_429:           bool  = False   # Rate-limited by Cartesia
    is_ws_fail:       bool  = False   # WebSocket connection failure
    error:            Optional[str] = None


def _classify_error(e: Exception) -> tuple[bool, bool]:
    """Return (is_429, is_ws_fail)."""
    msg = str(e).lower()
    is_429    = "429" in msg or "rate limit" in msg or "too many" in msg
    is_ws_fail = any(k in msg for k in ("websocket", "connect", "handshake", "timeout", "oserror"))
    return is_429, is_ws_fail


# ─────────────────────────────────────────────────────────────────────────────
# Single stream — text mode (production path #1)
# ─────────────────────────────────────────────────────────────────────────────
async def _run_text(stream_id: int, stagger: bool) -> StreamResult:
    result = StreamResult(stream_id=stream_id)

    if stagger:
        await asyncio.sleep(stream_id * LAUNCH_STAGGER_MS / 1000.0)

    result.start_time = time.time()
    stub = _LocalBotStub()
    first_audio_fired = False

    def _on_first_audio():
        nonlocal first_audio_fired
        if not first_audio_fired:
            first_audio_fired = True
            result.first_audio_time = time.time()
            result.ttfa_ms = (result.first_audio_time - result.start_time) * 1000

    try:
        # ── EXACT PRODUCTION PATH ────────────────────────────────────────────
        await cartesia_tts.cartesia_speak_text_streaming(
            TEST_TEXT, stub, on_first_audio=_on_first_audio,
        )
        result.completion_time = time.time()
        result.duration_ms     = (result.completion_time - result.start_time) * 1000
        result.bytes_received  = stub.bytes_received
        result.success         = True

    except Exception as e:
        result.error     = str(e)
        result.duration_ms = (time.time() - result.start_time) * 1000
        result.is_429, result.is_ws_fail = _classify_error(e)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Single stream — token mode (production path #2)
# ─────────────────────────────────────────────────────────────────────────────
async def _run_tokens(stream_id: int, stagger: bool) -> StreamResult:
    result = StreamResult(stream_id=stream_id)

    if stagger:
        await asyncio.sleep(stream_id * LAUNCH_STAGGER_MS / 1000.0)

    result.start_time = time.time()
    stub = _LocalBotStub()
    first_audio_fired = False

    def _on_first_audio():
        nonlocal first_audio_fired
        if not first_audio_fired:
            first_audio_fired = True
            result.first_audio_time = time.time()
            result.ttfa_ms = (result.first_audio_time - result.start_time) * 1000

    # Sync iterator of word tokens — mirrors how GPT streaming feeds production
    def _token_iter():
        for word in TEST_TOKENS:
            yield word + " "

    try:
        # ── EXACT PRODUCTION PATH (token streaming) ──────────────────────────
        await cartesia_tts.cartesia_speak_tokens(
            _token_iter(), stub, on_first_audio=_on_first_audio,
        )
        result.completion_time = time.time()
        result.duration_ms     = (result.completion_time - result.start_time) * 1000
        result.bytes_received  = stub.bytes_received
        result.success         = True

    except Exception as e:
        result.error       = str(e)
        result.duration_ms = (time.time() - result.start_time) * 1000
        result.is_429, result.is_ws_fail = _classify_error(e)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Run N concurrent streams
# ─────────────────────────────────────────────────────────────────────────────
async def run_batch(n: int, mode: str, stagger: bool) -> List[StreamResult]:
    strategy = f"staggered {n * LAUNCH_STAGGER_MS / 1000:.1f}s spread" if stagger else "burst"
    logger.info("🚀 Firing %d streams (%s, mode=%s)…", n, strategy, mode)

    runner = _run_text if mode == "text" else _run_tokens
    results = await asyncio.gather(*[runner(i + 1, stagger) for i in range(n)])
    return list(results)


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────
def _pct(values: list, p: float) -> float:
    if not values:
        return 0.0
    sv  = sorted(values)
    idx = (len(sv) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (idx - lo)


@dataclass
class BatchStats:
    n:            int
    mode:         str
    stagger:      bool
    success:      int
    failures:     int
    count_429:    int
    count_ws:     int
    p50_ttfa:     float
    p95_ttfa:     float
    p99_ttfa:     float
    avg_duration: float
    failure_rate: float
    degraded:     bool   # TTFA spike
    overloaded:   bool   # failure rate exceeded
    rate_limited: bool   # any 429s seen


def compute_stats(n: int, mode: str, stagger: bool,
                  results: List[StreamResult]) -> BatchStats:
    ok  = [r for r in results if r.success]
    bad = [r for r in results if not r.success]

    ttfa_vals = [r.ttfa_ms for r in ok if r.ttfa_ms is not None]
    dur_vals  = [r.duration_ms for r in ok if r.duration_ms is not None]

    p50 = _pct(ttfa_vals, 50)
    p95 = _pct(ttfa_vals, 95)
    p99 = _pct(ttfa_vals, 99)
    avg = sum(dur_vals) / len(dur_vals) if dur_vals else 0.0

    c429   = sum(1 for r in bad if r.is_429)
    cws    = sum(1 for r in bad if r.is_ws_fail)
    frate  = len(bad) / n if n else 0

    return BatchStats(
        n=n, mode=mode, stagger=stagger,
        success=len(ok), failures=len(bad),
        count_429=c429, count_ws=cws,
        p50_ttfa=p50, p95_ttfa=p95, p99_ttfa=p99,
        avg_duration=avg,
        failure_rate=frate,
        degraded     = p95 > TTFA_SPIKE_MS,
        overloaded   = frate > FAILURE_THRESHOLD,
        rate_limited = c429 > 0,
    )


def _verdict_line(s: BatchStats) -> str:
    if s.overloaded and s.rate_limited:
        return f"❌ RATE-LIMITED — {s.count_429} × 429 errors"
    if s.overloaded:
        return f"❌ OVERLOADED   — {s.failure_rate*100:.1f}% failures"
    if s.degraded:
        return f"⚠️  DEGRADED    — P95 TTFA {s.p95_ttfa:.0f}ms (>{TTFA_SPIKE_MS}ms)"
    return f"✅ HEALTHY      — P95={s.p95_ttfa:.0f}ms  avg={s.avg_duration/1000:.1f}s"


def print_batch_summary(stats: BatchStats, results: List[StreamResult]):
    w = 65
    strategy = f"staggered ({stats.n * LAUNCH_STAGGER_MS}ms spread)" if stats.stagger else "burst"
    print(f"\n{'═'*w}")
    print(f"  Concurrent Streams : {stats.n}  [{stats.mode} mode / {strategy}]")
    print(f"{'─'*w}")
    print(f"  Success     : {stats.success}")
    print(f"  Failures    : {stats.failures}")
    if stats.count_429:
        print(f"  ⚠️  429s     : {stats.count_429}  ← rate-limited")
    if stats.count_ws:
        print(f"  WS errors   : {stats.count_ws}")

    if stats.success > 0:
        print(f"\n  TTFA P50    : {stats.p50_ttfa:.0f} ms")
        print(f"  TTFA P95    : {stats.p95_ttfa:.0f} ms")
        print(f"  TTFA P99    : {stats.p99_ttfa:.0f} ms")
        print(f"  Avg Complete: {stats.avg_duration/1000:.2f} s")

    bad = [r for r in results if not r.success]
    if bad:
        ec: dict = {}
        for r in bad:
            tag = "[429]" if r.is_429 else ("[WS]" if r.is_ws_fail else "[ERR]")
            key = f"{tag} {(r.error or 'unknown')[:60]}"
            ec[key] = ec.get(key, 0) + 1
        print(f"\n  Errors:")
        for err, cnt in sorted(ec.items(), key=lambda x: -x[1]):
            print(f"    [{cnt:>3}×] {err}")

    print(f"{'─'*w}")
    print(f"  {_verdict_line(stats)}")
    print(f"{'═'*w}\n")


# ─────────────────────────────────────────────────────────────────────────────
# Ramp mode
# ─────────────────────────────────────────────────────────────────────────────
RAMP_PRESETS = [1, 5, 10, 25, 50, 100, 200, 500, 1000]


async def run_ramp(start: int, step: int, max_n: int, mode: str, stagger: bool,
                   csv_path: Optional[str], pause_s: float = 3.0):

    if step:
        levels = list(range(start, max_n + 1, step))
        if max_n not in levels:
            levels.append(max_n)
    else:
        levels = [l for l in RAMP_PRESETS if start <= l <= max_n]
        if not levels:
            levels = [start]

    all_stats: List[BatchStats] = []
    safe_capacity = 0
    w = 60

    strategy = "staggered" if stagger else "burst"
    print(f"\n{'═'*w}")
    print(f"  🔬 RAMP TEST  [mode={mode}  launch={strategy}]")
    print(f"  Levels  : {levels}")
    print(f"  Stop on : P95 TTFA > {TTFA_SPIKE_MS}ms | failures > {FAILURE_THRESHOLD*100:.0f}% | 429s")
    print(f"{'═'*w}\n")

    for n in levels:
        print(f"  Testing N={n}…", end=" ", flush=True)
        results   = await run_batch(n, mode, stagger)
        stats     = compute_stats(n, mode, stagger, results)
        all_stats.append(stats)

        verdict = _verdict_line(stats)
        print(f"N={n:<6}  {verdict}")

        if not stats.overloaded and not stats.degraded and not stats.rate_limited:
            safe_capacity = n
        else:
            print(f"\n  ⛔ Stopping ramp — limit reached at N={n}")
            break

        if n < levels[-1]:
            await asyncio.sleep(pause_s)

    # ── Ramp summary table ────────────────────────────────────────────────────
    print(f"\n{'═'*w}")
    print(f"  RAMP SUMMARY  [{mode} / {strategy}]")
    print(f"{'─'*w}")
    print(f"  {'N':<7} {'OK':>5} {'FAIL':>5} {'429':>5} {'P50':>8} {'P95':>8} {'P99':>8} {'Avg':>6}")
    print(f"{'─'*w}")
    for s in all_stats:
        mark = "✅" if not s.degraded and not s.overloaded and not s.rate_limited \
               else ("❌" if s.overloaded or s.rate_limited else "⚠️ ")
        print(
            f"  {mark} {s.n:<5} {s.success:>5} {s.failures:>5} {s.count_429:>5} "
            f"{s.p50_ttfa:>7.0f}ms {s.p95_ttfa:>7.0f}ms {s.p99_ttfa:>7.0f}ms "
            f"{s.avg_duration/1000:>5.1f}s"
        )
    print(f"{'─'*w}")

    if safe_capacity:
        print(f"\n  🎯 SAFE CAPACITY = {safe_capacity} concurrent streams")
        print(f"     (P95 TTFA < {TTFA_SPIKE_MS}ms, 0 failures, 0 rate-limits)")
    else:
        print(f"\n  ❌ Even N=1 failed — check API key / network")
    print(f"{'═'*w}\n")

    if csv_path:
        _write_csv(all_stats, csv_path)

    return all_stats, safe_capacity


async def run_single(n: int, mode: str, stagger: bool,
                     csv_path: Optional[str]):
    results = await run_batch(n, mode, stagger)
    stats   = compute_stats(n, mode, stagger, results)
    print_batch_summary(stats, results)
    if csv_path:
        _write_csv([stats], csv_path)
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# CSV output
# ─────────────────────────────────────────────────────────────────────────────
def _write_csv(all_stats: List[BatchStats], path: str):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "concurrency", "mode", "stagger",
            "success", "failures", "count_429", "count_ws",
            "p50_ttfa_ms", "p95_ttfa_ms", "p99_ttfa_ms",
            "avg_duration_ms", "failure_rate_pct",
            "degraded", "overloaded", "rate_limited",
        ])
        for s in all_stats:
            writer.writerow([
                s.n, s.mode, s.stagger,
                s.success, s.failures, s.count_429, s.count_ws,
                round(s.p50_ttfa, 1), round(s.p95_ttfa, 1), round(s.p99_ttfa, 1),
                round(s.avg_duration, 1), round(s.failure_rate * 100, 2),
                s.degraded, s.overloaded, s.rate_limited,
            ])
    print(f"  📊 CSV → {path}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _build_parser():
    p = argparse.ArgumentParser(
        description="Cartesia sustained-concurrency capacity tester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cartesia_capacity_test.py 10
  python cartesia_capacity_test.py 50 --burst
  python cartesia_capacity_test.py 100 --mode tokens --csv tokens.csv
  python cartesia_capacity_test.py --ramp --max 500
  python cartesia_capacity_test.py --ramp --start 1 --step 10 --max 1000 --csv ramp.csv
        """,
    )
    p.add_argument("n",        nargs="?", type=int,   help="Number of concurrent streams")
    p.add_argument("--ramp",   action="store_true",   help="Auto-ramp through levels")
    p.add_argument("--start",  type=int, default=1,   help="Ramp start  (default 1)")
    p.add_argument("--step",   type=int, default=0,   help="Ramp step   (0=preset levels)")
    p.add_argument("--max",    type=int, default=1000,help="Ramp max    (default 1000)")
    p.add_argument("--csv",    type=str, default=None,help="CSV output path")
    p.add_argument("--pause",  type=float, default=3.0, help="Pause between ramp levels (s)")
    p.add_argument("--mode",   choices=["text", "tokens"], default="text",
                   help="text=speak_text_streaming  tokens=speak_tokens  (default: text)")
    p.add_argument("--burst",  action="store_true",
                   help="Burst mode: fire all streams simultaneously (default: staggered)")
    p.add_argument("--stagger-ms", type=int, default=LAUNCH_STAGGER_MS,
                   dest="stagger_ms", help=f"ms between launches in stagger mode (default {LAUNCH_STAGGER_MS})")
    return p


async def _main():
    args    = _build_parser().parse_args()
    stagger = not args.burst

    # Override stagger interval if user passed --stagger-ms
    global LAUNCH_STAGGER_MS
    LAUNCH_STAGGER_MS = args.stagger_ms

    if not cartesia_tts.CARTESIA_API_KEY:
        print("❌  CARTESIA_API_KEY not set in .env")
        sys.exit(1)

    strategy = f"staggered ({LAUNCH_STAGGER_MS}ms/stream)" if stagger else "burst"
    print(f"\n{'═'*65}")
    print(f"  🔬 CARTESIA CAPACITY TEST — production code path")
    print(f"{'─'*65}")
    print(f"  API Key   : {cartesia_tts.CARTESIA_API_KEY[:8]}{'*'*16}")
    print(f"  Voice     : {cartesia_tts.CARTESIA_VOICE_ID}")
    print(f"  Model     : {cartesia_tts.CARTESIA_MODEL}")
    print(f"  Speed     : {cartesia_tts.CARTESIA_SPEED}")
    print(f"  Mode      : {args.mode}")
    print(f"  Launch    : {strategy}")
    print(f"  Text      : \"{TEST_TEXT[:55]}…\"")
    print(f"{'═'*65}\n")

    if args.ramp or (args.n is None):
        if args.ramp:
            await run_ramp(args.start, args.step, args.max,
                           args.mode, stagger, args.csv, args.pause)
        else:
            try:
                raw = input("N streams (or 'ramp'): ").strip().lower()
            except KeyboardInterrupt:
                sys.exit(0)
            if raw == "ramp":
                await run_ramp(1, 0, 1000, args.mode, stagger, args.csv)
            else:
                try:
                    n = int(raw)
                except ValueError:
                    print("❌ Enter a number or 'ramp'")
                    sys.exit(1)
                await run_single(n, args.mode, stagger, args.csv)
    else:
        await run_single(args.n, args.mode, stagger, args.csv)


if __name__ == "__main__":
    asyncio.run(_main())
#dummy push 2