# AIRA Engine — Multiprocessing vs Threading
## Architecture Decision Record & Migration Plan

> **Status:** `multiprocessing.Pool` in production (stable).  
> Threading migration is planned but NOT started.  
> This document captures the full analysis, risks, and exact migration steps  
> so the switch can be made at any time with confidence.

---

## Table of Contents

1. [Why We Used `mp.Pool` (Historical Context)](#1-why-we-used-mppool-historical-context)
2. [Current Architecture (LiveKit Era)](#2-current-architecture-livekit-era)
3. [Multiprocessing vs Threading — Full Comparison](#3-multiprocessing-vs-threading--full-comparison)
4. [Resource Usage on a 16 GB RAM VM](#4-resource-usage-on-a-16-gb-ram-vm)
5. [Race Conditions Analysis](#5-race-conditions-analysis)
6. [WebSocket Thread Safety Deep Dive](#6-websocket-thread-safety-deep-dive)
7. [What Needs to Change to Switch](#7-what-needs-to-change-to-switch)
8. [Migration Plan (Step-by-Step)](#8-migration-plan-step-by-step)
9. [Testing Checklist Before Going Live](#9-testing-checklist-before-going-live)

---

## 1. Why We Used `mp.Pool` (Historical Context)

The original AIRA engine drove interviews using **Selenium + Chrome**:

```
Orchestrator
    └── mp.Pool worker (Interview A)
            └── selenium.webdriver.Chrome()   ← spawns a Chrome OS process
                    └── WebRTC audio via browser
```

Chrome is a **subprocess** — it literally cannot exist inside a Python thread.  
`selenium.webdriver` calls `subprocess.Popen()` internally, so process-level isolation
was not a design choice; it was a **hard requirement** of the old stack.

Additional reasons `mp.Pool` made sense then:
- Chrome + ChromeDriver = **200–500 MB per interview** → process isolation gave clean memory release on crash
- A crashed Chrome process took down its entire mp.Pool worker but left all others running
- Selenium WebDriver state was inherently process-scoped

---

## 2. Current Architecture (LiveKit Era)

Chrome is completely gone. The interview bot now uses:

```
Orchestrator (aira_3_webrtc_orchestration.py)
    └── mp.Pool worker (Interview A)         ← STILL multiprocessing (legacy)
            └── BotAudioAdapter              ← creates its own asyncio event loop
                    └── AiraBotClient        ← pure Python + Rust (livekit-rtc)
                            ├── rtc.Room     ← LiveKit SFU connection
                            ├── DG WebSocket ← Deepgram STT streaming
                            └── Cartesia WS  ← TTS streaming
```

Every component is now pure Python/asyncio with no subprocess dependency.  
`mp.Pool` is now **legacy overhead** — not a hard requirement.

### Current `mp.Pool` call site

```python
# aira_3_webrtc_orchestration.py  line 1365, 1414
AIRA_MAX_PARALLEL = 5

with mp.Pool(processes=procs) as pool:
    pool.starmap(orchestrate_interview, jobs_with_index)
```

---

## 3. Multiprocessing vs Threading — Full Comparison

### Conceptual model

```
MULTIPROCESSING (current)              THREADING (proposed)
─────────────────────────────          ────────────────────────────────
OS Process A  [Interview A]            Single OS Process
  Python interpreter copy                Python interpreter (shared)
  All modules reimported                 All modules imported once
  Heap: fully isolated                   Heap: shared (threads see same objects)
  Globals: per-process copy              Globals: SHARED — race conditions possible

OS Process B  [Interview B]            Thread A  [Interview A]
  Python interpreter copy                  BotAudioAdapter Loop A  (daemon thread)
  ...                                        DG WS, Cartesia WS, LiveKit Room A

OS Process C  [Interview C]            Thread B  [Interview B]
  ...                                    BotAudioAdapter Loop B  (daemon thread)
                                           DG WS, Cartesia WS, LiveKit Room B
```

### Direct comparison table

| Property | `mp.Pool` (current) | `ThreadPoolExecutor` (proposed) |
|----------|---------------------|----------------------------------|
| **Python interpreter copies** | N (one per interview) | 1 (shared) |
| **Base memory per interview** | ~70 MB (interpreter copy) | ~0 MB (shared interpreter) |
| **Interview data memory** | ~8–60 MB | ~8–60 MB (same) |
| **Total RAM — 20 interviews** | ~2.6 GB | ~230 MB (**11× less**) |
| **Startup time per interview** | 1–3 s (fork + reimport all modules) | ~1 ms (new thread) |
| **Crash isolation** | ✅ Full — one crash = one dead process | ⚠️ Unhandled exception kills whole process (mitigatable with `try/except`) |
| **GIL impact** | None (separate processes) | Negligible — interviews are I/O bound; GIL released during I/O and Rust audio |
| **Module-level globals** | ✅ Safe — per-process copy | 🔴 SHARED — race conditions on `_livekit_adapter`, `ACCESS_TOKEN` |
| **DB connections** | N connections (one per process) | 1 connection pool shared → fewer DB connections |
| **IPC between interviews** | ❌ Hard (pickle, pipes) | ✅ Easy (shared memory, queues) |
| **Debuggability** | Hard (separate PIDs) | Easy (single process, single debugger) |
| **`AIRA_MAX_PARALLEL` headroom on 16 GB** | ~50–60 before OOM | **500+** before OOM |

### Why threading wins for the LiveKit stack

Interviews are **almost entirely I/O-bound**:
- Waiting on Deepgram WebSocket response
- Waiting on OpenAI GPT streaming response
- Waiting on Cartesia audio chunks
- Waiting on LiveKit SFU audio frames

Python threads release the GIL during any `await` / `socket.recv()` / `socket.send()` call.  
livekit-rtc (Rust/tokio) also releases the GIL during audio processing.  
Result: threads are effectively parallel for this workload — same as processes but without the memory overhead.

---

## 4. Resource Usage on a 16 GB RAM VM

### Per-interview resource breakdown

| Resource | Amount | Notes |
|----------|--------|-------|
| Python interpreter base | ~70 MB (mp) / ~0 MB (threads) | Shared in thread model |
| `rtc.Room` + audio track | ~3 MB | LiveKit internal state |
| `_incoming_buffer` peak | up to ~48 MB | 60s × 96 KB/s at 48 kHz; only during recording |
| `_incoming_buffer` idle | ~2 MB | Between questions |
| GPT context (10 Qs) | ~3–5 MB | Grows per question |
| DG WebSocket per question | ~50 KB | Short-lived per question |
| Cartesia WS per utterance | ~50 KB | Short-lived per TTS call |
| BotAudioAdapter OS thread | ~1 MB | Thread stack |
| Misc (logging, SQL result) | ~5 MB | |
| **Peak per interview** | **~131 MB (mp) / ~61 MB (threads)** | |
| **Idle per interview** | **~80 MB (mp) / ~10 MB (threads)** | |

### Capacity on 16 GB

```
System RAM:                  16,384 MB
OS + kernel:                  ~1,000 MB
Orchestrator process:           ~200 MB
LiveKit SFU (self-hosted):      ~500 MB
Connect (Node.js):              ~150 MB
Available for interviews:    ~14,534 MB

─── MULTIPROCESSING (current) ───
Per interview (peak):           ~131 MB
Max parallel:          14,534 / 131 =  ~110
Current limit (AIRA_MAX_PARALLEL=5):    5  ← uses only ~655 MB of 14 GB

─── THREADING (proposed) ───
Per interview (peak):            ~61 MB
Max parallel:          14,534 / 61  =  ~238
Safe recommended limit:         ~80–100 (API rate limits are the real ceiling)
```

### Real bottlenecks (not RAM)

| Bottleneck | Detail | Fix |
|-----------|--------|-----|
| `AIRA_MAX_PARALLEL = 5` | Way too conservative for available RAM | Raise to 20–30 after API audit |
| OpenAI rate limits | GPT-4o-mini: ~30,000 TPM default → ~10 concurrent streams | Get Tier-2 API access |
| Deepgram WS connections | Each interview holds 1 WS per question | Check DG plan limits |
| Cartesia API | Concurrent WS connections, new WS per utterance | Make WS persistent per interview |
| CPU (vCPUs) | Audio decode + GPT parsing | Use multi-core VM (>=8 vCPUs for 20+ interviews) |

---

## 5. Race Conditions Analysis

### With `mp.Pool` (current) — mostly safe

Because each interview is a separate OS process, module-level globals are **per-process copies**. Writes to them do not affect other interviews.

| Global | Location | Safe with mp.Pool? | Safe with threads? |
|--------|----------|-------------------|-------------------|
| `_livekit_adapter` | `webrtc_interview.py` line 83 | ✅ Yes (per-process) | 🔴 No — shared write |
| `ACCESS_TOKEN` | `webrtc_interview.py` line 181 | ✅ Yes (per-process) | 🔴 No — shared write |
| `base_url` | `webrtc_interview.py` line 4647 | ✅ Yes (per-process) | 🔴 No — shared write |
| `repeat_qtn`, `is_start_of_interview` | `webrtc_interview.py` lines 318, 321 | ✅ Yes (per-process) | 🔴 No — shared write |
| `USE_CARTESIA`, `DEEPGRAM_API_KEY` | env-derived constants | ✅ Yes | ✅ Yes (read-only) |

### Known bugs in current `mp.Pool` setup (already fixed)

| Bug | File | Status |
|-----|------|--------|
| `_realtime_dg_queue = None` before `await send_task` → `NoneType.get()` crash | `aira_bot_client.py` | ✅ Fixed — local `_dg_q` variable |
| Batch fallback re-recorded after VAD already fired | `aira_bot_client.py` | ✅ Fixed — uses `_incoming_buffer` |

### Remaining race in current mp setup

**`_incoming_buffer` read-while-write (low risk):**

```python
# aira_bot_client.py — stream_to_deepgram batch path
async def send_audio_loop():
    while self._recording:
        current = list(self._incoming_buffer)   # ← snapshot during active append
        new_chunks = current[prev_len:]
        for chunk in new_chunks:
            await dg_ws.send(chunk)             # yields here — buffer grows during yield
```

`list(deque)` is atomic in CPython (GIL-protected). Safe now. Would break with Python free-threading (3.13+). Low priority but worth noting.

---

## 6. WebSocket Thread Safety Deep Dive

This is the core concern when moving to `ThreadPoolExecutor`.

### Architecture of WebSocket ownership

```
ThreadPoolExecutor Thread  (runs orchestrate_interview — SYNCHRONOUS)
    │
    │   adapter.record_transcribe()   ← blocks here
    │   adapter._run(coro)
    │   asyncio.run_coroutine_threadsafe(coro, self._loop)
    │                                         │
    ▼                                         ▼
(blocked, waiting for result)    BotAudioAdapter Event Loop Thread
                                     ← ALL async code runs HERE
                                     ├── websockets.connect() → dg_ws
                                     │       dg_ws.send()  ← on THIS loop only
                                     │       dg_ws.recv()  ← on THIS loop only
                                     ├── AsyncCartesia.tts.websocket_connect() → cartesia_ws
                                     │       cartesia_ws operations ← on THIS loop only
                                     └── rtc.Room.connect() → LiveKit SFU
                                             room.on() callbacks ← fired by Rust tokio thread
```

**Key insight:** The `BotAudioAdapter._run()` bridge ensures all WebSocket I/O happens on the **dedicated per-interview event loop thread**, never on the ThreadPoolExecutor thread directly. This means:

### WebSocket safety verdict

| WebSocket | Created on | Used on | Thread-safe? |
|-----------|-----------|---------|-------------|
| Deepgram (`websockets` lib) | BotAudioAdapter loop | BotAudioAdapter loop | ✅ Safe |
| Cartesia (`AsyncCartesia`) | BotAudioAdapter loop | BotAudioAdapter loop | ✅ Safe |
| LiveKit `rtc.Room` | BotAudioAdapter loop | Rust tokio (internal) + BotAudioAdapter loop | ✅ Safe |

`websockets` library documentation explicitly states connections are **not thread-safe** — but since we never share a connection between threads (each interview has its own loop + its own WS objects), this constraint is already satisfied.

### Specific danger points

#### Danger 1 — `asyncio.get_event_loop()` deprecated in non-main threads

```python
# aira_bot_client.py line 584
asyncio.get_event_loop().run_in_executor(None, early_callback, partial)
```

This is called from inside an `async` function running on the BotAudioAdapter loop —
so `get_event_loop()` returns the correct loop here. But in Python 3.10+ this is deprecated.

**Fix when migrating:**
```python
asyncio.get_running_loop().run_in_executor(None, early_callback, partial)
```

#### Danger 2 — LiveKit `room.on()` callbacks fired from Rust thread

```python
self.room.on("track_subscribed", self._on_track_subscribed)

def _on_track_subscribed(self, track, publication, participant):
    self._incoming_track = track       # written from Rust tokio thread
    self.has_remote_audio = True       # written from Rust tokio thread
```

The Rust/tokio runtime fires these callbacks from its **own internal thread pool**, not from the BotAudioAdapter asyncio loop. Writing simple Python attributes (`bool`, object reference) is GIL-protected in CPython — safe. But:

- ❌ Do NOT call `asyncio.create_task()` inside these callbacks
- ❌ Do NOT call `asyncio.run_coroutine_threadsafe()` without explicitly passing the right loop
- ✅ If you need async work from a callback, use `loop.call_soon_threadsafe()`

#### Danger 3 — `AsyncCartesia` binds to event loop at creation

```python
# cartesia_tts.py line 71
client = AsyncCartesia(api_key=CARTESIA_API_KEY)  # created inside async fn ✅
async with client.tts.websocket_connect() as ws:
```

`AsyncCartesia` internally uses `httpx.AsyncClient` which binds to the running event loop at construction. Since this is always called from inside a `BotAudioAdapter` async context, it binds to the correct loop. ✅ Safe.

**Would break if:** `AsyncCartesia` were instantiated once globally and shared across interviews. Never do this.

### Thread count for N interviews

With threading:
```
N interviews =
    N × ThreadPoolExecutor threads       (run orchestrate_interview — mostly blocked)
  + N × BotAudioAdapter loop threads    (run all async I/O)
  + N × Rust tokio thread pools         (livekit-rtc internal, ~2-4 threads each)
  + 1   Main orchestrator thread
─────────────────────────────────────────
Total OS threads ≈ N × 6–8

20 interviews = ~140 threads    (Linux default limit: 32,768)
50 interviews = ~350 threads    (well within limits)
```

---

## 7. What Needs to Change to Switch

### 7.1 — Fix module-level mutable globals (REQUIRED, critical)

**File:** `webrtc_interview.py`

#### Problem

```python
# Current — module-level, shared across all threads
_livekit_adapter = None                     # line 83
ACCESS_TOKEN = None                          # line 181
repeat_qtn = False                           # line 318
is_start_of_interview = True                 # line 321
```

With threads, Interview A's `set_livekit_adapter(adapter_A)` would overwrite the same global
that Interview B expects to be `adapter_B`. Both would use the wrong adapter.

#### Fix — convert to `threading.local()`

```python
# webrtc_interview.py — replace module globals with thread-local storage
import threading
_tl = threading.local()   # one object, per-thread values

# Replace set_livekit_adapter():
def set_livekit_adapter(adapter):
    _tl.livekit_adapter = adapter
    if adapter is not None:
        # SAFE: only affects this thread's view
        # Don't write os.environ here — use thread-local flag instead
        _tl.use_livekit = True
    logger.info("🔌 LiveKit adapter set (adapter=%s)", "active" if adapter else "None")

# Replace _livekit_adapter reads:
def _get_livekit_adapter():
    return getattr(_tl, 'livekit_adapter', None)

# Replace ACCESS_TOKEN:
def _get_access_token():
    return getattr(_tl, 'access_token', None)

def _set_access_token(token):
    _tl.access_token = token
```

Then replace every reference:
- `_livekit_adapter` → `_get_livekit_adapter()`
- `ACCESS_TOKEN = ...` → `_set_access_token(...)`
- `ACCESS_TOKEN` (read) → `_get_access_token()`

#### Files needing changes

| File | Change |
|------|--------|
| `webrtc_interview.py` | Convert `_livekit_adapter`, `ACCESS_TOKEN`, `repeat_qtn`, `is_start_of_interview` to `threading.local()` |
| `aira_3_webrtc_orchestration.py` | Replace `mp.Pool` with `ThreadPoolExecutor`; remove `webrtc_interview.set_livekit_adapter()` calls from pool workers (they're already per-thread) |

### 7.2 — Replace `mp.Pool` with `ThreadPoolExecutor` (REQUIRED)

**File:** `aira_3_webrtc_orchestration.py`

```python
# BEFORE (current)
import multiprocessing as mp
AIRA_MAX_PARALLEL = 5

with mp.Pool(processes=procs) as pool:
    pool.starmap(orchestrate_interview, jobs_with_index)

# AFTER (threading)
from concurrent.futures import ThreadPoolExecutor, as_completed
AIRA_MAX_PARALLEL = 20   # can raise significantly — RAM is no longer the limit

with ThreadPoolExecutor(max_workers=procs) as executor:
    futures = {
        executor.submit(orchestrate_interview, job, idx): job
        for idx, job in enumerate(jobs)
    }
    for future in as_completed(futures):
        try:
            future.result()
        except Exception as e:
            logger.error("Interview worker crashed: %s", e, exc_info=True)
            # Other interviews continue running — crash isolation via try/except
```

Note: `ThreadPoolExecutor` already exists at line 760 in the file (imported but unused for this purpose).

### 7.3 — Fix `asyncio.get_event_loop()` deprecation (MINOR)

**File:** `aira_bot_client.py` line 584

```python
# BEFORE
asyncio.get_event_loop().run_in_executor(None, early_callback, partial)

# AFTER
asyncio.get_running_loop().run_in_executor(None, early_callback, partial)
```

### 7.4 — Persist Cartesia WebSocket per interview (BONUS — 2–4s latency win)

**File:** `cartesia_tts.py`

Currently a **new Cartesia WebSocket is opened per utterance**:
```python
async with client.tts.websocket_connect() as ws:   # opened and closed per TTS call
```

For a 10-question interview: 20+ WS open/close cycles × ~200–400ms cold start = **4–8 wasted seconds**.

With threading (and thread-local storage available), we can persist the WS for the interview lifetime:

```python
# In AiraBotClient or BotAudioAdapter:
# Open once at interview start
self._cartesia_ws = await client.tts.websocket_connect()

# Reuse for all questions
await speak_cartesia(text, bot_client, ws=self._cartesia_ws)

# Close at interview end
await self._cartesia_ws.__aexit__(...)
```

### 7.5 — Remove `os.environ["AIRA_USE_LIVEKIT"] = "true"` from `set_livekit_adapter`

```python
# webrtc_interview.py line 96
os.environ["AIRA_USE_LIVEKIT"] = "true"   # ← os.environ is process-wide
```

`os.environ` is shared across all threads in a process. Writing it per-interview would
have Interview A's env write affect Interview B's reads. Replace with thread-local flag:

```python
# AFTER
_tl.use_livekit = True if adapter is not None else False

def is_livekit_enabled():
    return getattr(_tl, 'use_livekit', False)
```

---

## 8. Migration Plan (Step-by-Step)

> Do these steps **in order**. Each step is independently testable.

### Phase 0 — Stabilize `mp.Pool` (current)
- [x] Fix `_realtime_dg_queue` race (done)
- [x] Fix batch fallback re-record bug (done)
- [x] Add `utterance_end_ms=1000` + `vad_events=true` to DG URL (done)
- [ ] Raise `AIRA_MAX_PARALLEL` from 5 → 15 (safe with current RAM)
- [ ] Run 10+ parallel interviews in staging to validate stability

### Phase 1 — Refactor globals (prerequisite for threads)
- [ ] Introduce `_tl = threading.local()` in `webrtc_interview.py`
- [ ] Migrate `_livekit_adapter` to `_tl.livekit_adapter`
- [ ] Migrate `ACCESS_TOKEN` to `_tl.access_token`
- [ ] Migrate `repeat_qtn`, `is_start_of_interview` to thread-local
- [ ] Replace `os.environ["AIRA_USE_LIVEKIT"]` with `_tl.use_livekit`
- [ ] Run full regression: `python -m pytest tests/ -x`
- [ ] Run 3 parallel interviews under `mp.Pool` — verify same behaviour (globals change is backward-compatible with mp.Pool)

### Phase 2 — Switch concurrency model
- [ ] Replace `mp.Pool` with `ThreadPoolExecutor` in `aira_3_webrtc_orchestration.py`
- [ ] Add `try/except` wrapper inside `orchestrate_interview` to catch all exceptions (crash isolation)
- [ ] Fix `asyncio.get_event_loop()` → `asyncio.get_running_loop()` in `aira_bot_client.py`
- [ ] Set `AIRA_MAX_PARALLEL = 20` initially
- [ ] Run 5 parallel interviews and check logs for cross-interview contamination

### Phase 3 — Optimisations unlocked by threading
- [ ] Persist Cartesia WebSocket per interview (2–4s latency improvement)
- [ ] Shared DB connection pool (reduces DB connections from N to 1 pool)
- [ ] Raise `AIRA_MAX_PARALLEL` to 30–50 after API rate limit audit
- [ ] Add shared in-process cache for JD parsing (currently parsed per-process)

---

## 9. Testing Checklist Before Going Live

### Functional correctness
- [ ] Two parallel interviews — verify transcript A doesn't bleed into interview B
- [ ] Crash one interview thread intentionally — verify others continue
- [ ] Check `_livekit_adapter` is correctly set per thread (add `logger.info` with `threading.current_thread().name`)
- [ ] Verify `ACCESS_TOKEN` is fetched per thread when JWT expires mid-interview

### Resource metrics
- [ ] `ps aux | grep python` — confirm single process (not N processes)
- [ ] `htop` — confirm thread count = `2N + 1` for N interviews
- [ ] `free -h` — confirm RAM usage is ~N × 61 MB not ~N × 131 MB

### Race condition canary test
```python
# Add this to the start of orchestrate_interview() temporarily:
import threading, time
logger.info("[THREAD %s] Starting interview %s", threading.current_thread().name, interview_id)
time.sleep(0.1)   # force race window
adapter = webrtc_interview._get_livekit_adapter()
assert adapter is not None, f"RACE: adapter is None on thread {threading.current_thread().name}"
```

### WebSocket isolation test
- [ ] Check DG WS logs — each interview's `[room XXXX]` log lines should appear only on that room's DG connection
- [ ] Cartesia: verify `context_id` UUIDs are unique per utterance and don't repeat across interviews

---

## Appendix: Key File Reference

| File | Key lines | Relevance |
|------|-----------|-----------|
| `aira_3_webrtc_orchestration.py` | 762: `orchestrate_interview()` | Interview entry point |
| `aira_3_webrtc_orchestration.py` | 1365: `AIRA_MAX_PARALLEL` | Concurrency limit |
| `aira_3_webrtc_orchestration.py` | 1414: `mp.Pool` call | Target for replacement |
| `webrtc_interview.py` | 83: `_livekit_adapter` | Global → thread-local |
| `webrtc_interview.py` | 181: `ACCESS_TOKEN` | Global → thread-local |
| `webrtc_interview.py` | 92: `set_livekit_adapter()` | Uses `global` keyword |
| `bot_audio_adapter.py` | 63: `asyncio.new_event_loop()` | Per-interview event loop |
| `bot_audio_adapter.py` | 78: `_run(coro)` | Thread→async bridge |
| `aira_bot_client.py` | 454: `websockets.connect()` | Realtime DG WS |
| `aira_bot_client.py` | 581: `asyncio.create_task()` | Early callback dispatch |
| `aira_bot_client.py` | 584: `asyncio.get_event_loop()` | Fix → `get_running_loop()` |
| `cartesia_tts.py` | 72: `websocket_connect()` | New WS per call → persist |

---

*Document created: 2026-06-09*  
*Author: AIRA Engineering (with Antigravity AI)*  
*Next review: After Phase 0 validation (mp.Pool stability at AIRA_MAX_PARALLEL=15)*
