#!/usr/bin/env python3
"""Unit test for the nano intent+quality classifier (Task 2, reflex_engine.generate_intent_quality).

Mocks the OpenAI client so no live call is made. Asserts parsing, validation (invalid intent →
others, quality forced to "na" for non-answer intents, confidence clamp), the failure fallback
(low confidence), and end-to-end mapping into an ack bucket via ack_audio.bucket_for.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reflex_engine import ReflexEngine
from ack_audio import bucket_for

ok = True
def check(cond, msg):
    global ok
    ok = ok and bool(cond)
    print(f"  {'✅' if cond else '❌'} {msg}")


# ── fake OpenAI client returning canned JSON (or raising) ────────────────────
class _Msg:
    def __init__(self, content): self.message = type("M", (), {"content": content})()
class _Resp:
    def __init__(self, content): self.choices = [_Msg(content)]
class _Completions:
    def __init__(self, content=None, exc=None): self.content, self.exc = content, exc
    def create(self, **kw):
        if self.exc:
            raise self.exc
        return _Resp(self.content)
class _Client:
    def __init__(self, content=None, exc=None):
        self.chat = type("Chat", (), {"completions": _Completions(content, exc)})()


def run(content=None, exc=None, **kw):
    eng = ReflexEngine(api_key="test")
    eng.client = _Client(content, exc)
    return eng.generate_intent_quality("some answer text", "Explain Python decorators", **kw)


# ── parsing happy paths ──────────────────────────────────────────────────────
r = run('{"intent":"others","quality":"good","confidence":0.9}')
check(r == {"intent": "others", "quality": "good", "confidence": 0.9}, "good answer parsed")
check(bucket_for(**r) == "answer_good", "good → answer_good bucket")

r = run('{"intent":"others","quality":"partial","confidence":0.7}')
check(r["quality"] == "partial" and bucket_for(**r) == "answer_partial", "partial → answer_partial")

r = run('{"intent":"others","quality":"wrong","confidence":0.8}')
check(bucket_for(**r) == "answer_wrong", "wrong → answer_wrong")

# low confidence on quality → neutral bucket (never wrongly-toned)
r = run('{"intent":"others","quality":"good","confidence":0.2}')
check(bucket_for(**r) == "neutral", "low-confidence good → neutral bucket")

# non-answer intents force quality to na
r = run('{"intent":"skip","quality":"good","confidence":0.9}')
check(r["quality"] == "na" and bucket_for(**r) == "skip", "skip forces quality=na → skip bucket")

r = run('{"intent":"repeat","quality":"na","confidence":0.9}')
check(bucket_for(**r) == "repeat", "repeat → repeat bucket")

r = run('{"intent":"exit","quality":"na","confidence":0.9}')
check(bucket_for(**r) == "exit", "exit → exit bucket")

# ── validation ───────────────────────────────────────────────────────────────
r = run('{"intent":"bogus","quality":"weird","confidence":9}')
check(r["intent"] == "others", "invalid intent → others")
check(r["quality"] in ("na", "good", "partial", "wrong"), "invalid quality sanitized")
check(0.0 <= r["confidence"] <= 1.0, "confidence clamped to [0,1]")

# last-question routing
r = run('{"intent":"others","quality":"good","confidence":0.9}')
check(bucket_for(**r, is_last=True) == "answer_good_final", "is_last → *_final bucket")

# ── failure fallback → low confidence, neutral ───────────────────────────────
r = run(exc=RuntimeError("api down"))
check(r["confidence"] == 0.3 and r["intent"] in {"others", "repeat", "rephrase", "skip", "exit"},
      "API failure → deterministic intent + low confidence")
check(bucket_for(**r) == "neutral", "failure fallback → neutral bucket")

# rubric param accepted (reliability aid for bank questions)
r = run('{"intent":"others","quality":"good","confidence":0.9}', rubric="mutability, hashability")
check(r["quality"] == "good", "rubric param accepted")

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
