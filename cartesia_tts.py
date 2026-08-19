"""
cartesia_tts.py

Cartesia WebSocket TTS — replaces OpenAI TTS-1 for the bot.

Key functions:
  cartesia_speak_text_streaming(text, bot)   — text → Cartesia → LiveKit (no gaps)
  cartesia_speak_tokens(token_iter, bot)     — GPT tokens → Cartesia → LiveKit

Architecture (gap-free):
  Cartesia receiver  →  puts raw PCM chunks into bot._stream_buf queue
  bot drain task     →  pulls 20ms frames at fixed clock rate → capture_frame()

TTFA: ~200-400ms vs OpenAI TTS ~1-2s.
Audio: pcm_s16le 24kHz mono → matches LiveKit PLAYBACK_SAMPLE_RATE.
"""

import asyncio
import base64
import logging
import os
import time
import uuid
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CARTESIA_API_KEY  = os.environ.get("CARTESIA_API_KEY", "")
CARTESIA_VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "b7d50908-b17c-442d-ad8d-810c63997ed9")
CARTESIA_MODEL    = os.environ.get("CARTESIA_MODEL", "sonic-2")
CARTESIA_SAMPLE_RATE = 24_000
# Speed of bot speech: "slowest" | "slow" | "normal" | "fast" | "fastest"
# Set CARTESIA_SPEED=normal to restore default pace.
CARTESIA_SPEED = os.environ.get("CARTESIA_SPEED", "slow")

_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding":  "pcm_s16le",
    "sample_rate": CARTESIA_SAMPLE_RATE,
}

def _voice_spec():
    """Build voice spec with current speed setting."""
    spec = {"mode": "id", "id": CARTESIA_VOICE_ID}
    if CARTESIA_SPEED and CARTESIA_SPEED != "normal":
        spec["__experimental_controls"] = {"speed": CARTESIA_SPEED}
    return spec


# ── Public API ────────────────────────────────────────────────────────────────

async def cartesia_speak_text_streaming(
    text: str,
    bot_client,
    *,
    on_first_audio: Optional[Callable] = None,
) -> None:
    """
    Text → Cartesia WS → PCM chunks → LiveKit (gap-free queue playback).

    Uses bot_client.start_stream_to_livekit() so audio plays at a fixed
    20ms clock regardless of chunk arrival timing → no voice breaking.
    """
    if not text or not text.strip():
        return
    if not CARTESIA_API_KEY:
        logger.error("❌ [Cartesia] CARTESIA_API_KEY not set")
        return

    from cartesia import AsyncCartesia

    # Use the turn-held Cartesia lease (one slot spanning ack + question) so a filler
    # can't intrude mid-turn; it's released at the listen boundary, not here. Fall
    # back to a self-managed lease for non-adapter callers/tests. on_wait plays an
    # (escalating) filler while all slots are busy (FIFO-fair).
    _own_lease = None
    if hasattr(bot_client, "_ensure_speak_lease"):
        cart_key = await bot_client._ensure_speak_lease()
    else:
        from api_key_pool import acquire_key_async
        cart_key, _own_lease = await acquire_key_async(
            "cartesia", on_wait=getattr(bot_client, "play_filler", None)
        )

    t0 = time.time()
    first_audio = False

    # Start continuous drain task BEFORE receiving any audio
    stream_q = await bot_client.start_stream_to_livekit(sample_rate=CARTESIA_SAMPLE_RATE)

    try:
        client = AsyncCartesia(api_key=cart_key)
        voice = _voice_spec()

        # Retry WS connection up to 3 times (handshake timeouts under load)
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
                        transcript=text,
                        voice=voice,
                        output_format=_OUTPUT_FORMAT,
                        continue_=False,
                    )

                    # Receiver: put raw PCM into queue — NO blocking, NO sleep here
                    async for event in ctx.receive():
                        if event.type == "done":
                            break
                        if event.type == "error":
                            logger.error("❌ [Cartesia] error: %s", event)
                            break
                        raw = getattr(event, "data", None)
                        if not raw:
                            continue

                        pcm_chunk = base64.b64decode(raw)

                        if not first_audio:
                            first_audio = True
                            ttfa = (time.time() - t0) * 1000
                            logger.info("⚡ [Cartesia] TTFA: %.0fms | text_len=%d", ttfa, len(text))
                            if on_first_audio:
                                on_first_audio()

                        # Non-blocking enqueue → drain task handles pacing
                        stream_q.put_nowait(pcm_chunk)

                break  # success — exit retry loop

            except (asyncio.TimeoutError, TimeoutError, OSError) as ws_err:
                if attempt < max_retries:
                    logger.warning(
                        "⚠️ [Cartesia] WS connect attempt %d/%d failed: %s — retrying in 1s",
                        attempt, max_retries, ws_err,
                    )
                    await asyncio.sleep(1)
                else:
                    raise  # final attempt — let outer except handle it

    except Exception as e:
        logger.error("❌ [Cartesia] speak_text_streaming failed: %s", e, exc_info=True)
    finally:
        # Signal drain task to flush remaining audio and exit
        await bot_client.stop_stream_to_livekit()
        # Release ONLY a self-managed lease; a turn-held lease is released at listen.
        if _own_lease is not None:
            from api_key_pool import release_key
            release_key("cartesia", _own_lease)
        elapsed = (time.time() - t0) * 1000
        logger.info("✅ [Cartesia] speak done: %.0fms | text_len=%d", elapsed, len(text))


