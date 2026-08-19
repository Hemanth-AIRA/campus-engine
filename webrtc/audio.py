"""LiveKit audio I/O — Deepgram STT (via the bot adapter), Cartesia/OpenAI TTS playback,
and the ai_speak family. Split out of webrtc.interview for modularity. Depends on
webrtc.livekit_adapter and webrtc.analysis; the interview loop imports these back."""
import os
import re
import io
import time
import json
import base64
import queue
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import sounddevice as sd
import requests
from pydub import AudioSegment
from webrtc.livekit_adapter import get_livekit_adapter, USE_CARTESIA
from webrtc.analysis import openai_client

logger = logging.getLogger("webrtc.audio")


_TTS_SEMAPHORE = threading.Semaphore(3)
# Task 2: play pre-rendered acks from memory instead of generating + synthesizing them
# live. Kill-switch; when off (or the ack bank is unrendered), the loop uses live acks.
USE_CACHED_ACKS = os.getenv("USE_CACHED_ACKS", "true").lower() == "true"
# Task 2: source JD questions from the /claim question_bank (sequential) and play their
# Redis-cached audio instead of the planner + live TTS. Default OFF until a job's
# question_bank + audio are seeded and validated with a live sim; any miss falls back to live.
USE_CACHED_QUESTIONS = os.getenv("USE_CACHED_QUESTIONS", "false").lower() == "true"
DG_API_KEY = os.getenv("DEEPGRAM_API_KEY")
DG_TIMEOUT_SEC = int(os.getenv("DG_TIMEOUT_SEC", "900"))          # generous for long clips (15 min)
DG_CONNECT_TIMEOUT = int(os.getenv("DG_CONNECT_TIMEOUT", "30"))
DG_CHUNK_BYTES = int(os.getenv("DG_CHUNK_BYTES", str(1_000_000))) # ~1MB chunks
DG_MAX_RETRIES = int(os.getenv("DG_MAX_RETRIES", "3"))
DG_BACKOFF_BASE = float(os.getenv("DG_BACKOFF_BASE", "1.5"))
DG_MODEL = os.getenv("DG_MODEL", "nova-2")  # Switch to nova-3 for faster/newer model


def livekit_speak_cartesia(text: str, room_id: str = "", process_id: int = 0) -> bool:
    """
    Speak text via Cartesia WS → LiveKit when both adapter and CARTESIA_API_KEY
    are active. Falls back to returning False so caller uses OpenAI TTS.

    This is the single intercept point for Cartesia in the interview flow:
        ai_speak_streamed_text → livekit_speak_cartesia (if adapter + cartesia)
        ai_speak_streaming     → livekit_speak_cartesia (if adapter + cartesia)
        ai_speak_with_audio    → kept as-is (pre-generated MP3 still played)

    Returns True if Cartesia handled the speech, False if caller should continue.
    """
    if not USE_CARTESIA:
        return False
    adapter = get_livekit_adapter()
    if not adapter:
        return False
    if not text or not text.strip():
        return True  # Nothing to say, consider handled
    word_count = len(text.split())
    try:
        logger.info(
            "⏱ TIMELINE [process id: %s][room: %s] BOT SPEAK START "
            "| %d words | text='%s'",
            process_id, room_id, word_count,
            text[:80] + "..." if len(text) > 80 else text
        )
        t0 = time.time()
        result = adapter.speak_cartesia(text)
        elapsed = time.time() - t0
        logger.info(
            "⏱ TIMELINE [process id: %s][room: %s] BOT SPEAK DONE "
            "| total=%.0fms | %d words | ~%.0f wps",
            process_id, room_id, elapsed * 1000, word_count,
            word_count / elapsed if elapsed > 0 else 0,
        )
        return result
    except Exception as e:
        logger.error(
            "❌ [CARTESIA][process id: %s][room id: %s] failed: %s",
            process_id, room_id, e
        )
        return False


def play_cached_ack(bucket: str, recent_ids=None, room_id: str = "", process_id: int = 0):
    """Task 2: play a pre-rendered acknowledgement for `bucket` from memory.

    Zero GPT (no ack generation) and zero Cartesia (no live synth). Returns the clip_id
    played (so the caller can track no-immediate-repeat), or None when unavailable —
    ack bank unrendered, kill-switch off, or no adapter — so the caller falls back to
    the live ack path.
    """
    if not USE_CACHED_ACKS:
        return None
    adapter = get_livekit_adapter()
    if not adapter:
        return None
    try:
        from ack_audio import get_ack_bank
        bank = get_ack_bank()
        picked = bank.pick(bucket, recent_ids)
        if not picked:
            return None
        pcm, clip_id = picked
        if not pcm:
            return None
        if adapter.play_cached_clip(pcm, bank.sample_rate, clip_id):
            logger.info(
                "🔊 [process id: %s][room: %s] CACHED ACK '%s' [bucket=%s] — no GPT, no Cartesia",
                process_id, room_id, clip_id, bucket,
            )
            return clip_id
        return None
    except Exception as e:
        logger.warning(
            "⚠️ [process id: %s][room: %s] cached ack failed (%s) — live fallback",
            process_id, room_id, e,
        )
        return None


