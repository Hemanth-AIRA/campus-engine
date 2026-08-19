#!/usr/bin/env python3
"""Unit tests for interview_config — validation clamping + RuntimeSkillState + campus thresholds.

Complements test_question_count.py (which covers the jd/resume count math). Here we check the
guard-rails: invalid inputs fall back to safe defaults, and the runtime adaptive state exists.
"""
from core.interview_config import (
    InterviewConfig, InterviewContext, RuntimeSkillState,
    _CAMPUS_ESCALATE_CONF, _CAMPUS_LOWER_CONF, _CAMPUS_REMOVE_CONF,
)
from core.difficulty_policy import DifficultyVector

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


if __name__ == "__main__":
    # 1. Invalid inputs clamp to safe defaults (never crash a live interview).
    c = InterviewConfig(total_rounds=0, questions_per_round=6, base_difficulty="impossible",
                        focus_mode="weird", role_experience_required="wizard",
                        pass_threshold_pct=250, max_duration_minutes=-5)
    check(c.total_rounds >= 1, "invalid total_rounds → clamped to >=1")
    check(c.base_difficulty in ("easy", "medium", "hard"), "invalid base_difficulty → valid default")
    check(c.role_experience_required in ("junior", "mid", "senior"), "invalid role → valid default")
    check(0 <= c.pass_threshold_pct <= 100, "out-of-range pass_threshold → clamped to [0,100]")
    check(c.max_duration_minutes >= 1, "invalid duration → clamped to >=1")

    # 2. Sensible defaults.
    d = InterviewConfig(questions_per_round=8)
    check(d.total_rounds == 1 and d.questions_per_round == 8, "defaults: 1 round, honors qpr")

    # 3. total_questions property = rounds * qpr.
    e = InterviewConfig(total_rounds=3, questions_per_round=5)
    check(e.total_questions == 15, "total_questions = rounds * qpr")

    # 4. Campus adaptive thresholds are ordered sanely (remove < lower < escalate).
    check(_CAMPUS_REMOVE_CONF < _CAMPUS_LOWER_CONF < _CAMPUS_ESCALATE_CONF,
          f"campus thresholds ordered: {_CAMPUS_REMOVE_CONF} < {_CAMPUS_LOWER_CONF} < {_CAMPUS_ESCALATE_CONF}")

    # 5. RuntimeSkillState + InterviewContext basic wiring.
    ctx = InterviewContext(config=InterviewConfig(questions_per_round=6))
    ctx.skill_states["Python"] = RuntimeSkillState(vector=DifficultyVector(2, 0, 0, 0), skill="Python")
    ctx.skill_states["Python"].recent_conf.append(0.8)
    check(ctx.skill_states["Python"].status == "active", "new skill state starts active")
    ctx.advance_question()
    check(ctx.current_question_index == 1, "advance_question increments index")

    print("\n" + ("🎉 INTERVIEW CONFIG VALIDATED (guard-rails + runtime state OK)"
                  if ok else "⚠️ INTERVIEW CONFIG ISSUE — see above"))
    import sys
    sys.exit(0 if ok else 1)
