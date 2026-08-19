#!/usr/bin/env python3
"""CAMPUS Phase 1 — determinism proof.

The #1 pain was "difficulty feels inconsistent" — the same config produced a different
question/skill/difficulty sequence every run because skill/format/paradigm selection was
randomized. Phase 1 removed every `random.*` from those paths. This test proves the
selection is now a pure function of state: two runs of the same config yield the SAME
skill sequence, the SAME format sequence, and the SAME skill-count sequence.
"""
import core.responses_client as rc
from core.responses_client import (
    QuestionFormatTracker, QuestionFormat, SkillTracker,
    decide_skill_count, _det_frac, _det_pick,
)

# Make skill grouping hermetic + deterministic (avoid the one-time LLM call at init).
rc.generate_skill_groups = lambda skills, api_key=None: {}


def run_skills(mand, opt, n):
    t = SkillTracker(mandatory_skills=list(mand), optional_skills=list(opt))
    t._total_questions = n
    seq = []
    for q in range(2, n + 2):
        sk = t.get_next_skills(count=1)
        seq.append(tuple(sk))
        t.register_question(q, sk, f"Q{q}")
    return seq


def run_formats(n, target_conceptual, target_scenario, target_resume, resume_ratio=0.0):
    t = QuestionFormatTracker(
        _total_q_with_greeting=n + 2, resume_ratio=resume_ratio,
        target_conceptual=target_conceptual, target_scenario=target_scenario,
        target_resume=target_resume,
    )
    seq = []
    for q in range(2, n + 2):
        f = t.get_next_format(q)
        t.register_format(q, f)
        seq.append(f.value)
    return seq


def run_counts(n, role):
    # decide_skill_count needs a tracker for status; use a small SkillTracker.
    t = SkillTracker(mandatory_skills=[f"S{i}" for i in range(6)], optional_skills=[])
    t._total_questions = n
    return [decide_skill_count(q, t, role) for q in range(2, n + 2)]


if __name__ == "__main__":
    ok = True
    MAND = ["Python", "SQL", "React", "Docker", "Testing", "Git"]
    OPT = ["Kubernetes", "AWS"]

    # 1. Skill sequence is identical across two independent runs.
    s1, s2 = run_skills(MAND, OPT, 8), run_skills(MAND, OPT, 8)
    print("skills run1:", [s[0] for s in s1])
    print("skills run2:", [s[0] for s in s2])
    if s1 != s2:
        ok = False; print("  ❌ skill sequence NOT deterministic")
    else:
        print("  ✅ skill sequence deterministic")

    # 2. Format sequence identical across runs — legacy (no targets) AND explicit.
    for label, kw in (
        ("legacy (no targets)", dict(target_conceptual=None, target_scenario=None, target_resume=0)),
        ("explicit 3c/1s/3r",   dict(target_conceptual=3, target_scenario=1, target_resume=3)),
    ):
        f1, f2 = run_formats(4, **kw), run_formats(4, **kw)
        print(f"formats [{label}] run1:", f1)
        if f1 != f2:
            ok = False; print(f"  ❌ format sequence NOT deterministic ({label})")
        else:
            print(f"  ✅ format sequence deterministic ({label})")

    # 3. Skill-count sequence identical across runs.
    for role in ("junior", "mid", "senior"):
        c1, c2 = run_counts(8, role), run_counts(8, role)
        if c1 != c2:
            ok = False; print(f"  ❌ skill-count NOT deterministic ({role})")
        else:
            print(f"  ✅ skill-count deterministic ({role}): {c1}")

    # 4. Helpers are pure.
    assert all(_det_frac(i) == _det_frac(i) for i in range(20))
    assert _det_pick([1, 2, 3], 4) == _det_pick([1, 2, 3], 4)
    print("  ✅ _det_frac / _det_pick pure")

    print("\n" + ("🎉 CAMPUS SELECTION IS FULLY DETERMINISTIC (pain #1 fixed)"
                  if ok else "⚠️ NON-DETERMINISM REMAINS — see above"))
    import sys
    sys.exit(0 if ok else 1)