def _deepgram_stream_upload(byte_iter, content_type: str):
    """
    Stream bytes to Deepgram /v1/listen using chunked transfer encoding,
    with retries/backoff. Returns parsed JSON.
    """
    import requests, time, json, itertools

    url = "https://api.deepgram.com/v1/listen"
    params = {
        "model": DG_MODEL,
        "language": "en",
        "smart_format": "true",
        "punctuate": "true",
        "endpointing": "300",  # Faster final results for short utterances
    }
    headers = {
        "Authorization": f"Token {DG_API_KEY}",
        "Accept": "application/json",
        "Content-Type": content_type or "application/octet-stream",
        # NOTE: no Content-Length -> requests will send chunked
        "Connection": "keep-alive",
    }

    for attempt in range(1, DG_MAX_RETRIES + 1):
        try:
            with requests.Session() as s:
                resp = s.post(
                    url,
                    params=params,
                    headers=headers,
                    data=byte_iter,  # iterable -> chunked upload
                    timeout=(DG_CONNECT_TIMEOUT, DG_TIMEOUT_SEC),
                )
                # Raise HTTP errors so we can handle 408, 413, etc.
                resp.raise_for_status()
                return resp.json()

        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = (getattr(e.response, "text", "") or "")[:400]
            logging.getLogger(__name__).warning(
                "Deepgram HTTP error (status=%s) attempt %d/%d. Body: %s",
                status, attempt, DG_MAX_RETRIES, body
            )
            # For 408/429/5xx retry with backoff
            if status in (408, 429) or (status and 500 <= status < 600):
                if attempt < DG_MAX_RETRIES:
                    time.sleep((DG_BACKOFF_BASE ** attempt))
                    continue
            # Otherwise, give up
            raise
        except requests.RequestException as e:
            logging.getLogger(__name__).warning(
                "Deepgram network error attempt %d/%d: %s",
                attempt, DG_MAX_RETRIES, e
            )
            if attempt < DG_MAX_RETRIES:
                time.sleep((DG_BACKOFF_BASE ** attempt))
                continue
            raise


def _iter_bytes(b: bytes, chunk_size: int):
    for i in range(0, len(b), chunk_size):
        yield b[i:i+chunk_size]


def deepgram_transcribe_bytes(audio_bytes: bytes, content_type: str) -> str:
    """
    Stream a bytes payload to Deepgram and return the transcript string.
    """
    resp = _deepgram_stream_upload(_iter_bytes(audio_bytes, DG_CHUNK_BYTES), content_type)
    return (resp.get("results", {})
               .get("channels", [{}])[0]
               .get("alternatives", [{}])[0]
               .get("transcript", "")).strip()


def record_transcribe_webrtc_audio(room_id, process_id,
    driver,
    *,
    tick_ms: int = 200,
    silence_ms_idle: int = 4000,     # no-audio baseline window
    silence_ms_active: int | None = None,  # post-activity window; defaults to idle if None
    max_ms: int = 1_020_000,  # Increased max time to 17 minutes (1020,000 ms)
    silence_rms: float = 0.015,
    min_active_ms: int = 1000
):
    """
    WebRTC recorder with adaptive silence thresholds:
      - If *no audio ever detected*: stop on 'silence_ms_idle' (e.g., 30s for muted-present windows).
      - Once any audio is detected (RMS >= threshold): stop on 'silence_ms_active' (e.g., 4s).
    """
    from datetime import datetime
    import logging
    logger = logging.getLogger(__name__)

    # ── LiveKit path: bypass browser entirely ──
    adapter = get_livekit_adapter()
    if adapter:
        logger.info("🎤🔌[process id: %s][room id: %s] [LIVEKIT] record_transcribe via LiveKit adapter", process_id, room_id)
        return adapter.record_transcribe(
            silence_ms=silence_ms_active or silence_ms_idle,
            max_ms=max_ms,
            silence_rms=silence_rms,
            min_active_ms=min_active_ms,
        )

    # LiveKit-only: a missing adapter means the bot isn't wired up — that's a
    # bug, not a browser fallback. Fail loud.
    raise RuntimeError(
        f"[{room_id}] record_transcribe_webrtc_audio: no LiveKit adapter (LiveKit-only, no browser fallback)"
    )


def record_transcribe_webrtc_streaming(room_id, process_id,
    driver,
    *,
    tick_ms: int = 200,
    silence_ms_idle: int = 3000,
    silence_ms_active: int | None = None,
    max_ms: int = 1_020_000,
    silence_rms: float = 0.015,
    min_active_ms: int = 1000,
    speculative: bool = False
):
    """
    WebSocket streaming version: audio is streamed to Deepgram DURING recording.
    Transcript is ready the instant the candidate stops speaking — near-zero transcription wait.

    Falls back to batch mode on any WebSocket failure.

    Same return format as record_transcribe_webrtc_audio: {answer, answer_duration}
    """
    from datetime import datetime
    import logging
    logger = logging.getLogger(__name__)

    # ── LiveKit path: bypass browser entirely ──
    adapter = get_livekit_adapter()
    if adapter:
        logger.info(
            "⏱ TIMELINE [process id: %s][room: %s] RECORDING START "
            "(LiveKit adapter | silence=%dms max=%dms)",
            process_id, room_id,
            silence_ms_active or silence_ms_idle, max_ms,
        )
        t_rec = time.time()
        result = adapter.record_transcribe(
            silence_ms=silence_ms_active or silence_ms_idle,
            max_ms=max_ms,
            silence_rms=silence_rms,
            min_active_ms=min_active_ms,
        )
        rec_elapsed = (time.time() - t_rec) * 1000
        answer = result.get("answer", "") if result else ""
        word_count = len(answer.split()) if answer.strip() else 0
        logger.info(
            "⏱ TIMELINE [process id: %s][room: %s] TRANSCRIPT RETURNED "
            "to orchestrator (%.0fms) | %d words | answer='%s'",
            process_id, room_id, rec_elapsed, word_count,
            answer[:100] + "..." if len(answer) > 100 else answer,
        )
        return result

    # LiveKit-only: a missing adapter means the bot isn't wired up — that's a
    # bug, not a browser fallback. Fail loud.
    raise RuntimeError(
        f"[{room_id}] record_transcribe_webrtc_streaming: no LiveKit adapter (LiveKit-only, no browser fallback)"
    )


def _streaming_continuation(driver, room_id, process_id, skill_states=None):
    """
    Continuation script: waits for window.__airaRecording.done OR reSilence.
    If reSilence detected, returns early with updated transcript for another speculative attempt.
    """
    raise RuntimeError("_streaming_continuation: browser speculative path removed — LiveKit-only, no browser fallback")


