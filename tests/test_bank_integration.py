#!/usr/bin/env python3
"""CAMPUS Phase 3 — integration proof for the hybrid sourcing hook in ResponsesClient.

Exercises _campus_source_question directly (no LLM call): a banked skill returns a curated
bank question (and rotates through unused ones); an unbanked skill falls through to the LLM
text unchanged. Requires OPENAI_API_KEY only to construct the client (no network use)."""
import os
import sys

if not os.getenv("OPENAI_API_KEY"):
    print("SKIP: OPENAI_API_KEY not set (needed only to construct ResponsesClient)")
    sys.exit(0)

from core.responses_client import ResponsesClient
from core.interview_config import InterviewConfig
from core.difficulty_policy import DifficultyVector

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")

cfg = InterviewConfig(questions_per_round=6, role_experience_required="junior", base_difficulty="medium")
client = ResponsesClient(interview_config=cfg)
client._last_used_vector = DifficultyVector(1, 0, 0, 0)  # foundational tier

check(client._campus_bank is not None and client._campus_bank.has_skill("Python"),
      "ResponsesClient loaded the campus bank")

DUMMY = "Tell me about a cooking recipe you enjoy."  # clearly off-target

q1 = client._campus_source_question(DUMMY, ["Python"], 3)
check(q1 != DUMMY, f"banked skill (Python) served a CURATED question, not the LLM text")

q2 = client._campus_source_question(DUMMY, ["Python"], 4)
check(q2 != q1 and q2 != DUMMY, "second banked call rotates to a DIFFERENT curated question")

q3 = client._campus_source_question(DUMMY, ["Kotlin"], 5)  # unbanked
check(q3 == DUMMY, "unbanked skill (Kotlin) falls through to the LLM text unchanged")

print("🎉 HYBRID SOURCING INTEGRATED (bank-first for banked skills, LLM fallback otherwise)"
      if ok else "⚠️ INTEGRATION ISSUE — see above")
sys.exit(0 if ok else 1)
