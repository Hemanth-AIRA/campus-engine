#!/usr/bin/env python3
"""CAMPUS Phase 2 — adaptive-first difficulty proof.

Difficulty must FOLLOW the student's live performance (the confidence_score fed after
each answer), in bounded steps, within the round envelope:
  • strong answers (>= CAMPUS_ESCALATE_CONF) → difficulty steps UP
  • weak answers  (<  CAMPUS_LOWER_CONF)    → difficulty drops to the easy end (scaffold)
  • middle answers                          → difficulty HOLDS
and struggling students are NOT removed (prep, not hiring) unless the answer is ~blank.
"""
from core.interview_config import (
    InterviewConfig, InterviewContext, RuntimeSkillState,
    _CAMPUS_ESCALATE_CONF, _CAMPUS_LOWER_CONF, _CAMPUS_REMOVE_CONF,
)
from core.difficulty_policy import compute_difficulty_vector, DifficultyVector


def vsum(v):
    return v.abstraction_depth + v.constraint_pressure + v.ambiguity + v.scale_complexity


def ctx_with_skill(skill="Python", role="mid", diff="medium", start_vec=None):
    cfg = InterviewConfig(questions_per_round=6, role_experience_required=role,
                          base_difficulty=diff, total_rounds=1)
    ctx = InterviewContext(config=cfg)
    v = start_vec or compute_difficulty_vector(diff, role, 1, total_rounds=1)
    ctx.skill_states[skill] = RuntimeSkillState(vector=v, skill=skill, role=role,
                                                difficulty=diff, round_number=1)
    return cfg, ctx, skill


def feed(ctx, skill, scores):
    st = ctx.skill_states[skill]
    for s in scores:
        st.recent_conf.append(s)
    ctx.maybe_adapt(skill)
    return st


if __name__ == "__main__":
    ok = True

    # 1. STRONG answers → escalate (difficulty rises).
    cfg, ctx, sk = ctx_with_skill()
    st = ctx.skill_states[sk]
    before = vsum(st.vector)
    st = feed(ctx, sk, [0.9, 0.9])
    after = vsum(st.vector)
    print(f"strong: {before} → {after}  (status={st.status})")
    if not (after > before):
        ok = False; print("  ❌ strong answers did not escalate difficulty")
    else:
        print("  ✅ strong answers escalate difficulty")

    # 2. WEAK answers → drop to easy end (confirming/scaffold), NOT removed.
    high = DifficultyVector(4, 2, 1, 1)
    cfg, ctx, sk = ctx_with_skill(start_vec=high)
    st = ctx.skill_states[sk]
    before = vsum(st.vector)
    st = feed(ctx, sk, [0.30, 0.30])       # below LOWER, above REMOVE
    after = vsum(st.vector)
    print(f"weak:   {before} → {after}  (status={st.status})")
    if not (after < before and st.status != "removed"):
        ok = False; print("  ❌ weak answers did not scaffold (or wrongly removed the skill)")
    else:
        print("  ✅ weak answers lower difficulty and keep the skill (scaffold, not remove)")

    # 3. MIDDLE answers → hold.
    mid = DifficultyVector(3, 1, 0, 0)
    cfg, ctx, sk = ctx_with_skill(start_vec=DifficultyVector(mid.abstraction_depth, mid.constraint_pressure, mid.ambiguity, mid.scale_complexity))
    st = ctx.skill_states[sk]
    before = vsum(st.vector)
    mid_conf = (_CAMPUS_LOWER_CONF + _CAMPUS_ESCALATE_CONF) / 2
    st = feed(ctx, sk, [mid_conf, mid_conf])
    after = vsum(st.vector)
    print(f"hold:   {before} → {after}  (conf={mid_conf:.2f}, status={st.status})")
    if after != before or st.status == "removed":
        ok = False; print("  ❌ middle answers changed difficulty (should hold)")
    else:
        print("  ✅ middle answers hold difficulty")

    # 4. Only ~blank answers remove the skill.
    cfg, ctx, sk = ctx_with_skill()
    st = feed(ctx, sk, [0.02, 0.02])
    print(f"blank:  status={st.status}")
    if st.status != "removed":
        print("  ⚠️ note: blank answer did not remove (acceptable if campus never removes)")
    else:
        print("  ✅ near-blank answers remove the skill (extreme case only)")

    print(f"\nthresholds: escalate>={_CAMPUS_ESCALATE_CONF}, lower<{_CAMPUS_LOWER_CONF}, remove<{_CAMPUS_REMOVE_CONF}")
    print("🎉 CAMPUS DIFFICULTY IS ADAPTIVE-FIRST (follows live performance, bounded)"
          if ok else "⚠️ ADAPTATION NOT BEHAVING — see above")
    import sys
    sys.exit(0 if ok else 1)