def transcribe_webrtc_audio(audio_data_url,room_id,process_id):
    """
    Deepgram-only transcription for WebRTC data URLs.
    Tries native format first (e.g., audio/webm;codecs=opus) via chunked streaming.
    Falls back to WAV conversion via ffmpeg if needed.
    """
    from datetime import datetime
    import os, base64, logging, json, tempfile, subprocess, mimetypes

    logger = logging.getLogger(__name__)
    t0 = datetime.now()
    logger.info("📝[process id: %s][room id: %s] [AUDIO LOG] Starting WebRTC audio transcription (Deepgram, chunked)",process_id,room_id)
    start=time.time()
    # ---- 1) Parse data URL ----
    try:
        if "," in audio_data_url:
            header, b64 = audio_data_url.split(",", 1)
            # Preserve full MIME including codecs (e.g., 'audio/webm;codecs=opus')
            ct = "application/octet-stream"
            if header.startswith("data:"):
                # Typical header: 'data:audio/webm;codecs=opus;base64'
                # Prefer everything between 'data:' and the trailing ';base64'
                try:
                    ct = header[5: header.rindex(";base64")]
                except ValueError:
                    # Fallback: everything after 'data:' if ';base64' not present
                    ct = header[5:]
            audio_bytes = base64.b64decode(b64)
            logger.info("🔍[process id: %s][room id: %s] [AUDIO LOG] Data URL content-type: %s; bytes=%d",process_id,room_id, ct, len(audio_bytes))
        else:
            ct = "application/octet-stream"
            audio_bytes = base64.b64decode(audio_data_url)
            logger.info("🔍[process id: %s][room id: %s] [AUDIO LOG] No header; bytes=%d",process_id,room_id, len(audio_bytes))
    except Exception:
        logger.exception("❌[process id: %s][room id: %s] [AUDIO LOG] Base64 decode failed",process_id,room_id)
        return "Transcription failed: could not decode audio."


    # ---- 2) Try native media first with chunked upload & retries ----
    try:
        transcript = deepgram_transcribe_bytes(audio_bytes, ct)
        end = time.time()
        elapsed = end - start
        logger.info(f"[process id: {process_id}][room id: {room_id}]Execution time (time taken to transcribe audio... where length of transcript is: {len(transcript)} and total number of words in transcript are: {len(transcript.split(' '))}): {elapsed:.6f} seconds")
        if transcript:
            dt = (datetime.now() - t0).total_seconds()
            logger.info("✅[process id: %s][room id: %s] [AUDIO LOG] Deepgram transcription OK in %.2fs",process_id,room_id, dt)
            logger.info("✅[process id: %s][room id: %s] [AUDIO LOG] - ----------------------------------------------------------------------------------------------------",process_id,room_id)
            logger.info("📄[process id: %s][room id: %s] Transcript: %s",process_id,room_id, transcript[:500] + ("…" if len(transcript) > 500 else ""))
            logger.info("✅[process id: %s][room id: %s] [AUDIO LOG] - ----------------------------------------------------------------------------------------------------",process_id,room_id)
            return transcript
        logger.warning("⚠️[process id: %s][room id: %s] [AUDIO LOG] Empty transcript recieveed from deepgram.",process_id,room_id)
    except Exception as e:
        # Typical here: 415/415-ish or 408 under heavy parallel load; we fall back to WAV
        logger.warning("⚠️[process id: %s][room id: %s] [AUDIO LOG] Native format upload failed (%s). Trying WAV fallback.",process_id,room_id, e)


def play_audio_in_webrtc(room_id,process_id,driver, audio_data, timeout_ms=180000, monitor_locally=False):
    """
    Inject TTS audio into the page's Web Audio graph so peers hear it over WebRTC.
    Blocks until playback ends (success) or errors/times out. Returns True/False.
    """
    from datetime import datetime
    import base64, json, logging
    logger = logging.getLogger(__name__)

    # ── LiveKit path: publish audio directly via LiveKit track ──
    adapter = get_livekit_adapter()
    if adapter:
        try:
            audio_bytes = audio_data if isinstance(audio_data, bytes) else audio_data.encode()
            return adapter.play_audio(audio_bytes, format="mp3")
        except Exception as e:
            logger.error("❌[process id: %s][room id: %s] [LIVEKIT] play_audio failed: %s", process_id, room_id, e)
            return False

    # LiveKit-only: a missing adapter means the bot isn't wired up — that's a
    # bug, not a browser fallback. Fail loud.
    raise RuntimeError(
        f"[{room_id}] play_audio_in_webrtc: no LiveKit adapter (LiveKit-only, no browser fallback)"
    )


def play_ack_with_bargein(room_id, process_id, driver, audio_bytes,
                          bargein_rms=0.03, bargein_sustain_ms=400,
                          cooldown_ms=1500, timeout_ms=180000):
    """
    Play audio through WebRTC while monitoring candidate's mic for barge-in.
    
    If candidate speaks during playback (RMS > threshold for sustained duration):
      - Stops audio playback immediately
      - Opens Deepgram WS to capture candidate speech until 1s silence
      - Returns { success:True, interrupted:True, transcript:"..." }
    
    If playback finishes without interruption:
      - Returns { success:True, interrupted:False }
    
    Args:
        audio_bytes: MP3 audio bytes to play
        bargein_rms: RMS threshold for barge-in detection (0.03 avoids echo false positives)
        bargein_sustain_ms: Sustained speech duration to trigger (400ms avoids transient echo)
        cooldown_ms: Ignore mic after play starts (1.5s for echo roundtrip to settle)
        timeout_ms: Max playback time
    """
    # ── LiveKit path: play with server-side barge-in detection ──
    adapter = get_livekit_adapter()
    if adapter:
        logger.info("🔊🔌[process id: %s][room id: %s] [LIVEKIT] play_with_bargein via LiveKit adapter", process_id, room_id)
        result = adapter.play_with_bargein(
            audio_bytes, format="mp3",
            bargein_rms=bargein_rms,
            bargein_ms=bargein_sustain_ms,
        )
        # Normalize to match existing return format
        result["success"] = True
        return result

    # LiveKit-only: a missing adapter means the bot isn't wired up — that's a
    # bug, not a browser fallback. Fail loud.
    raise RuntimeError(
        f"[{room_id}] play_ack_with_bargein: no LiveKit adapter (LiveKit-only, no browser fallback)"
    )


