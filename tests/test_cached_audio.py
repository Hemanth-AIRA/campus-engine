#!/usr/bin/env python3
"""Unit tests for the cached-speech banks (Task 2) — pcm_clip_bank, ack_audio, filler_audio.

Hermetic: builds a synthetic manifest + silent PCM in a temp dir so it does not depend on
rendered assets or Cartesia. Covers bucket routing, no-immediate-repeat, fallback chains,
keyed lookup, and graceful degradation when a bank is missing.
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcm_clip_bank import PcmClipBank
from ack_audio import AckBank, bucket_for, ACK_BUCKETS

ok = True
def check(cond, msg):
    global ok
    ok = ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {msg}")


# ── bucket_for routing (pure function) ───────────────────────────────────────
check(bucket_for("others", "good", 0.9) == "answer_good", "good answer → answer_good")
check(bucket_for("others", "partial", 0.9) == "answer_partial", "partial → answer_partial")
check(bucket_for("others", "wrong", 0.9) == "answer_wrong", "wrong → answer_wrong")
check(bucket_for("others", "good", 0.2) == "neutral", "low confidence → neutral")
check(bucket_for("skip", None, 0.9) == "skip", "skip → skip")
check(bucket_for("repeat", None, 0.9) == "repeat", "repeat → repeat")
check(bucket_for("audio", None, 0.9) == "repeat", "audio clarification → repeat")
check(bucket_for("exit", None, 0.9) == "exit", "exit → exit")
check(bucket_for("others", "good", 0.9, is_last=True) == "answer_good_final", "last good → answer_good_final")
check(bucket_for("skip", None, 0.9, is_last=True) == "skip_final", "last skip → skip_final")
check(bucket_for("repeat", None, 0.9, is_last=True) == "repeat", "last repeat has no final variant")


# ── synthetic bank in a temp dir ─────────────────────────────────────────────
def build_bank(tmp, buckets):
    """buckets: {bucket: n_clips}. Writes silent PCM + manifest, returns AckBank."""
    clips = []
    for bucket, n in buckets.items():
        bdir = os.path.join(tmp, bucket)
        os.makedirs(bdir, exist_ok=True)
        for i in range(n):
            rel = os.path.join(bucket, f"c{i}.pcm")
            with open(os.path.join(tmp, rel), "wb") as f:
                f.write(b"\x00\x00" * 100)
            clips.append({"id": f"{bucket}_{i}", "bucket": bucket, "path": rel})
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump({"sample_rate": 24000, "clips": clips}, f)
    return AckBank(tmp)


with tempfile.TemporaryDirectory() as tmp:
    bank = build_bank(tmp, {"answer_good": 5, "neutral": 3})
    check(bank.available, "bank loads from manifest")
    check(bank.sample_rate == 24000, "sample_rate read from manifest")

    # no-immediate-repeat: consecutive picks avoiding the last id differ
    recent = []
    distinct = True
    for _ in range(5):
        pcm, cid = bank.pick("answer_good", recent[-1:])
        if recent and cid == recent[-1]:
            distinct = False
        recent.append(cid)
    check(distinct, "no-immediate-repeat across picks")
    check(all(bank.pick("answer_good")[0] == b"\x00\x00" * 100 for _ in range(3)), "pick returns PCM bytes")

    # fallback: an unrendered bucket falls back to neutral
    pcm, cid = bank.pick("answer_wrong")
    check(pcm is not None and cid.startswith("neutral"), "empty bucket falls back to neutral")

    # keyed lookup
    check(bank.get("answer_good_0") == b"\x00\x00" * 100, "keyed get() returns the clip")
    check(bank.get("does_not_exist") is None, "keyed get() misses cleanly")


# ── graceful degradation: missing manifest ───────────────────────────────────
with tempfile.TemporaryDirectory() as empty:
    dead = AckBank(empty)
    check(not dead.available, "missing manifest → not available")
    check(dead.pick("answer_good") is None, "missing manifest → pick None (live TTS fallback)")


print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
