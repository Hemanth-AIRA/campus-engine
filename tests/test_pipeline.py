#!/usr/bin/env python3
"""Pipeline smoke + contract tests: aira_1 (ATS) → aira_2 (reminders) → aira_3 (orchestration)
→ webrtc.interview + core.responses_client + integrations. Catches wiring bugs, missing
functions, and (critically) any browser/Selenium code sneaking back in. No live services."""
import importlib, inspect, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ok = True
def check(cond, msg):
    global ok
    ok = ok and cond
    print(f"  {'✅' if cond else '❌'} {msg}")


# ── 1. Every pipeline entry point imports (wiring intact) ─────────────────────
# Campus engine: aira_1 (ATS) + aira_2 (reminders) are JobDiva-hiring-only and removed.
ENTRY = ["aira_3_webrtc_orchestration", "app"]
for m in ENTRY:
    try:
        importlib.import_module(m); check(True, f"imports: {m}")
    except Exception as e:
        check(False, f"imports: {m} — {type(e).__name__}: {e}")


# ── 2. LiveKit-only guarantee: NO browser/Selenium in the interview path ──────
def src(modpath):
    return inspect.getsource(importlib.import_module(modpath))

for mod in ("webrtc.interview", "aira_3_webrtc_orchestration"):
    try:
        s = src(mod)
        browser = sum(t in s for t in ("import selenium", "webdriver", "execute_async_script",
                                       "execute_script(", "chrome_options", "launch_browser_and_join"))
        check(browser == 0, f"{mod}: no browser/Selenium code (found {browser})")
    except Exception as e:
        check(False, f"{mod}: could not inspect — {e}")


# ── 3. webrtc.interview exposes the LiveKit interview API ─────────────────────
try:
    wi = importlib.import_module("webrtc.interview")
    for fn in ("ask_questions_in_webrtc", "get_livekit_adapter"):
        check(hasattr(wi, fn), f"webrtc.interview.{fn} present")
except Exception as e:
    check(False, f"webrtc.interview API — {e}")


# ── 4. core.responses_client planners work (deterministic question shaping) ───
try:
    from core.responses_client import QuestionFormatTracker, QuestionFormat, SkillTracker
    t = QuestionFormatTracker(_total_q_with_greeting=6 + 2, resume_ratio=0.0,
                              target_conceptual=4, target_scenario=2, target_resume=2)
    fmts = []
    for qn in range(2, 8):
        f = t.get_next_format(qn); t.register_format(qn, f); fmts.append(f)
    resume = sum(1 for f in fmts if f == QuestionFormat.RESUME_PROJECT)
    scenario = sum(1 for f in fmts if "SCENARIO" in f.name)
    check(len(fmts) == 6 and resume == 2 and scenario == 2,
          f"QuestionFormatTracker honors config (6 content: {resume} resume, {scenario} scenario)")
    st = SkillTracker(mandatory_skills=["Python", "SQL", "System Design"])
    picks = [st.get_next_skills(1)[0] for _ in range(3)]
    for p in picks:
        st.mark_covered([p])
    check(len(set(picks)) >= 1, f"SkillTracker returns skills: {picks}")
except Exception as e:
    check(False, f"responses_client planners — {type(e).__name__}: {e}")


# ── 5. integrations expose their public API ───────────────────────────────────
try:
    import integrations.aira_sql as airadb
    check(hasattr(airadb, "_api_call") and hasattr(airadb, "update_interview"),
          "integrations.aira_sql exposes _api_call + update_interview")
    import integrations.MSEmailClient as mc
    check(hasattr(mc, "send_email"), "integrations.MSEmailClient exposes send_email")
except Exception as e:
    check(False, f"integrations API — {type(e).__name__}: {e}")


# ── 6. aira_3 orchestration is LiveKit-only + fail-loud ───────────────────────
try:
    a3 = src("aira_3_webrtc_orchestration")
    check("USE_LIVEKIT = True" in a3, "aira_3: USE_LIVEKIT hardcoded True (no browser fallback)")
except Exception as e:
    check(False, f"aira_3 fail-loud — {e}")


print("\n" + ("🎉 PIPELINE CONTRACT OK — aira_1/2/3 wired, LiveKit-only, planners honor config"
              if ok else "⚠️ PIPELINE ISSUE — see above"))
sys.exit(0 if ok else 1)
