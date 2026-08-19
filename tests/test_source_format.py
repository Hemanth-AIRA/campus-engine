#!/usr/bin/env python3
"""Exact-count test for the decoupled SOURCE × STYLE question model.

Every config's four counts (jd/resume source, conceptual/scenario style) must be
honored EXACTLY and deterministically. RESUME_PROJECT questions are conceptual-styled
and count toward the conceptual total (a resume question asked as "explain your
project X" IS conceptual). So:  conceptual = RESUME_PROJECT + CONCEPTUAL.
"""
from collections import Counter
from core.responses_client import QuestionFormatTracker, QuestionFormat
try:
    from core.responses_client import _SCENARIO_FAMILY
except Exception:
    _SCENARIO_FAMILY = (QuestionFormat.SCENARIO_BASED,)

CONFIGS = [
    {"questions_per_round": 4,  "jd_question_count": 2, "resume_question_count": 2,
     "conceptual_question_count": 4, "scenario_question_count": 0},           # job 6511
    {"questions_per_round": 10, "jd_question_count": 6, "resume_question_count": 4,
     "conceptual_question_count": 8, "scenario_question_count": 2},           # the 10-Q config
]


def run(cfg):
    qpr = cfg["questions_per_round"]
    # Q1 greeting, Q2 background → content is Q3 .. Q(qpr+2), i.e. exactly qpr content questions.
    t = QuestionFormatTracker(
        _total_q_with_greeting=qpr + 2, resume_ratio=0.0,
        target_conceptual=cfg["conceptual_question_count"],
        target_scenario=cfg["scenario_question_count"],
        target_resume=cfg["resume_question_count"],
    )
    fmts = []
    for qn in range(2, qpr + 1 + 1):        # the loop asks content questions as cq=2..qpr+1
        f = t.get_next_format(qn)
        t.register_format(qn, f)
        fmts.append(f)
    c = Counter()
    for f in fmts:
        if f == QuestionFormat.RESUME_PROJECT: c["resume"] += 1
        elif f in _SCENARIO_FAMILY:            c["scenario"] += 1
        elif f == QuestionFormat.CONCEPTUAL:   c["conceptual_only"] += 1
    c["conceptual"] = c["resume"] + c["conceptual_only"]        # resume is conceptual-styled
    c["jd"] = len(fmts) - c["resume"]
    return c, fmts


if __name__ == "__main__":
    ok = True
    for cfg in CONFIGS:
        c, fmts = run(cfg)
        print(f"\nconfig qpr={cfg['questions_per_round']} "
              f"(jd={cfg['jd_question_count']},resume={cfg['resume_question_count']},"
              f"conceptual={cfg['conceptual_question_count']},scenario={cfg['scenario_question_count']})")
        print("  formats:", [f.value for f in fmts])
        print(f"  got: resume={c['resume']} jd={c['jd']} conceptual={c['conceptual']} scenario={c['scenario']}")
        for key, cfgkey in (("resume","resume_question_count"),("jd","jd_question_count"),
                            ("conceptual","conceptual_question_count"),("scenario","scenario_question_count")):
            exp = cfg[cfgkey]
            if c[key] != exp:
                print(f"  ❌ {key}: got {c[key]}, expected {exp}"); ok = False
        if all(c[k] == cfg[ck] for k,ck in (("resume","resume_question_count"),("jd","jd_question_count"),
               ("conceptual","conceptual_question_count"),("scenario","scenario_question_count"))):
            print("  ✅ exact match")
    print("\n" + ("🎉 ALL CONFIGS HONORED EXACTLY" if ok else "⚠️ MISMATCH — see above"))
    import sys; sys.exit(0 if ok else 1)
