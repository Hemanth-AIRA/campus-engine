#!/usr/bin/env python3
"""Unit tests for evaluation_engine — confidence clamping, result validation, safe fallback.

No LLM calls (uses the empty-answer short-circuit + pure helpers), so it's fast and free.
Verifies the evaluator never raises and always returns a well-formed, in-range result.
"""
from core.evaluation_engine import _clamp_confidence, _validate_result, evaluate_answer, _FALLBACK

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


if __name__ == "__main__":
    # 1. Confidence clamping to [0.05, 0.95] with type safety.
    check(_clamp_confidence(0.7) == 0.7, "in-range confidence passes through")
    check(_clamp_confidence(2.0) == 0.95 and _clamp_confidence(-1) == 0.05, "out-of-range clamps to [0.05,0.95]")
    check(_clamp_confidence("nan") == 0.5 and _clamp_confidence(None) == 0.5, "non-numeric → safe 0.5")

    # 2. Result validation fills schema + clamps.
    v = _validate_result({"confidence_score": 5, "evaluation_tags": "bad"})
    check(v["confidence_score"] == 0.95, "validate clamps confidence")
    check(isinstance(v["evaluation_tags"], list) and "skill" in v and "projects_mentioned" in v,
          "validate coerces tags to list and fills required keys")
    check(_validate_result("not a dict") == _FALLBACK, "non-dict result → fallback")

    # 3. evaluate_answer never raises + handles empty answers safely (no LLM call).
    r = evaluate_answer("Python", "What is a list?", "", round_number=1)
    check(r["evaluation_tags"] == ["no_answer"] and r["confidence_score"] == 0.05 and r["skill"] == "Python",
          "empty answer → no_answer fallback (score 0.05), never raises")
    r2 = evaluate_answer("SQL", "Explain JOINs", "   ", round_number=1)
    check(r2["confidence_score"] == 0.05, "whitespace-only answer → treated as no answer")

    # 4. Fallback shape is complete (report layer relies on these keys).
    for k in ("skill", "summary", "confidence_score", "evaluation_tags", "concept_tags", "projects_mentioned"):
        check(k in r, f"result always has key '{k}'") if k not in r else None
    check(all(k in r for k in _FALLBACK), "result carries the full fallback schema")

    print("\n" + ("🎉 EVALUATION ENGINE ROBUST (clamped, validated, never raises)"
                  if ok else "⚠️ EVALUATION ENGINE ISSUE — see above"))
    import sys
    sys.exit(0 if ok else 1)
