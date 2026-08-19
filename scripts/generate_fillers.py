"""
generate_fillers.py — one-time offline renderer for the filler bank.

Renders a curated set of short, content-neutral interviewer lines to raw PCM using
the SAME Cartesia voice/model/format as cartesia_tts.py, so the fillers sound like the
same interviewer. Run once, and re-run whenever CARTESIA_VOICE_ID changes:

    python generate_fillers.py

Writes:
    assets/fillers/<phase>/<slug>.pcm     (raw pcm_s16le, 24kHz mono)
    assets/fillers/manifest.json

Keep the lines PHASE-appropriate and CONTENT-NEUTRAL — a filler must never contradict
the question that follows, and must never sound like an error ("we're having issues").
It's just a natural interviewer beat while the next question is prepared.
"""

# --- repo-root on sys.path (script lives in scripts/) ---
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import asyncio
import base64
import json
import os
import re
import uuid

from dotenv import load_dotenv
load_dotenv(override=True)

# Reuse the exact TTS config so fillers match the live voice.
from cartesia_tts import (
    CARTESIA_API_KEY, CARTESIA_MODEL, CARTESIA_SAMPLE_RATE, _OUTPUT_FORMAT, _voice_spec,
)

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fillers")

# Phase → filler lines. Add/edit freely; re-run to regenerate.
FILLERS = {
    "opening": [
        "Alright, let's get started.",
        "Okay, let me pull up the first question for you.",
        "Great — let's dive in.",
        "Perfect, let's begin.",
    ],
    "mid": [
        "Let me think of a good one for you.",
        "Okay, give me just a second here.",
        "Alright, let me line up the next question.",
        "Mm, let me put together the next one.",
        "Good — let me think about where to take this next.",
        "Okay, one moment while I get the next question ready.",
    ],
    "closing": [
        "Alright, we're almost there — let me get the last one.",
        "Okay, just one more coming up.",
        "Great, let me wrap up with a final question.",
    ],
    "neutral": [
        "Okay, one moment.",
        "Let me think about that for a second.",
        "Alright, just a moment.",
        "Mm, give me a second here.",
    ],
    # "wait" fillers are used when the wait is LONGER (deep queue) — after a first
    # normal filler already played. Explicit, calm brief-hold lines so the pause
    # reads as intentional, never like something is broken.
    "wait": [
        "Just a moment, please — bear with me.",
        "Hang on one second, almost ready.",
        "Thanks for your patience, just a moment more.",
        "Give me one more second here.",
        "Bear with me — I'll be right with you.",
    ],
    # "waiting" fillers are the PRE-INTERVIEW holding pattern: when many
    # interviews start at once and the Cartesia pool is saturated, these play
    # (spaced out, every ~15-20s) before the greeting so the candidate is never
    # left in silence. Deliberately time-agnostic — never promise a start time.
    "waiting": [
        "Thanks for joining — we're just getting things ready for you. Please stay with us, we'll begin in a moment.",
        "Hang tight, we're setting up your interview now. It'll just be a moment — thanks for your patience.",
        "Almost ready — please stay on the line, and do make sure your camera and microphone are on. We'll start shortly.",
        "Thanks for waiting. We're preparing your session and will get started very soon.",
        "Just a little longer — we're finalizing a few things and will begin your interview shortly. Thanks for bearing with us.",
    ],
}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40]


async def _render(client, text: str) -> bytes:
    """Synthesize one line → concatenated raw PCM bytes."""
    voice = _voice_spec()
    pcm = bytearray()
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
        async for event in ctx.receive():
            if event.type == "done":
                break
            if event.type == "error":
                raise RuntimeError(f"cartesia error: {event}")
            raw = getattr(event, "data", None)
            if raw:
                pcm.extend(base64.b64decode(raw))
    return bytes(pcm)


async def main():
    if not CARTESIA_API_KEY:
        raise SystemExit("❌ CARTESIA_API_KEY not set — cannot render fillers")

    from cartesia import AsyncCartesia
    key = CARTESIA_API_KEY.split(",")[0].strip()  # first key if a comma-list
    client = AsyncCartesia(api_key=key)

    clips = []
    try:
        for phase, sentences in FILLERS.items():
            os.makedirs(os.path.join(ASSETS, phase), exist_ok=True)
            for text in sentences:
                slug = _slug(text)
                rel = f"{phase}/{slug}.pcm"
                pcm = await _render(client, text)
                with open(os.path.join(ASSETS, rel), "wb") as f:
                    f.write(pcm)
                dur_ms = int(len(pcm) / (CARTESIA_SAMPLE_RATE * 2) * 1000)
                clips.append({
                    "id": f"{phase}_{slug}",
                    "phase": phase,
                    "text": text,
                    "path": rel,
                    "duration_ms": dur_ms,
                })
                print(f"  ✓ [{phase:8}] {dur_ms:>5}ms  {text}")
    finally:
        close = getattr(client, "close", None)
        if close:
            try:
                await close()
            except Exception:
                pass

    os.makedirs(ASSETS, exist_ok=True)
    with open(os.path.join(ASSETS, "manifest.json"), "w") as f:
        json.dump({
            "sample_rate": CARTESIA_SAMPLE_RATE,
            "voice_id": os.getenv("CARTESIA_VOICE_ID", ""),
            "clips": clips,
        }, f, indent=2)
    print(f"\n✅ wrote {len(clips)} clips + manifest to {ASSETS}")


if __name__ == "__main__":
    asyncio.run(main())