def play_streaming_audio_in_webrtc(room_id, process_id, driver, audio_bytes, timeout_ms=180000):
    """
    Play a single MP3 audio segment through WebRTC using the existing __airaAudio infrastructure.
    Designed for sequential calls — each segment plays after the previous ends.
    Returns True if audio was successfully played.
    """
    # ── LiveKit path: publish audio segment directly ──
    adapter = get_livekit_adapter()
    if adapter:
        try:
            return adapter.play_audio(audio_bytes, format="mp3")
        except Exception as e:
            logger.error("❌[process id: %s][room id: %s] [LIVEKIT] streaming play failed: %s", process_id, room_id, e)
            return False

    # LiveKit-only: a missing adapter means the bot isn't wired up — that's a
    # bug, not a browser fallback. Fail loud.
    raise RuntimeError(
        f"[{room_id}] play_streaming_audio_in_webrtc: no LiveKit adapter (LiveKit-only, no browser fallback)"
    )


def ai_speak_streaming(ack_text_generator, driver=None, room_id="", process_id=0):
    """
    Fully pipelined: GPT text stream → overlapped TTS → sequential WebRTC playback.
    
    Architecture:
      - Producer thread: consumes GPT tokens, buffers into sentences,
        fires TTS per sentence via thread pool (non-blocking), puts
        (sentence_text, tts_future) into a queue.
      - Consumer (this thread): pulls from queue in order, waits for
        each TTS future, plays audio through WebRTC.
    
    This means:
      - While sentence #1 audio is playing, sentence #2 TTS is already running,
        AND GPT is still generating tokens for sentence #3.
      - Zero dead air between segments.
    
    Args:
        ack_text_generator: Generator yielding text chunks from GPT streaming
        driver: Selenium WebDriver for WebRTC audio injection
        room_id: For logging
        process_id: For logging
        
    Returns:
        str: The full ack text that was spoken
    """
    # ── Cartesia fast-path (LiveKit + Cartesia) ──────────────────────────────
    # Stream GPT tokens directly into Cartesia WS — candidate hears audio
    # within ~300ms of the first GPT token. No buffering, no waiting.
    adapter = get_livekit_adapter()
    if USE_CARTESIA and adapter:
        collected_parts = []
        try:
            # Wrap the generator to collect tokens for return value
            # while streaming them live to Cartesia
            def _collecting_iter():
                for chunk in ack_text_generator:
                    collected_parts.append(chunk)
                    yield chunk

            t0 = time.time()
            logger.info(
                "⏱ TIMELINE [process id: %s][room: %s] BOT ACK SPEAK START (token-streaming)",
                process_id, room_id,
            )
            adapter.speak_cartesia_tokens(_collecting_iter())
            full_text = "".join(collected_parts)
            elapsed = time.time() - t0
            word_count = len(full_text.split())
            logger.info(
                "⏱ TIMELINE [process id: %s][room: %s] BOT ACK SPEAK DONE "
                "| total=%.0fms | %d words | text='%s'",
                process_id, room_id, elapsed * 1000, word_count,
                full_text[:80] + "..." if len(full_text) > 80 else full_text,
            )
            return full_text
        except Exception as e:
            logger.error("❌ [CARTESIA][process id: %s][room id: %s] token-streaming fallback: %s",
                         process_id, room_id, e)
            # fall through to OpenAI pipeline below
            ack_text_generator = iter(collected_parts)  # replay collected tokens
    # ───────────────────────────────────────────────────────────────
    import queue
    import threading
    from concurrent.futures import ThreadPoolExecutor

    pipeline_start = time.time()
    audio_queue = queue.Queue()  # Items: (idx, sentence_text, tts_future) or None (sentinel)

    logger.info(
        "🚀[process id: %s][room id: %s] [STREAM-PIPELINE] Starting pipelined GPT→TTS→WebRTC",
        process_id, room_id
    )

    # ── TTS helper ──
    def _fire_tts(sentence_text, idx):
        """Generate TTS for a single sentence (runs in thread pool)."""
        tts_start = time.time()
        try:
            with _TTS_SEMAPHORE:  # cap concurrent TTS calls to prevent OpenAI connection stalls
                response = openai_client.audio.speech.create(
                    model="tts-1",
                    voice="nova",
                    input=sentence_text,
                    response_format="mp3",
                    speed=1,
                )
            tts_elapsed = time.time() - tts_start
            logger.info(
                "🔊[process id: %s][room id: %s] [PIPELINE-TTS] Sentence #%d TTS: %.0fms | %d bytes | '%s'",
                process_id, room_id, idx, tts_elapsed * 1000,
                len(response.content),
                sentence_text[:60] + "..." if len(sentence_text) > 60 else sentence_text
            )
            return response.content
        except Exception as e:
            logger.error(
                "❌[process id: %s][room id: %s] [PIPELINE-TTS] Sentence #%d failed: %s",
                process_id, room_id, idx, e
            )
            return None
    
    # ── Producer thread: GPT tokens → sentences → TTS futures → queue ──
    def _producer(tts_pool):
        """Consume GPT stream, buffer into sentences, fire TTS, enqueue."""
        buffer = ""
        sentence_count = 0
        
        BOUNDARIES = {'.', '!', '—'}
        SOFT_BOUNDARY = {','}
        MIN_SEGMENT_LEN = 20
        MIN_SOFT_SEGMENT_LEN = 50
        
        try:
            for text_chunk in ack_text_generator:
                buffer += text_chunk
                
                stripped = buffer.rstrip()
                should_flush = False
                
                if len(stripped) >= MIN_SEGMENT_LEN:
                    if any(stripped.endswith(b) for b in BOUNDARIES):
                        should_flush = True
                    elif len(stripped) >= MIN_SOFT_SEGMENT_LEN and any(stripped.endswith(b) for b in SOFT_BOUNDARY):
                        should_flush = True
                
                if should_flush:
                    sentence_count += 1
                    segment_text = buffer.strip()
                    buffer = ""
                    
                    # Fire TTS in thread pool — NON-BLOCKING, immediately continue buffering
                    future = tts_pool.submit(_fire_tts, segment_text, sentence_count)
                    audio_queue.put((sentence_count, segment_text, future))
                    
                    logger.info(
                        "📤[process id: %s][room id: %s] [PIPELINE] Sentence #%d queued for TTS: '%s'",
                        process_id, room_id, sentence_count,
                        segment_text[:50] + "..." if len(segment_text) > 50 else segment_text
                    )
            
            # Flush remaining buffer
            if buffer.strip():
                sentence_count += 1
                segment_text = buffer.strip()
                future = tts_pool.submit(_fire_tts, segment_text, sentence_count)
                audio_queue.put((sentence_count, segment_text, future))
                
                logger.info(
                    "📤[process id: %s][room id: %s] [PIPELINE] Sentence #%d (final) queued: '%s'",
                    process_id, room_id, sentence_count,
                    segment_text[:50] + "..." if len(segment_text) > 50 else segment_text
                )
                
        except Exception as e:
            logger.error(
                "❌[process id: %s][room id: %s] [PIPELINE] Producer failed: %s",
                process_id, room_id, e
            )
        finally:
            audio_queue.put(None)  # Sentinel: producer is done
    
    # ── Consumer: play audio segments in order ──
    full_ack_text = ""
    segments_played = 0
    
    try:
        tts_pool = ThreadPoolExecutor(max_workers=3)
        producer_thread = threading.Thread(target=_producer, args=(tts_pool,), daemon=True)
        producer_thread.start()
        
        while True:
            item = audio_queue.get(timeout=30)  # 30s safety timeout
            
            if item is None:
                break  # Producer is done
            
            idx, sentence_text, tts_future = item
            full_ack_text += (" " if full_ack_text else "") + sentence_text
            
            # Wait for THIS sentence's TTS to complete
            audio_bytes = tts_future.result(timeout=15)
            
            if not audio_bytes:
                continue
            
            segments_played += 1
            
            if segments_played == 1:
                ttfa = time.time() - pipeline_start
                logger.info(
                    "⚡[process id: %s][room id: %s] [STREAM-PIPELINE] TIME-TO-FIRST-AUDIO: %.0fms",
                    process_id, room_id, ttfa * 1000
                )
            
            # Play through WebRTC (while producer continues buffering + firing TTS)
            _play_segment_system_audio(audio_bytes, room_id, process_id)
        
        producer_thread.join(timeout=5)
        tts_pool.shutdown(wait=False)
        
        total_elapsed = time.time() - pipeline_start
        logger.info(
            "✅[process id: %s][room id: %s] [STREAM-PIPELINE] Complete: %d segments in %.0fms | ack='%s'",
            process_id, room_id, segments_played, total_elapsed * 1000,
            full_ack_text[:80] + "..." if len(full_ack_text) > 80 else full_ack_text
        )
        
    except Exception as e:
        logger.error(
            "❌[process id: %s][room id: %s] [STREAM-PIPELINE] Failed: %s",
            process_id, room_id, e, exc_info=True
        )
        try:
            tts_pool.shutdown(wait=False)
        except Exception:
            pass
    
    return full_ack_text


