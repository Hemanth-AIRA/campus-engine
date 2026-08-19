#!/usr/bin/env python3
"""CAMPUS Phase 3 — hybrid question bank + on-target validation proof.

Targets the "questions drift off-target" pain:
  • the bank serves unused, tier-matched, skill-anchored questions;
  • every bank question passes the on-target validator for its own skill;
  • the validator rejects off-target questions (so LLM-fallback drift is caught);
  • unbanked skills return None (→ caller uses the LLM fallback).
"""
from campus.question_bank import QuestionBank, is_on_target, vector_to_tier, FOUNDATIONAL, APPLIED, ADVANCED
from core.difficulty_policy import DifficultyVector

ok = True


def check(cond, msg):
    global ok
    if not cond:
        ok = False
        print(f"  ❌ {msg}")
    else:
        print(f"  ✅ {msg}")


if __name__ == "__main__":
    qb = QuestionBank()

    # 1. Loaded the starter banks.
    check(qb.has_skill("Python") and qb.has_skill("SQL"), "bank loaded Python + SQL")
    check(not qb.has_skill("Rust"), "unbanked skill (Rust) reports absent")

    # 2. Tier-matched selection + unused tracking.
    used = set()
    e1 = qb.get("Python", "junior", FOUNDATIONAL, used)
    check(e1 and e1["_tier"] == FOUNDATIONAL, f"got a foundational Python question: {e1 and e1['id']}")
    used.add(e1["id"])
    e2 = qb.get("Python", "junior", FOUNDATIONAL, used)
    check(e2 and e2["id"] != e1["id"], "second call returns a DIFFERENT (unused) question")

    # 3. Adjacent-tier fallback (senior Python only has 'advanced').
    e3 = qb.get("Python", "senior", FOUNDATIONAL, set())
    check(e3 is not None, "requesting a missing tier falls back to an adjacent tier")

    # 4. Unbanked skill → None (caller uses LLM fallback).
    check(qb.get("Rust", "mid", APPLIED, set()) is None, "unbanked skill returns None")

    # 5. vector_to_tier mapping.
    check(vector_to_tier(DifficultyVector(1, 0, 0, 0)) == FOUNDATIONAL, "A=1,C=0 → foundational")
    check(vector_to_tier(DifficultyVector(3, 1, 0, 0)) == APPLIED, "A=3,C=1 → applied")
    check(vector_to_tier(DifficultyVector(5, 2, 1, 1)) == ADVANCED, "A=5 → advanced")

    # 6. On-target validator — positive, alias, and negative.
    check(is_on_target("Explain how a Python dictionary works.", "Python", qb.aliases("Python")),
          "validator accepts an on-target Python question")
    check(is_on_target("How would you tune a slow Postgres query?", "SQL", qb.aliases("SQL")),
          "validator matches via alias (postgres → SQL)")
    check(not is_on_target("Tell me about your favorite hobby.", "Python", qb.aliases("Python")),
          "validator rejects an off-target question")
    check(not is_on_target("Describe an algorithm you like.", "Go", ["golang"]),
          "validator does not false-match 'go' inside 'algorithm'")

    # 7. EVERY bank question is on-target for its own skill (bank quality gate).
    all_on = True
    count = 0
    for skill in ("Python", "SQL"):
        al = qb.aliases(skill)
        tp = qb.topics(skill)
        for tier in (FOUNDATIONAL, APPLIED, ADVANCED):
            for lvl in ("junior", "mid", "senior"):
                seen = set()
                while True:
                    e = qb.get(skill, lvl, tier, seen)
                    if not e or e["id"] in seen:
                        break
                    seen.add(e["id"])
                    count += 1
                    if not is_on_target(e["question"], skill, al, tp):
                        all_on = False
                        print(f"    off-target bank entry: {e['id']}: {e['question'][:50]}")
    check(all_on, f"all sampled bank questions are on-target ({count} checks)")

    print("\n🎉 HYBRID BANK + ON-TARGET VALIDATION WORKS (pain #2 addressed)"
          if ok else "⚠️ BANK/VALIDATOR ISSUE — see above")
    import sys
    sys.exit(0 if ok else 1)
