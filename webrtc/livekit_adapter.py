"""LiveKit adapter registry — the orchestrator injects the per-interview adapter
here (set_livekit_adapter) and every audio function reads it back
(get_livekit_adapter). Split out of webrtc.interview for modularity."""
import os
import logging

logger = logging.getLogger("livekit_adapter")

_livekit_adapter = None  # Set by orchestrator via set_livekit_adapter()

# ════════════════════════════════════════════════════════
# Cartesia TTS flag — set CARTESIA_API_KEY in env to enable
# When active, all bot speech uses Cartesia WS streaming
# instead of OpenAI TTS-1. TTFA: ~300ms vs 1-2s.
# ════════════════════════════════════════════════════════
USE_CARTESIA = bool(os.getenv("CARTESIA_API_KEY", ""))

def set_livekit_adapter(adapter):
    """Called by orchestrator to inject the LiveKit adapter for this interview.
    Setting an adapter automatically enables the LiveKit path."""
    global _livekit_adapter
    _livekit_adapter = adapter
    if adapter is not None:
        os.environ["AIRA_USE_LIVEKIT"] = "true"
    logger.info("🔌 LiveKit adapter set (adapter=%s)", "active" if adapter else "None")

def get_livekit_adapter():
    """Get the current LiveKit adapter (or None if not using LiveKit).
    Checks env at call time so it respects runtime changes."""
    if _livekit_adapter is not None and os.getenv("AIRA_USE_LIVEKIT", "false").lower() == "true":
        return _livekit_adapter
    return None