def _play_segment_system_audio(audio_bytes, room_id, process_id):
    """Play a single MP3 audio segment through system audio (fallback)."""
    try:
        audio_stream = io.BytesIO(audio_bytes)
        audio = AudioSegment.from_file(audio_stream, format="mp3")
        samples = (
            np.array(audio.get_array_of_samples()).astype(np.float32) / 32768.0
        )
        sd.play(samples, audio.frame_rate)
        sd.wait()
    except Exception as e:
        logger.warning(
            "❌[process id: %s][room id: %s] [STREAM-PLAY] System audio fallback failed: %s",
            process_id, room_id, e
        )


def ai_speak_streamed_text(text, driver=None, room_id="", process_id=0, allow_bargein=False):
    """
    Speak pre-computed text using PARALLEL sentence TTS + sequential playback.
    
    Strategy:
    1. Split text into sentences at boundary characters (. ! — etc.)
    2. Fire ALL TTS calls simultaneously via ThreadPoolExecutor
    3. Play audio segments sequentially as they become ready
    
    By the time sentence #1 finishes playing (~2s), sentences #2-4 
    are already generated → zero dead air between segments.
    
    Falls back to legacy ai_speak() on failure.
    
    When allow_bargein=True:
    - Uses play_ack_with_bargein() which monitors candidate mic during playback
    - If interrupted, returns dict: {"interrupted": True, "bargein_transcript": "..."}
    - If NOT interrupted, returns the spoken text string (normal behavior)
    
    Args:
        text: The full text to speak (plain string)
        driver: Selenium WebDriver for WebRTC audio injection
        room_id: For logging
        process_id: For logging
        allow_bargein: If True, enable barge-in detection during playback
        
    Returns:
        str if no interruption (the text spoken)
        dict if interrupted: {"interrupted": True, "bargein_transcript": "..."}"""
    if not text or not text.strip():
        return ""

    # ── Cartesia fast-path (LiveKit + Cartesia) ──────────────────────────────
    # When both LiveKit adapter and CARTESIA_API_KEY are set, use Cartesia WS
    # streaming directly. TTFA ~300ms vs OpenAI TTS ~1-2s.
    # No sentence splitting, no thread pool, no pre-generation needed.
    if livekit_speak_cartesia(text, room_id, process_id):
        return text
    # ─────────────────────────────────────────────────────────────────────────


    
    logger.info(
        "🚀[process id: %s][room id: %s] [STREAM-TEXT] Starting parallel-TTS for: '%s'",
        process_id, room_id, text[:80] + "..." if len(text) > 80 else text
    )
    
    try:
        # ── Fast path: short text → single TTS call (no splitting overhead) ──
        # For short acks (<120 chars), a single API call is faster & more reliable
        # than splitting into 2 calls where one could spike.
        SHORT_TEXT_THRESHOLD = 120
        if len(text) <= SHORT_TEXT_THRESHOLD:
            logger.info(
                "⚡[process id: %s][room id: %s] [STREAM-TEXT] Short text (%d chars) → single TTS call",
                process_id, room_id, len(text)
            )
            tts_start = time.time()
            response = openai_client.audio.speech.create(
                model="tts-1",
                voice="nova",
                input=text,
                response_format="mp3",
                speed=1,
            )
            audio_bytes = response.content
            ttfa = time.time() - pipeline_start
            logger.info(
                "⚡[process id: %s][room id: %s] [STREAM-TEXT] TIME-TO-FIRST-AUDIO: %.0fms | %d bytes",
                process_id, room_id, ttfa * 1000, len(audio_bytes)
            )
            
            _play_segment_system_audio(audio_bytes, room_id, process_id)
            return text
        
        # ── Step 1: Split text into sentence-sized segments ──
        sentences = _split_into_sentences(text)
        
        if not sentences:
            ai_speak(text, driver, room_id, process_id)
            return text
        
        logger.info(
            "📝[process id: %s][room id: %s] [STREAM-TEXT] Split into %d sentences",
            process_id, room_id, len(sentences)
        )
        
        # ── Step 2: Fire ALL TTS calls in parallel ──
        from concurrent.futures import ThreadPoolExecutor as TtsPool
        
        def _fire_tts(sentence_text, idx):
            """Generate TTS for a single sentence."""
            tts_start = time.time()
            try:
                with _TTS_SEMAPHORE:  # cap concurrent TTS calls to prevent OpenAI connection stalls
                    response = openai_client.audio.speech.create(
                        model="tts-1",
                        voice="nova",
                        input=sentence_text,
                        response_format="mp3",
                        speed=1,
                    )
                tts_elapsed = time.time() - tts_start
                logger.info(
                    "🔊[process id: %s][room id: %s] [PARALLEL-TTS] Sentence #%d TTS: %.0fms | %d bytes | '%s'",
                    process_id, room_id, idx + 1, tts_elapsed * 1000,
                    len(response.content),
                    sentence_text[:60] + "..." if len(sentence_text) > 60 else sentence_text
                )
                return response.content
            except Exception as e:
                logger.error(
                    "❌[process id: %s][room id: %s] [PARALLEL-TTS] Sentence #%d failed: %s",
                    process_id, room_id, idx + 1, e
                )
                return None
        
        # Fire all TTS requests simultaneously
        tts_futures = []
        with TtsPool(max_workers=min(len(sentences), 4)) as pool:
            for i, sentence in enumerate(sentences):
                tts_futures.append(pool.submit(_fire_tts, sentence, i))
            
            # ── Step 3: Play segments sequentially as they complete ──
            # Wait for each in ORDER (not as-completed) to maintain speech order
            for i, future in enumerate(tts_futures):
                audio_bytes = future.result(timeout=15)
                
                if not audio_bytes:
                    continue
                
                if i == 0:
                    ttfa = time.time() - pipeline_start
                    logger.info(
                        "⚡[process id: %s][room id: %s] [STREAM-TEXT] TIME-TO-FIRST-AUDIO: %.0fms",
                        process_id, room_id, ttfa * 1000
                    )
                
                # Play through WebRTC (with optional barge-in on first segment)
                _play_segment_system_audio(audio_bytes, room_id, process_id)
        
        total_elapsed = time.time() - pipeline_start
        logger.info(
            "✅[process id: %s][room id: %s] [STREAM-TEXT] Complete: %d sentences in %.0fms",
            process_id, room_id, len(sentences), total_elapsed * 1000
        )
        return text
        
    except Exception as e:
        logger.warning(
            "⚠️[process id: %s][room id: %s] [STREAM-TEXT] Failed: %s, falling back to legacy",
            process_id, room_id, e
        )
        ai_speak(text, driver, room_id, process_id)
        return text


