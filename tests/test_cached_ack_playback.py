#!/usr/bin/env python3
"""Integration test for webrtc.audio.play_cached_ack (Task 2).

Mocks the LiveKit adapter and installs a synthetic ack bank, then asserts play_cached_ack:
plays the picked clip through adapter.play_cached_clip, returns the clip_id, and cleanly
returns None (→ live-TTS fallback) when the kill-switch is off, no adapter is present, or
the bank misses.
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webrtc.audio as A
import ack_audio
from ack_audio import AckBank

ok = True
def check(cond, msg):
    global ok
    ok = ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {msg}")


# ── synthetic ack bank (silent PCM) ──────────────────────────────────────────
def build_bank(tmp, buckets):
    clips = []
    for bucket, n in buckets.items():
        os.makedirs(os.path.join(tmp, bucket), exist_ok=True)
        for i in range(n):
            rel = os.path.join(bucket, f"c{i}.pcm")
            with open(os.path.join(tmp, rel), "wb") as f:
                f.write(b"\x00\x00" * 240)
            clips.append({"id": f"{bucket}_{i}", "bucket": bucket, "path": rel})
    with open(os.path.join(tmp, "manifest.json"), "w") as f:
        json.dump({"sample_rate": 24000, "clips": clips}, f)
    return AckBank(tmp)


class FakeAdapter:
    def __init__(self):
        self.calls = []
    def play_cached_clip(self, pcm, sample_rate=24000, clip_id=""):
        self.calls.append((clip_id, len(pcm), sample_rate))
        return True


tmp = tempfile.mkdtemp()
ack_audio._BANK = build_bank(tmp, {"answer_good": 4, "neutral": 2})

fake = FakeAdapter()
_orig_adapter, _orig_flag = A.get_livekit_adapter, A.USE_CACHED_ACKS
try:
    A.get_livekit_adapter = lambda: fake
    A.USE_CACHED_ACKS = True

    cid = A.play_cached_ack("answer_good", [], room_id="R", process_id=0)
    check(cid and cid.startswith("answer_good"), "plays a cached ack clip and returns its id")
    check(fake.calls and fake.calls[-1][0] == cid, "routed the picked clip to adapter.play_cached_clip")
    check(fake.calls[-1][2] == 24000, "passes the bank sample rate")

    # empty bucket → neutral fallback still plays
    cid2 = A.play_cached_ack("answer_wrong", [], room_id="R")
    check(cid2 and cid2.startswith("neutral"), "empty bucket falls back to neutral clip")

    # kill-switch off → None (live TTS)
    A.USE_CACHED_ACKS = False
    check(A.play_cached_ack("answer_good", []) is None, "kill-switch off → None (live fallback)")
    A.USE_CACHED_ACKS = True

    # no adapter → None
    A.get_livekit_adapter = lambda: None
    check(A.play_cached_ack("answer_good", []) is None, "no adapter → None (live fallback)")

    # bank unavailable → None
    A.get_livekit_adapter = lambda: fake
    ack_audio._BANK = AckBank(tempfile.mkdtemp())  # empty dir, no manifest
    check(A.play_cached_ack("answer_good", []) is None, "unrendered bank → None (live fallback)")
finally:
    A.get_livekit_adapter, A.USE_CACHED_ACKS = _orig_adapter, _orig_flag
    ack_audio._BANK = None

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