async def cartesia_speak_tokens(
    token_iter,
    bot_client,
    *,
    on_first_audio: Optional[Callable] = None,
) -> None:
    """
    GPT token stream → Cartesia WS → PCM → LiveKit (gap-free).

    token_iter: sync or async iterable of str chunks from ChatCompletions stream.
    Candidate hears audio within ~300ms of first token pushed to Cartesia.
    """
    if not CARTESIA_API_KEY:
        # Accumulate then one-shot
        parts = []
        if hasattr(token_iter, "__aiter__"):
            async for t in token_iter:
                parts.append(t)
        else:
            for t in token_iter:
                parts.append(t)
        await cartesia_speak_text_streaming("".join(parts), bot_client, on_first_audio=on_first_audio)
        return

    from cartesia import AsyncCartesia

    # Use the turn-held Cartesia lease (spans ack + question) so a filler can't
    # intrude mid-turn; released at the listen boundary, not here. Falls back to a
    # self-managed lease for non-adapter callers/tests.
    _own_lease = None
    if hasattr(bot_client, "_ensure_speak_lease"):
        cart_key = await bot_client._ensure_speak_lease()
    else:
        from api_key_pool import acquire_key_async
        cart_key, _own_lease = await acquire_key_async(
            "cartesia", on_wait=getattr(bot_client, "play_filler", None)
        )

    t0 = time.time()
    first_audio = False
    collected: list[str] = []

    stream_q = await bot_client.start_stream_to_livekit(sample_rate=CARTESIA_SAMPLE_RATE)

    try:
        client = AsyncCartesia(api_key=cart_key)
        voice = _voice_spec()
        async with client.tts.websocket_connect() as ws:
            ctx = ws.context(
                context_id=str(uuid.uuid4()),
                model_id=CARTESIA_MODEL,
                voice=voice,
                output_format=_OUTPUT_FORMAT,
            )

            # ── Sender: push GPT tokens to Cartesia ──────────────────────
            async def _push():
                buf = ""
                async_mode = hasattr(token_iter, "__aiter__")
                try:
                    if async_mode:
                        async for token in token_iter:
                            collected.append(token)
                            buf += token
                            if buf.endswith((" ", ".", ",", "!", "?", "\n")) or len(buf) > 40:
                                await ctx.push(buf)
                                buf = ""
                    else:
                        for token in token_iter:
                            collected.append(token)
                            buf += token
                            if buf.endswith((" ", ".", ",", "!", "?", "\n")) or len(buf) > 40:
                                await ctx.push(buf)
                                buf = ""
                    if buf:
                        await ctx.push(buf)
                except Exception as e:
                    logger.error("❌ [Cartesia] token push error: %s", e)
                finally:
                    await ctx.no_more_inputs()

            # ── Receiver: enqueue PCM chunks (non-blocking) ───────────────
            async def _recv():
                nonlocal first_audio
                async for event in ctx.receive():
                    if event.type == "done":
                        break
                    if event.type == "error":
                        logger.error("❌ [Cartesia] error: %s", event)
                        break
                    raw = getattr(event, "data", None)
                    if not raw:
                        continue
                    pcm_chunk = base64.b64decode(raw)
                    if not first_audio:
                        first_audio = True
                        ttfa = (time.time() - t0) * 1000
                        logger.info("⚡ [Cartesia] token-stream TTFA: %.0fms", ttfa)
                        if on_first_audio:
                            on_first_audio()
                    stream_q.put_nowait(pcm_chunk)  # non-blocking

            await asyncio.gather(_push(), _recv())

    except Exception as e:
        logger.error("❌ [Cartesia] speak_tokens failed: %s", e, exc_info=True)
        if collected:
            # Fallback: drain queue + speak accumulated text fresh
            await bot_client.stop_stream_to_livekit()
            await cartesia_speak_text_streaming("".join(collected), bot_client)
            return
    finally:
        await bot_client.stop_stream_to_livekit()
        # Release ONLY a self-managed lease; a turn-held lease is released at listen.
        if _own_lease is not None:
            from api_key_pool import release_key
            release_key("cartesia", _own_lease)
        elapsed = (time.time() - t0) * 1000
        logger.info("✅ [Cartesia] token-stream done: %.0fms", elapsed)