def _split_into_sentences(text):
    """
    Split text into sentence-sized segments for parallel TTS.
    
    Splits at sentence boundaries (. ! ? —) while respecting minimum
    segment length to avoid tiny TTS calls with poor prosody.
    """
    import re
    
    # Split on sentence-ending punctuation followed by whitespace
    # Keeps the punctuation attached to the sentence
    raw_segments = re.split(r'(?<=[.!?—])\s+', text.strip())
    
    # Merge very short segments with the next one for better prosody
    MIN_LEN = 20
    merged = []
    buffer = ""
    
    for seg in raw_segments:
        if buffer:
            buffer += " " + seg
        else:
            buffer = seg
        
        if len(buffer) >= MIN_LEN:
            merged.append(buffer)
            buffer = ""
    
    # Flush remaining buffer
    if buffer:
        if merged:
            merged[-1] += " " + buffer
        else:
            merged.append(buffer)
    
    return merged


def ai_speak(text, driver=None, room_id="", process_id=0, **kwargs):
    logger.info("-" * 100)
    logger.info(
        "🗣️[process id: %s][room id: %s] AIRA STARTING TO SPEAK: %s", process_id, room_id, text[:500] + "..." if len(text) > 500 else text
    )
    logger.info("-" * 100)

    # ── Cartesia intercept ──────────────────────────────────────────────────────
    # When LiveKit+Cartesia is active, bypass OpenAI TTS entirely.
    # Cartesia TTFA ~300ms vs OpenAI TTS ~2-4s — no wasted network round-trip.
    if livekit_speak_cartesia(text, room_id, process_id):
        return
    # ────────────────────────────────────────────────────────────────────────────

    def speak_task():
        speak_start_time = datetime.now()
        logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] AIRA speech initiated at %s", process_id, room_id, speak_start_time.strftime("%Y-%m-%d %H:%M:%S.%f"))
        
        try:
            tts_start = time.time()
            audio_data = generate_ai_response(text,room_id,process_id)
            audio_stream = io.BytesIO()
            for chunk in audio_data:
                if chunk:
                    audio_stream.write(chunk)
            audio_stream.seek(0)
            tts_elapsed = time.time() - tts_start
            
            # Calculate audio duration for logging
            try:
                audio = AudioSegment.from_file(audio_stream, format="mp3")
                audio_duration = len(audio) / 1000.0  # Duration in seconds
                audio_size = len(audio_stream.getvalue())
                logger.info("🎵[process id: %s][room id: %s] [AUDIO LOG] Generated TTS audio: Duration=%.2fs, Size=%d bytes | OpenAI TTS took %.0fms",process_id,room_id, audio_duration, audio_size, tts_elapsed*1000)
            except Exception as duration_error:
                logger.warning("[process id: %s][room id: %s] Could not calculate audio duration: %s",process_id,room_id, duration_error)
                audio_duration = 0
            
            audio_stream.seek(0)  # Reset stream position
            
            # ── LiveKit path: route through adapter (no driver needed) ──
            if get_livekit_adapter() is not None:
                audio_bytes = audio_stream.getvalue()
                logger.info("🔌[process id: %s][room id: %s] [AUDIO LOG] Playing TTS through LiveKit adapter",process_id,room_id)
                success = play_audio_in_webrtc(room_id,process_id,driver, audio_bytes)
                if success:
                    speak_end_time = datetime.now()
                    total_duration = (speak_end_time - speak_start_time).total_seconds()
                    logger.info("✅[process id: %s][room id: %s] [AUDIO LOG] AIRA LiveKit speech completed successfully",process_id,room_id)
                    logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] AIRA speech ended at %s (Total duration: %.2fs)",process_id,room_id, 
                              speak_end_time.strftime("%Y-%m-%d %H:%M:%S.%f"), total_duration)
                    return

            # If we have a WebRTC driver and WebRTC audio is enabled, try playing through browser
            
            # Fallback to system audio
            logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] Playing TTS through system audio",process_id,room_id)
            audio = AudioSegment.from_file(audio_stream, format="mp3")
            samples = (
                np.array(audio.get_array_of_samples()).astype(np.float32) / 32768.0
            )
            # Use default audio output device
            try:
                sd.play(samples, audio.frame_rate)
                sd.wait()
                speak_end_time = datetime.now()
                total_duration = (speak_end_time - speak_start_time).total_seconds()
                logger.info("✅[process id: %s][room id: %s] [AUDIO LOG] AIRA system audio speech completed successfully",process_id,room_id)
                logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] AIRA speech ended at %s (Total duration: %.2fs)",process_id,room_id, 
                          speak_end_time.strftime("%Y-%m-%d %H:%M:%S.%f"), total_duration)
            except Exception as audio_error:
                speak_end_time = datetime.now()
                total_duration = (speak_end_time - speak_start_time).total_seconds()
                logger.warning("❌[process id: %s][room id: %s] [AUDIO LOG] Audio playback failed: %s",process_id,room_id, audio_error)
                logger.info("🔇[process id: %s][room id: %s] [AUDIO LOG] Running in headless mode - audio would be played here",process_id,room_id)
                logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] AIRA speech ended at %s (Total duration: %.2fs)",process_id,room_id, 
                          speak_end_time.strftime("%Y-%m-%d %H:%M:%S.%f"), total_duration)
        except Exception as e:
            speak_end_time = datetime.now()
            total_duration = (speak_end_time - speak_start_time).total_seconds()
            logger.error("❌[process id: %s][room id: %s] [AUDIO LOG] Error in ai_speak: %s",process_id,room_id, e)
            logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] AIRA speech ended with error at %s (Total duration: %.2fs)",process_id,room_id, 
                      speak_end_time.strftime("%Y-%m-%d %H:%M:%S.%f"), total_duration)

    speak_task()


