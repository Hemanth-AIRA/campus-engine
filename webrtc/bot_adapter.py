"""
bot_audio_adapter.py

Synchronous adapter wrapping the async AiraBotClient.
Drop-in replacement for browser-based audio functions in webrtc_interview.py.

The interface this exposes is the CONTRACT between the 9000-line interview
engine and whatever transport layer is underneath. These method signatures
MUST NEVER CHANGE:

    adapter.record_transcribe(silence_ms, max_ms) -> dict
    adapter.play_audio(audio_bytes, format)       -> None
    adapter.play_with_bargein(audio_bytes, ...)    -> dict
    adapter.speak_cartesia(text)                   -> bool
    adapter.speak_cartesia_tokens(token_iter)      -> bool
    adapter.close()                               -> None

The backend can be LiveKit, a WebSocket bridge, or a mock for testing.
The interface is stable. This is what makes the transport replaceable
without touching interview logic.
"""

import asyncio
import logging
import os
import threading
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class BotAudioAdapter:
    """
    Sync wrapper around AiraBotClient for use in synchronous code.

    Runs its own asyncio event loop in a background daemon thread.
    All public methods are synchronous — they block until the async
    operation completes.

    Usage:
        bot = AiraBotClient(url, room_id, token)
        adapter = BotAudioAdapter(bot)
        adapter.connect()

        # Drop-in replacements:
        result = adapter.record_transcribe(silence_ms=3000, max_ms=120000)
        adapter.play_audio(mp3_bytes, format="mp3")
        result = adapter.play_with_bargein(mp3_bytes, format="mp3")

        # Cartesia TTS (lower latency):
        adapter.speak_cartesia("Hello world")
        adapter.speak_cartesia_tokens(gpt_token_generator)

        adapter.close()
    """

    def __init__(self, bot_client):
        """
        Args:
            bot_client: An AiraBotClient instance (not yet connected).
        """
        self.bot = bot_client
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name=f"bot-audio-{bot_client.room_id}",
        )
        self._thread.start()
        logger.info(
            "[room %s] BotAudioAdapter initialized with background event loop",
            bot_client.room_id,
        )

    def _run(self, coro, timeout: float = 300):
        """Run a coroutine on the background loop and block until done."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # =========================================================
    # CONNECTION -- replaces setup_selenium_driver()
    # =========================================================

    def connect(self):
        """Connect the bot to the LiveKit room. Blocks until connected."""
        self._run(self.bot.connect(), timeout=30)
        logger.info("[room %s] Adapter connected", self.bot.room_id)

    # =========================================================
    # RECORDING -- replaces record_transcribe_webrtc_*()
    # =========================================================

    def record_transcribe(
        self,
        silence_ms: int = 3000,
        max_ms: int = 120_000,
        silence_rms: float = 0.015,
        min_active_ms: int = 500,
        early_callback=None,
    ) -> dict:
        """
        Record candidate audio with server-side VAD, transcribe via Deepgram.

        Uses simultaneous record+stream: Deepgram WS is opened BEFORE recording
        starts so frames are transcribed in real-time. Transcript is ready
        ~instantly when VAD stops (vs 3-9s batch post-recording delay).

        early_callback: optional callable(partial_transcript: str).
            Fired when DG is_final is received AND 1s of VAD silence has elapsed.
            Use this to start GPT/Cartesia streaming immediately -- the remaining
            2s of VAD silence is free time to prefetch the ack.

        Returns:
            {"answer": str, "answer_duration": float}   on success
            {}                                           on failure
        """
        try:
            transcript = self._run(
                self.bot.record_and_transcribe_realtime(
                    silence_ms=silence_ms,
                    max_ms=max_ms,
                    silence_threshold=silence_rms,
                    min_speech_ms=min_active_ms,
                    early_callback=early_callback,
                ),
                timeout=max_ms / 1000 + 60,
            )

            if not transcript or not transcript.strip():
                logger.warning(
                    "[room %s] Deepgram returned empty transcript",
                    self.bot.room_id,
                )
                return {}

            return {
                "answer": transcript,
                "answer_duration": silence_ms / 1000,
            }

        except Exception as e:
            logger.error(
                "[room %s] record_transcribe failed: %s",
                self.bot.room_id, e, exc_info=True,
            )
            return {}

    # =========================================================
    # CARTESIA TTS -- streaming synthesis -> LiveKit
    # =========================================================

    def speak_cartesia(self, text: str) -> bool:
        """
        Synthesise text via Cartesia WS, stream PCM directly to LiveKit.

        TTFA ~200-300ms vs OpenAI TTS 1-2s.
        Drop-in for play_audio() when you have text (not pre-rendered audio).
        """
        try:
            self._run(self.bot.speak_cartesia(text), timeout=300)
            return True
        except Exception as e:
            logger.error(
                "[room %s] speak_cartesia failed: %s",
                self.bot.room_id, e, exc_info=True,
            )
            return False

    def speak_cartesia_tokens(self, token_iter, on_first_audio=None) -> bool:
        """
        Stream GPT tokens word-by-word -> Cartesia WS -> PCM -> LiveKit.

        Candidate hears audio within ~300ms of first GPT token.
        token_iter: sync or async iterable of str tokens from ChatCompletions stream.
        on_first_audio: optional callable(), fired when first PCM chunk arrives.
        """
        try:
            self._run(
                self.bot.speak_cartesia_tokens(
                    token_iter, on_first_audio=on_first_audio
                ),
                timeout=300,
            )
            return True
        except Exception as e:
            logger.error(
                "[room %s] speak_cartesia_tokens failed: %s",
                self.bot.room_id, e, exc_info=True,
            )
            return False

    # =========================================================
    # PLAYBACK -- replaces play_audio_in_webrtc(), ai_speak*()
    # =========================================================

    def play_audio(
        self,
        audio_bytes: bytes,
        format: str = "mp3",
    ) -> bool:
        """
        Play TTS audio to the candidate via LiveKit track.

        Drop-in for:
            play_audio_in_webrtc()     (L1759-1898)
            ai_speak()                 (L8989-9068)
            ai_speak_with_audio()      (L9071+)
            ai_speak_streamed_text()   (L8753+)
            ai_speak_streaming()       (L8541+)

        Returns True on success (matches play_audio_in_webrtc return type).
        """
        try:
            self._run(
                self.bot.play_audio(audio_bytes, format=format),
                timeout=300,
            )
            return True
        except Exception as e:
            logger.error(
                "[room %s] play_audio failed: %s",
                self.bot.room_id, e, exc_info=True,
            )
            return False

    def play_cached_clip(self, pcm: bytes, sample_rate: int = 24000, clip_id: str = "") -> bool:
        """Play a pre-rendered PCM clip (cached ack / question) from memory to the
        candidate. Task 2: no Cartesia slot is used. Returns True if it played."""
        try:
            return bool(self._run(
                self.bot.play_cached_clip(pcm, sample_rate, clip_id),
                timeout=120,
            ))
        except Exception as e:
            logger.error(
                "[room %s] play_cached_clip failed: %s",
                self.bot.room_id, e, exc_info=True,
            )
            return False

    # =========================================================
    # BARGE-IN -- replaces play_ack_with_bargein()
    # =========================================================

    def play_with_bargein(
        self,
        audio_bytes: bytes,
        format: str = "mp3",
        bargein_rms: float = 0.03,
        bargein_ms: int = 300,
    ) -> dict:
        """
        Play audio while monitoring for candidate interruption.

        Drop-in for:
            play_ack_with_bargein()  (L8004+)

        Returns:
            {"interrupted": True}  -- candidate spoke over the bot
            {"interrupted": False} -- playback completed normally
        """
        try:
            return self._run(
                self.bot.play_with_bargein(
                    audio_bytes,
                    format=format,
                    bargein_rms_threshold=bargein_rms,
                    bargein_sustained_ms=bargein_ms,
                ),
                timeout=300,
            )
        except Exception as e:
            logger.error(
                "[room %s] play_with_bargein failed: %s",
                self.bot.room_id, e, exc_info=True,
            )
            return {"interrupted": False}

    # =========================================================
    # UTILITY
    # =========================================================

    def set_filler_phase(self, question_no, total=None) -> None:
        """Set which filler bucket to use next (opening / mid / closing).

        Delegates to the underlying AiraBotClient.set_filler_phase().
        Called by the interview flow before each question so the bot plays
        phase-appropriate filler audio while waiting for a Cartesia key slot.
        """
        self.bot.set_filler_phase(question_no, total)

    def release_speak_lease(self) -> None:
        """Release the held Cartesia turn-speech lease.

        Delegates to AiraBotClient.release_speak_lease(). The interview flow calls
        this on the adapter at the listen boundary (_capture_with_speculative_reflex),
        so the ack+question lease is freed each turn instead of being held for the
        whole interview.
        """
        self.bot.release_speak_lease()

    def stop_playback(self):
        """Immediately stop any in-progress playback."""
        self.bot.stop_playback()

    @property
    def is_connected(self) -> bool:
        return self.bot.is_connected

    @property
    def candidate_present(self) -> bool:
        return self.bot.candidate_present

    def close(self):
        """Clean up the bot connection and background thread."""
        try:
            self._run(self.bot.close(), timeout=10)
        except Exception as e:
            logger.warning(
                "[room %s] Error during adapter close: %s",
                self.bot.room_id, e,
            )
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        logger.info("[room %s] Adapter closed", self.bot.room_id)


# =========================================================
# FACTORY -- replaces setup_selenium_driver()
# =========================================================

def create_adapter_from_env(room_id: str, process_id: int = 0) -> Tuple:
    """
    Factory function: creates and connects a bot client from environment vars.

    Call this in interview orchestration INSTEAD of setup_selenium_driver().

    Returns:
        (bot_client, adapter) tuple

    Requires env:
        LIVEKIT_URL           -- LiveKit server URL (wss://...)
        AIRA_CONNECT_URL      -- Aira-Connect server URL (for token minting)
        AIRA_BOT_JWT          -- Existing bot JWT for auth against Connect server

    Example:
        if USE_LIVEKIT:
            bot, adapter = create_adapter_from_env(room_id)
            # use adapter.record_transcribe(), adapter.play_audio(), etc.
        else:
            driver = setup_selenium_driver(room_url)
    """
    from aira_bot_client import AiraBotClient

    livekit_url = os.environ.get("LIVEKIT_URL", "")
    if not livekit_url:
        raise ValueError("LIVEKIT_URL environment variable not set")

    bot_token = _fetch_bot_token(room_id)

    bot = AiraBotClient(
        livekit_url=livekit_url,
        room_id=room_id,
        bot_token=bot_token,
    )

    adapter = BotAudioAdapter(bot)
    adapter.connect()

    logger.info(
        "[process %d][room %s] LiveKit bot adapter ready (no browser)",
        process_id, room_id,
    )
    return bot, adapter


def _fetch_bot_token(room_id: str) -> str:
    """
    Fetch a LiveKit token for the bot from the Aira-Connect server.

    Uses the existing JWT auth mechanism -- no new auth system needed.
    The Connect server mints a LiveKit token after verifying the bot's
    existing Aira JWT.
    """
    import requests

    connect_url = os.environ.get("AIRA_CONNECT_URL", "")
    if not connect_url:
        return _generate_token_locally(room_id)

    bot_jwt = os.environ.get("AIRA_BOT_JWT", "")

    try:
        response = requests.post(
            f"{connect_url.rstrip('/')}/api/livekit-token",
            json={
                "roomId": room_id,
                "identity": f"bot-{room_id}",
                "role": "bot",
                "name": "AIRA Interviewer",
            },
            headers={
                "Authorization": f"Bearer {bot_jwt}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        return data["token"]

    except Exception as e:
        logger.warning(
            "Failed to fetch LiveKit token from Connect server: %s. "
            "Falling back to local token generation.",
            e,
        )
        return _generate_token_locally(room_id)


def _generate_token_locally(room_id: str) -> str:
    """
    Generate a LiveKit token locally using the API key/secret.
    Fallback when the Connect server is unreachable.
    """
    from livekit import api as lk_api

    api_key = os.environ.get("LIVEKIT_API_KEY", "")
    api_secret = os.environ.get("LIVEKIT_API_SECRET", "")

    if not api_key or not api_secret:
        raise ValueError(
            "Cannot generate LiveKit token: "
            "LIVEKIT_API_KEY and LIVEKIT_API_SECRET required"
        )

    token = (
        lk_api.AccessToken(api_key, api_secret)
        .with_identity(f"bot-{room_id}")
        .with_name("AIRA Interviewer")
        .with_grants(
            lk_api.VideoGrants(
                room_join=True,
                room=room_id,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .to_jwt()
    )
    return token
#dummy push this is new file