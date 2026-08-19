#!/usr/bin/env python3
"""
generate_cached_audio.py — Offline renderer for AIRA's cached-speech banks (Task 2).

This is the ONLY place Cartesia (TTS) is spent for reusable speech. Everything it
produces plays straight from memory at interview time (zero live TTS, zero slot
contention), so its cost amortizes across every future interview.

It renders four banks, each = a manifest.json + raw pcm_s16le/24kHz/mono clips that
pcm_clip_bank.PcmClipBank loads:

    fillers  →  assets/fillers/     (masks key-pool waits; FillerBank)
    acks     →  assets/acks/        (per intent/quality bucket; AckBank)
    script   →  assets/script/      (fixed intro/warning/closing lines; ScriptBank)
    banks    →  banks_audio/        (one clip per banks/*.json question id; QuestionBank)

Source text is content-file driven so copy can be edited without touching code:
    assets/acks/ack_texts.json      {"buckets": {bucket: [text, ...]}}
    assets/script/script_texts.json {"lines":   {line_id: text}}
    assets/fillers/filler_texts.json{"phases":  {phase:  [text, ...]}}   (optional;
        falls back to re-rendering the texts already in assets/fillers/manifest.json)
    banks/*.json                    curated questions, each {"id", "question"}

Usage:
    python scripts/generate_cached_audio.py --all
    python scripts/generate_cached_audio.py --acks
    python scripts/generate_cached_audio.py --banks --dry-run   # silent PCM, no TTS spend

--dry-run writes short silent clips + full manifests so the wiring/manifests can be
validated end to end without calling Cartesia.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import json
import logging
import os
import re
import sys
import uuid

# repo root on path + .env loaded (keys/voice come from .env, same as live)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"), override=False)
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("generate_cached_audio")

# ── Cartesia config — MUST match cartesia_tts.py so cached == live voice ─────────
SAMPLE_RATE = 24_000
VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "b7d50908-b17c-442d-ad8d-810c63997ed9")
MODEL = os.environ.get("CARTESIA_MODEL", "sonic-2")
SPEED = os.environ.get("CARTESIA_SPEED", "slow")
OUTPUT_FORMAT = {"container": "raw", "encoding": "pcm_s16le", "sample_rate": SAMPLE_RATE}

ASSETS = os.path.join(_ROOT, "assets")
BANKS_DIR = os.path.join(_ROOT, "banks")
BANKS_AUDIO_DIR = os.path.join(_ROOT, "banks_audio")


def _voice_spec():
    spec = {"mode": "id", "id": VOICE_ID}
    if SPEED and SPEED != "normal":
        spec["__experimental_controls"] = {"speed": SPEED}
    return spec


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:40].rstrip("_") or "clip"


def _duration_ms(pcm: bytes) -> int:
    # pcm_s16le mono → 2 bytes/sample
    return int(len(pcm) / (SAMPLE_RATE * 2) * 1000)


# ── synthesis ────────────────────────────────────────────────────────────────
async def _synth_async(text: str, api_key: str) -> bytes:
    """Text → Cartesia WS → accumulated raw PCM bytes (offline, not streamed to LiveKit)."""
    from cartesia import AsyncCartesia
    client = AsyncCartesia(api_key=api_key)
    out = bytearray()
    voice = _voice_spec()
    try:
        async with client.tts.websocket_connect() as ws:
            ctx = ws.context(context_id=str(uuid.uuid4()), model_id=MODEL,
                             voice=voice, output_format=OUTPUT_FORMAT)
            await ctx.send(model_id=MODEL, transcript=text, voice=voice,
                           output_format=OUTPUT_FORMAT, continue_=False)
            async for event in ctx.receive():
                if event.type == "done":
                    break
                if event.type == "error":
                    raise RuntimeError(f"Cartesia error: {event}")
                raw = getattr(event, "data", None)
                if raw:
                    out += base64.b64decode(raw)
    finally:
        try:
            await client.close()
        except Exception:
            pass
    return bytes(out)


def synthesize_pcm(text: str, *, dry_run: bool = False) -> bytes:
    """Return raw PCM for `text`. In dry-run, return ~0.6s of silence (no TTS spend)."""
    if dry_run:
        return b"\x00\x00" * int(SAMPLE_RATE * 0.6)
    api_key = (os.environ.get("CARTESIA_API_KEY", "").split(",")[0] or "").strip()
    if not api_key:
        raise SystemExit("CARTESIA_API_KEY not set — cannot render (use --dry-run to test wiring).")
    return asyncio.run(_synth_async(text, api_key))


# ── generic grouped-bank renderer (fillers / acks) ───────────────────────────
def render_grouped(out_dir: str, group_key: str, groups: dict, *, dry_run: bool) -> int:
    """Render {group: [texts]} into out_dir/<group>/<slug>.pcm + manifest.json.

    Clip id = "<group>_<slug>"; the manifest tags each clip with `group_key` so the
    matching PcmClipBank subclass buckets them correctly.
    """
    os.makedirs(out_dir, exist_ok=True)
    clips = []
    n = 0
    for group, texts in groups.items():
        gdir = os.path.join(out_dir, group)
        os.makedirs(gdir, exist_ok=True)
        for text in texts:
            if not text or not text.strip():
                continue
            slug = _slug(text)
            rel = os.path.join(group, f"{slug}.pcm")
            pcm = synthesize_pcm(text, dry_run=dry_run)
            with open(os.path.join(out_dir, rel), "wb") as f:
                f.write(pcm)
            clips.append({
                "id": f"{group}_{slug}",
                group_key: group,
                "text": text,
                "path": rel,
                "duration_ms": _duration_ms(pcm),
            })
            n += 1
            logger.info("  ✅ %s/%s (%dms)", group, slug, _duration_ms(pcm))
    manifest = {"sample_rate": SAMPLE_RATE, "voice_id": VOICE_ID, "clips": clips}
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    logger.info("📦 wrote %d clip(s) + manifest → %s", n, out_dir)
    return n


# ── content loaders ──────────────────────────────────────────────────────────
def _load_ack_groups() -> dict:
    with open(os.path.join(ASSETS, "acks", "ack_texts.json")) as f:
        return json.load(f)["buckets"]


def _load_filler_groups() -> dict:
    """Prefer assets/fillers/filler_texts.json; else reconstruct from the existing manifest."""
    ftexts = os.path.join(ASSETS, "fillers", "filler_texts.json")
    if os.path.exists(ftexts):
        with open(ftexts) as f:
            return json.load(f)["phases"]
    manifest = os.path.join(ASSETS, "fillers", "manifest.json")
    groups: dict = {}
    if os.path.exists(manifest):
        with open(manifest) as f:
            for clip in json.load(f).get("clips", []):
                groups.setdefault(clip.get("phase", "neutral"), []).append(clip.get("text", ""))
    return groups


def _load_script_lines() -> dict:
    path = os.path.join(ASSETS, "script", "script_texts.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f).get("lines", {})


# ── script bank (fixed lines, keyed by line id — no buckets) ─────────────────
def render_script(lines: dict, *, dry_run: bool) -> int:
    out_dir = os.path.join(ASSETS, "script")
    os.makedirs(out_dir, exist_ok=True)
    clips = []
    for line_id, text in lines.items():
        if not text or not text.strip():
            continue
        rel = f"{line_id}.pcm"
        pcm = synthesize_pcm(text, dry_run=dry_run)
        with open(os.path.join(out_dir, rel), "wb") as f:
            f.write(pcm)
        clips.append({"id": line_id, "line": line_id, "text": text,
                      "path": rel, "duration_ms": _duration_ms(pcm)})
        logger.info("  ✅ script/%s (%dms)", line_id, _duration_ms(pcm))
    manifest = {"sample_rate": SAMPLE_RATE, "voice_id": VOICE_ID, "clips": clips}
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    logger.info("📦 wrote %d script line(s) + manifest → %s", len(clips), out_dir)
    return len(clips)


def _pcm_to_mp3(pcm: bytes) -> bytes:
    """Encode raw pcm_s16le/24k/mono → MP3 (compressed ~10-20x for Redis storage)."""
    import io
    from pydub import AudioSegment
    seg = AudioSegment(data=pcm, sample_width=2, frame_rate=SAMPLE_RATE, channels=1)
    buf = io.BytesIO()
    seg.export(buf, format="mp3", bitrate="64k")
    return buf.getvalue()


# ── bank-question audio (one clip per banks/*.json entry id) ─────────────────
# REFERENCE PRODUCER. The real per-job question_bank (with skill-combo groups) is authored
# by the campus backend; this renders the LOCAL per-skill banks/*.json as a concrete example
# of the schema + the Redis storage the engine consumes (question_bank_cache.QuestionBankPlan).
def render_banks(*, dry_run: bool, to_redis: bool = False, job_id: str = "local") -> int:
    os.makedirs(BANKS_AUDIO_DIR, exist_ok=True)
    clips = []
    groups = []            # question_bank schema (one group per skill bank)
    cache = None
    if to_redis and not dry_run:
        from question_bank_cache import get_audio_cache
        cache = get_audio_cache()
        if not cache.available:
            logger.error("--redis given but REDIS_URL unset/redis unavailable; storing files only")
            cache = None
    for path in sorted(glob.glob(os.path.join(BANKS_DIR, "*.json"))):
        bank_name = os.path.splitext(os.path.basename(path))[0]
        with open(path) as f:
            data = json.load(f)
        bdir = os.path.join(BANKS_AUDIO_DIR, bank_name)
        os.makedirs(bdir, exist_ok=True)
        gskill = data.get("skill", bank_name)
        group = {"key": bank_name, "skills": [gskill], "questions": []}
        for level, tiers in (data.get("levels", {}) or {}).items():
            for tier, entries in (tiers or {}).items():
                for entry in entries:
                    eid = entry.get("id")
                    q = entry.get("question")
                    if not eid or not q:
                        continue
                    rel = os.path.join(bank_name, f"{eid}.pcm")
                    pcm = synthesize_pcm(q, dry_run=dry_run)
                    with open(os.path.join(BANKS_AUDIO_DIR, rel), "wb") as f:
                        f.write(pcm)
                    audio_key = f"qbank:{job_id}:{bank_name}:{eid}"
                    if cache is not None:
                        cache.put_audio(audio_key, _pcm_to_mp3(pcm))
                    clips.append({"id": eid, "bank": bank_name, "skill": gskill,
                                  "level": level, "tier": tier, "text": q,
                                  "path": rel, "duration_ms": _duration_ms(pcm)})
                    group["questions"].append({"id": eid, "text": q,
                                               "audio_key": audio_key, "rubric": entry.get("rubric", "")})
                    logger.info("  ✅ %s/%s (%dms)%s", bank_name, eid, _duration_ms(pcm),
                                " → redis" if cache is not None else "")
        if group["questions"]:
            groups.append(group)
    manifest = {"sample_rate": SAMPLE_RATE, "voice_id": VOICE_ID, "clips": clips}
    with open(os.path.join(BANKS_AUDIO_DIR, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    # Emit the question_bank the /claim response would carry (text + Redis keys, no audio bytes).
    question_bank = {"sample_rate": SAMPLE_RATE, "job_id": job_id, "groups": groups}
    with open(os.path.join(BANKS_AUDIO_DIR, "question_bank.reference.json"), "w") as f:
        json.dump(question_bank, f, indent=2, ensure_ascii=False)
    logger.info("📦 wrote %d bank-question clip(s) + manifest + question_bank.reference.json → %s",
                len(clips), BANKS_AUDIO_DIR)
    return len(clips)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="render every bank")
    ap.add_argument("--fillers", action="store_true")
    ap.add_argument("--acks", action="store_true")
    ap.add_argument("--script", action="store_true")
    ap.add_argument("--banks", action="store_true")
    ap.add_argument("--redis", action="store_true",
                    help="with --banks: also upload question audio (MP3) to Redis + emit question_bank.reference.json")
    ap.add_argument("--job-id", default="local", help="job id for the reference question_bank + Redis keys")
    ap.add_argument("--dry-run", action="store_true", help="write silent PCM instead of calling Cartesia")
    args = ap.parse_args()

    do = {"fillers": args.fillers, "acks": args.acks, "script": args.script, "banks": args.banks}
    if args.all or not any(do.values()):
        do = {k: True for k in do}

    if args.dry_run:
        logger.info("🌵 DRY RUN — writing silent PCM, no Cartesia calls")

    total = 0
    if do["fillers"]:
        logger.info("── fillers ──")
        total += render_grouped(os.path.join(ASSETS, "fillers"), "phase", _load_filler_groups(), dry_run=args.dry_run)
    if do["acks"]:
        logger.info("── acks ──")
        total += render_grouped(os.path.join(ASSETS, "acks"), "bucket", _load_ack_groups(), dry_run=args.dry_run)
    if do["script"]:
        logger.info("── script ──")
        lines = _load_script_lines()
        if lines:
            total += render_script(lines, dry_run=args.dry_run)
        else:
            logger.warning("no assets/script/script_texts.json — skipping script bank")
    if do["banks"]:
        logger.info("── bank questions ──")
        total += render_banks(dry_run=args.dry_run, to_redis=args.redis, job_id=args.job_id)

    logger.info("🎉 done — %d clip(s) rendered", total)


if __name__ == "__main__":
    main()