def ai_speak_with_audio(text, pre_generated_audio_stream, driver=None, room_id="", process_id=0, **kwargs):
    """
    Like ai_speak, but uses PRE-GENERATED audio bytes instead of calling OpenAI TTS.
    Used for the Q1 greeting whose TTS was pre-computed during init.
    When Cartesia is active, ignores the pre-generated audio and speaks via Cartesia.
    """
    logger.info("-" * 100)
    logger.info(
        "🗣️[process id: %s][room id: %s] AIRA STARTING TO SPEAK (PRE-GENERATED): %s", process_id, room_id, text[:500] + "..." if len(text) > 500 else text
    )
    logger.info("-" * 100)

    # ── Cartesia intercept ────────────────────────────────────────────────────
    # When Cartesia is active, use it directly instead of the pre-generated MP3.
    # This eliminates the 14s OpenAI TTS pre-generation cost.
    if livekit_speak_cartesia(text, room_id, process_id):
        return
    # ─────────────────────────────────────────────────────────────────────────

    def speak_task():
        speak_start_time = datetime.now()
        logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] AIRA speech initiated (pre-generated) at %s", process_id, room_id, speak_start_time.strftime("%Y-%m-%d %H:%M:%S.%f"))

        try:
            audio_stream = pre_generated_audio_stream
            audio_stream.seek(0)

            # Calculate audio duration for logging
            try:
                audio = AudioSegment.from_file(audio_stream, format="mp3")
                audio_duration = len(audio) / 1000.0
                audio_size = len(audio_stream.getvalue())
                logger.info("🎵[process id: %s][room id: %s] [AUDIO LOG] Pre-generated TTS audio: Duration=%.2fs, Size=%d bytes | OpenAI TTS took 0ms (pre-computed)", process_id, room_id, audio_duration, audio_size)
            except Exception as duration_error:
                logger.warning("[process id: %s][room id: %s] Could not calculate audio duration: %s", process_id, room_id, duration_error)
                audio_duration = 0

            audio_stream.seek(0)

            # ── LiveKit path: publish pre-generated audio via adapter (no browser, no sd.play) ──
            adapter = get_livekit_adapter()
            if adapter:
                try:
                    audio_bytes = audio_stream.getvalue()
                    logger.info("🔌[process id: %s][room id: %s] [LIVEKIT] ai_speak_with_audio: publishing via LiveKit adapter", process_id, room_id)
                    success = adapter.play_audio(audio_bytes, format="mp3")
                    speak_end_time = datetime.now()
                    total_duration = (speak_end_time - speak_start_time).total_seconds()
                    if success:
                        logger.info("✅[process id: %s][room id: %s] [LIVEKIT] ai_speak_with_audio completed (%.2fs)", process_id, room_id, total_duration)
                    else:
                        logger.warning("⚠️[process id: %s][room id: %s] [LIVEKIT] ai_speak_with_audio adapter returned False", process_id, room_id)
                    return  # Always return — never fall through to sd.play() when adapter is set
                except Exception as lk_err:
                    logger.error("❌[process id: %s][room id: %s] [LIVEKIT] ai_speak_with_audio adapter error: %s", process_id, room_id, lk_err, exc_info=True)
                    return  # Return even on error — sd.play() would play on server speakers, not candidate


            # Fallback to system audio (only reached when no LiveKit adapter and no driver)
            logger.info("🔊[process id: %s][room id: %s] [AUDIO LOG] Playing PRE-GENERATED TTS through system audio", process_id, room_id)
            audio_stream.seek(0)
            audio = AudioSegment.from_file(audio_stream, format="mp3")
            samples = (
                np.array(audio.get_array_of_samples()).astype(np.float32) / 32768.0
            )
            try:
                sd.play(samples, audio.frame_rate)
                sd.wait()
                speak_end_time = datetime.now()
                total_duration = (speak_end_time - speak_start_time).total_seconds()
                logger.info("✅[process id: %s][room id: %s] [AUDIO LOG] AIRA system audio speech completed", process_id, room_id)
            except Exception as audio_error:
                logger.warning("❌[process id: %s][room id: %s] [AUDIO LOG] Audio playback failed: %s", process_id, room_id, audio_error)
        except Exception as e:
            logger.error("❌[process id: %s][room id: %s] [AUDIO LOG] Error in ai_speak_with_audio: %s", process_id, room_id, e)
            # Fallback: generate fresh TTS
            logger.info("🔄[process id: %s][room id: %s] Falling back to fresh TTS generation", process_id, room_id)
            ai_speak(text, driver, room_id, process_id)

    speak_task()


