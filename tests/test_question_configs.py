#!/usr/bin/env python3
"""Question generation validated against N interview configs (deterministic, no LLM).

For every config in the matrix, drive the real QuestionFormatTracker over the content
range and assert the plan honors the config EXACTLY: resume/jd source split and
conceptual/scenario style split all land on their configured counts. This is the
"validate question generation against n configs" check, run for free.
"""
import os, sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.fake_candidate import sample_configs
from core.responses_client import QuestionFormatTracker, QuestionFormat
try:
    from core.responses_client import _SCENARIO_FAMILY
except Exception:
    _SCENARIO_FAMILY = (QuestionFormat.SCENARIO_BASED,)


def deliver(cfg):
    qpr = cfg["questions_per_round"]
    t = QuestionFormatTracker(
        _total_q_with_greeting=qpr + 2, resume_ratio=0.0,
        target_conceptual=cfg["conceptual_question_count"],
        target_scenario=cfg["scenario_question_count"],
        target_resume=cfg["resume_question_count"],
    )
    c = Counter()
    fmts = []
    for qn in range(2, qpr + 2):           # content questions cq = 2 .. qpr+1
        f = t.get_next_format(qn)
        t.register_format(qn, f)
        fmts.append(f)
    for f in fmts:
        if f == QuestionFormat.RESUME_PROJECT:
            c["resume"] += 1
        elif f in _SCENARIO_FAMILY:
            c["scenario"] += 1
        elif f == QuestionFormat.CONCEPTUAL:
            c["conceptual_only"] += 1
    c["conceptual"] = c["resume"] + c["conceptual_only"]   # resume questions are conceptual-styled
    c["jd"] = len(fmts) - c["resume"]
    c["total"] = len(fmts)
    return c


if __name__ == "__main__":
    ok = True
    configs = sample_configs()
    print(f"Validating question generation against {len(configs)} configs:\n")
    for i, cfg in enumerate(configs, 1):
        c = deliver(cfg)
        exp = {
            "total": cfg["questions_per_round"],
            "resume": cfg["resume_question_count"],
            "jd": cfg["jd_question_count"],
            "conceptual": cfg["conceptual_question_count"],
            "scenario": cfg["scenario_question_count"],
        }
        got = {k: c[k] for k in exp}
        good = got == exp
        ok = ok and good
        print(f"  {'✅' if good else '❌'} cfg{i} qpr={cfg['questions_per_round']:>2} "
              f"jd={exp['jd']} resume={exp['resume']} concept={exp['conceptual']} scenario={exp['scenario']}"
              + ("" if good else f"  → GOT {got}"))

    print("\n" + ("🎉 QUESTION GENERATION HONORS ALL CONFIGS EXACTLY"
                  if ok else "⚠️ CONFIG NOT HONORED — see above"))
    sys.exit(0 if ok else 1)
