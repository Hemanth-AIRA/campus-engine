#!/usr/bin/env python3
"""Unit tests for difficulty_policy — round envelopes, vector computation, escalation.

Pure functions, no LLM. Verifies the difficulty engine is internally consistent:
vectors always land inside the round envelope, adaptation moves the right way, and the
round→canonical mapping is monotonic.
"""
from core.difficulty_policy import (
    get_round_caps, compute_difficulty_vector, get_escalation_strategy, DifficultyVector,
)

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


def in_env(v, caps):
    return (caps["A"][0] <= v.abstraction_depth <= caps["A"][1]
            and caps["C"][0] <= v.constraint_pressure <= caps["C"][1]
            and caps["Amb"][0] <= v.ambiguity <= caps["Amb"][1]
            and caps["S"][0] <= v.scale_complexity <= caps["S"][1])


if __name__ == "__main__":
    # 1. Round caps exist for every (role, difficulty, round) and are (min<=max).
    all_valid = True
    for role in ("junior", "mid", "senior"):
        for diff in ("easy", "medium", "hard"):
            for rnd in (1, 2, 3):
                caps = get_round_caps(role, diff, rnd, total_rounds=3)
                for dim in ("A", "C", "Amb", "S"):
                    if not (caps[dim][0] <= caps[dim][1]):
                        all_valid = False
    check(all_valid, "every (role,difficulty,round) envelope has min<=max on all 4 dims")

    # 2. Round mapping: a 1-round interview peaks (canonical R3); 3-round is R1<R2<R3.
    c1 = get_round_caps("mid", "medium", 1, total_rounds=1)
    c3 = get_round_caps("mid", "medium", 3, total_rounds=3)
    check(c1 == c3, "1-round interview maps to canonical peak (R3)")
    a1 = get_round_caps("mid", "medium", 1, 3)["A"][1]
    a3 = get_round_caps("mid", "medium", 3, 3)["A"][1]
    check(a3 >= a1, "later rounds are at least as hard (A ceiling grows R1→R3)")

    # 3. No baseline → vector sits at the envelope floor.
    caps = get_round_caps("mid", "medium", 1, 3)
    v0 = compute_difficulty_vector("medium", "mid", 1, skill_baseline=None, total_rounds=3)
    check(v0.abstraction_depth == caps["A"][0] and in_env(v0, caps),
          "no baseline → vector at envelope floor, inside envelope")

    # 4. Strong baseline escalates but STILL stays inside the envelope (guardrail holds).
    strong = {"baseline_complexity": 3.0, "baseline_confidence": 0.9}
    vs = compute_difficulty_vector("medium", "mid", 3, skill_baseline=strong, total_rounds=3)
    caps3 = get_round_caps("mid", "medium", 3, 3)
    check(in_env(vs, caps3), "strong baseline escalates but stays clamped in the envelope")

    # 5. Weak baseline does not exceed a competent one.
    weak = {"baseline_complexity": 1.0, "baseline_confidence": 0.2}
    vw = compute_difficulty_vector("medium", "mid", 1, skill_baseline=weak, total_rounds=3)
    def total(v): return v.abstraction_depth + v.constraint_pressure + v.ambiguity + v.scale_complexity
    check(total(vw) <= total(vs), "weak-baseline vector is no harder than strong-baseline vector")

    # 6. Escalation strategy is one of the two known orders.
    strat = get_escalation_strategy("senior", "hard", 2)
    check(strat in ("abstraction_first", "constraint_first"), f"escalation strategy valid: {strat}")

    print("\n" + ("🎉 DIFFICULTY POLICY CONSISTENT (envelopes hold, adaptation directional)"
                  if ok else "⚠️ DIFFICULTY POLICY ISSUE — see above"))
    import sys
    sys.exit(0 if ok else 1)
