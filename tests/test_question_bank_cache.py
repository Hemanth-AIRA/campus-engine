#!/usr/bin/env python3
"""Unit tests for question_bank_cache (Task 2, Pillar 2): QuestionBankPlan + QuestionAudioCache.

No live Redis: a fake client is injected. Covers sequential group flattening (both schemas),
PCM passthrough + memoization, miss/unavailable → None (live-TTS fallback), and put/get.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from question_bank_cache import QuestionBankPlan, QuestionAudioCache

ok = True
def check(cond, msg):
    global ok
    ok = ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {msg}")


# ── QuestionBankPlan: groups-list schema, sequential ─────────────────────────
qb = {
    "sample_rate": 24000,
    "job_id": "J1",
    "groups": [
        {"key": "python", "skills": ["Python"], "questions": [
            {"id": "q1", "text": "Q one", "audio_key": "qbank:J1:python:q1"},
            {"id": "q2", "text": "Q two", "audio_key": "qbank:J1:python:q2"},
        ]},
        {"key": "python+sql", "skills": ["Python", "SQL"], "questions": [
            {"id": "q3", "text": "Q three", "audio_key": "qbank:J1:python+sql:q3"},
        ]},
    ],
}
plan = QuestionBankPlan(qb)
check(plan.available and plan.remaining == 3, "plan loads 3 questions")
seq = [plan.next()["id"] for _ in range(3)]
check(seq == ["q1", "q2", "q3"], "sequential per group in list order")
check(plan.next() is None, "exhausted → None")
check(QuestionBankPlan(qb).next()["skills"] == ["Python"], "question carries its group's skills")

# dict shape tolerated
qb_dict = {"groups": None, "python": {"skills": ["Python"], "questions": [{"id": "a", "text": "T"}]}}
check(QuestionBankPlan(qb_dict).remaining == 1, "dict {group: {questions}} shape accepted")

# empty / missing → not available (live fallback)
check(not QuestionBankPlan({}).available, "empty question_bank → not available")
check(not QuestionBankPlan(None).available, "None question_bank → not available")


# ── QuestionAudioCache with a fake Redis client ──────────────────────────────
class FakeRedis:
    def __init__(self): self.store = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v, ex=None): self.store[k] = v; return True


cache = QuestionAudioCache(url="redis://fake:6379/0")
cache._client = FakeRedis()
check(cache.available, "cache available when url + redis lib present")

pcm = b"\x00\x00" * 240  # raw PCM (even length, no MP3/ID3 header) → passthrough
cache._client.store["k1"] = pcm
got = cache.get_pcm("k1")
check(got == pcm, "get_pcm returns stored PCM (passthrough)")
check("k1" in cache._pcm_memo, "decoded PCM is memoized")
check(cache.get_pcm("k1") is got, "second get_pcm hits the memo")

check(cache.get_pcm("missing") is None, "missing key → None (live fallback)")
check(cache.get_pcm("") is None, "empty key → None")

# producer round-trip
check(cache.put_audio("k2", b"\x01\x02" * 10), "put_audio stores bytes")
check(cache._client.store.get("k2") == b"\x01\x02" * 10, "stored bytes retrievable")

# unavailable cache → None
dead = QuestionAudioCache(url="")
check(not dead.available, "no url → not available")
check(dead.get_pcm("k1") is None, "unavailable cache → get_pcm None")

# bogus non-PCM/odd-length that isn't decodable → None (graceful)
cache._client.store["bad"] = b"\xff\xfbnot-real-mp3-frame"
check(cache.get_pcm("bad") is None, "undecodable audio → None (graceful)")

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