def prefetch_tts(text, room_id, process_id, executor):
    """
    Fire-and-forget TTS pre-generation in a background thread.
    Returns a Future that resolves to an io.BytesIO audio stream.
    Use this to overlap TTS generation with other work (e.g. video checks)
    so the audio is ready by the time we need to speak.

    NOTE: When Cartesia is active, prefetch is skipped entirely.
    Cartesia TTFA is ~300ms, so prefetching 5-8s of OpenAI audio that
    gets thrown away is pure waste.
    """
    if USE_CARTESIA:
        # Cartesia intercepts all speak calls — prefetch bytes are never used.
        # Return a no-op future immediately so callers still get a valid Future
        # but the pre_gen_audio will be None and Cartesia will be called live.
        import concurrent.futures
        f = concurrent.futures.Future()
        f.set_result(None)
        logger.info(
            "⚡[process id: %s][room id: %s] [PREFETCH] Cartesia active — skipping OpenAI TTS prefetch for: %s",
            process_id, room_id, text[:80] + "..." if len(text) > 80 else text
        )
        return f

    def _generate():
        try:
            tts_start = time.time()
            with _TTS_SEMAPHORE:  # cap concurrent TTS calls to prevent OpenAI connection stalls
                response = openai_client.audio.speech.create(
                    model="tts-1",
                    voice="nova",
                    input=text,
                    response_format="mp3",
                    speed=1,
                )
            audio_stream = io.BytesIO(response.content)
            audio_stream.seek(0)
            elapsed = time.time() - tts_start
            logger.info(
                "⚡[process id: %s][room id: %s] [PREFETCH] TTS pre-generated in %.0fms (%d bytes) for: %s",
                process_id, room_id, elapsed * 1000, len(response.content),
                text[:80] + "..." if len(text) > 80 else text
            )
            return audio_stream
        except Exception as e:
            logger.error(
                "❌[process id: %s][room id: %s] [PREFETCH] TTS pre-generation failed: %s",
                process_id, room_id, e
            )
            return None

    future = executor.submit(_generate)
    logger.info(
        "🚀[process id: %s][room id: %s] [PREFETCH] TTS pre-generation fired for: %s",
        process_id, room_id, text[:80] + "..." if len(text) > 80 else text
    )
    return future


def chain_prefetch_to_planner(planner_future, room_id, process_id, tts_executor):
    """
    Chain TTS pre-generation to a planner future via done-callback.
    
    When the planner resolves with a next question, immediately fire TTS in
    tts_executor — this happens DURING ACK playback, not after it finishes.
    
    Returns a dict with:
      - "tts_future": the TTS Future (may still be in-flight or None)
    
    The dict is updated in-place by the callback, so the caller can check
    chained_prefetch["tts_future"] at any later point.
    """
    result_holder = {"tts_future": None}

    def _on_planner_done(future):
        try:
            planner_result = future.result()
            if planner_result is None:
                return

            # Extract question text from planner result
            question_text = None
            if isinstance(planner_result, dict):
                question_text = planner_result.get("next_question", "")

            if question_text and question_text.strip():
                tts_future = prefetch_tts(question_text, room_id, process_id, tts_executor)
                result_holder["tts_future"] = tts_future
                logger.info(
                    "🔗[process id: %s][room id: %s] [CHAIN] TTS prefetch auto-fired from planner callback",
                    process_id, room_id
                )
        except Exception as e:
            logger.warning(
                "⚠️[process id: %s][room id: %s] [CHAIN] Planner callback failed: %s",
                process_id, room_id, e
            )

    planner_future.add_done_callback(_on_planner_done)
    return result_holder


def generate_ai_response(text,room_id,process_id):
    """Generate TTS audio using OpenAI's TTS API. Returns audio bytes as a generator."""
    try:
        with _TTS_SEMAPHORE:  # cap concurrent TTS calls to prevent OpenAI connection stalls
            response = openai_client.audio.speech.create(
                model="tts-1",
                voice="nova",
                input=text,
                response_format="mp3",
                speed=1,
            )
        # OpenAI returns the full audio content; yield it as a single chunk
        yield response.content
    except Exception as e:
        logger.error("[process id: %s][room id: %s] Error generating AI response (OpenAI TTS): %s",process_id,room_id, e)
        raise
