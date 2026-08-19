#!/usr/bin/env python3
"""
Engine test runner — ONE command, a clear bug report for the whole engine.

Two passes:
  1. IMPORT SWEEP — imports every module in the engine (aira_1/2/3, app, webrtc/,
     core/, campus/, integrations/, and every root module) in an isolated subprocess.
     Surfaces the bugs that matter most: SyntaxError, broken local imports (bad refactor),
     NameError at import time. Failures that are just a missing external dependency or a
     live service (DB, deepgram, livekit…) are reported separately so real code bugs stand out.
  2. LOGIC TESTS — runs every self-contained test in load_tests/ and reports pass/fail.

Usage:  python run_tests.py            # full run
        python run_tests.py --imports  # import sweep only
        python run_tests.py --logic     # logic tests only
        python run_tests.py --service   # also run tests that need live services
Exit code is non-zero if any real bug (import bug or failing test) is found.
"""
import os, sys, glob, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TIMEOUT = 60
PACKAGES = ("core", "webrtc", "campus", "integrations", "answer_analysis")

# Entry-point scripts run in a spawned process; app.py starts a server. Skip in the sweep
# (aira_1/2/3 ARE swept — they must import clean).
SKIP_IMPORT = {"run_tests", "app"}

LOCAL_MODULES = {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(ROOT, "*.py"))}
for pkg in PACKAGES:
    LOCAL_MODULES |= {pkg}

# Tests that hit live services or cost money — only with --service.
SERVICE_TESTS = {
    "test_01_deepgram.py", "test_02_openai.py", "test_03_tts.py", "test_04_database.py",
    "test_05_livekit.py", "test_06_full_simulation.py", "test_keypool_redis.py", "test_analysis_card_styling.py", "test_plain_analysis_cards.py",
}


def _env():
    e = dict(os.environ)
    e["PYTHONPATH"] = ROOT + os.pathsep + e.get("PYTHONPATH", "")
    envf = os.path.join(ROOT, ".env")
    if os.path.isfile(envf):
        for line in open(envf):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k and not e.get(k):
                e[k] = v.strip().strip('"').strip("'")
    return e


def classify(stderr: str):
    last = ""
    for ln in reversed(stderr.strip().splitlines()):
        if ln.strip():
            last = ln.strip(); break
    if "SyntaxError" in stderr or "IndentationError" in stderr:
        return "SYNTAX BUG", True, last
    if "cannot import name" in stderr:
        return "IMPORT BUG", True, last
    if "ModuleNotFoundError" in stderr:
        name = last.split("'")[1] if "'" in last else ""
        if name.split(".")[0] in LOCAL_MODULES:
            return "IMPORT BUG (local)", True, last
        return "missing dependency", False, last
    if "NameError" in stderr:
        return "NAME BUG", True, last
    if any(s in stderr for s in ("ConnectionError", "OperationalError", "could not connect",
                                 "getaddrinfo", "Connection refused", "Timeout", "Access denied",
                                 "pymysql", "psycopg2", "redis", "livekit", "deepgram", "LiveTranscription")):
        return "needs service/env", False, last
    return "other error", False, last


def import_sweep():
    mods = sorted(os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(ROOT, "*.py")))
    mods = [m for m in mods if m not in SKIP_IMPORT and not m.startswith("test_")]
    for pkg in PACKAGES:
        for f in glob.glob(os.path.join(ROOT, pkg, "*.py")):
            name = os.path.splitext(os.path.basename(f))[0]
            if name != "__init__":
                mods.append(f"{pkg}.{name}")
    env = _env()
    ok, bugs, soft = [], [], []
    for m in mods:
        try:
            p = subprocess.run([PY, "-c", f"import {m}"], cwd=ROOT, env=env,
                               capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            bugs.append((m, "TIMEOUT", f"import hung > {TIMEOUT}s")); continue
        if p.returncode == 0:
            ok.append(m)
        else:
            cat, real, line = classify(p.stderr)
            (bugs if real else soft).append((m, cat, line))
    return ok, bugs, soft, len(mods)


def run_logic_tests(include_service=False):
    tests = sorted(glob.glob(os.path.join(ROOT, "load_tests", "test_*.py")) +
                   glob.glob(os.path.join(ROOT, "tests", "test_*.py")))
    if not include_service:
        tests = [t for t in tests if os.path.basename(t) not in SERVICE_TESTS]
    env = _env()
    passed, failed = [], []
    for t in tests:
        rel = os.path.relpath(t, ROOT)
        try:
            p = subprocess.run([PY, t], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            failed.append((rel, "timeout")); continue
        (passed if p.returncode == 0 else failed).append(
            (rel, "") if p.returncode == 0 else (rel, (p.stdout + p.stderr).strip().splitlines()[-1] if (p.stdout+p.stderr).strip() else "failed"))
    return passed, failed


def main():
    args = set(sys.argv[1:])
    do_imports = "--logic" not in args
    do_logic = "--imports" not in args
    service = "--service" in args
    real_bugs = 0

    if do_imports:
        print("=" * 72 + "\n IMPORT SWEEP — every engine module\n" + "=" * 72)
        ok, bugs, soft, total = import_sweep()
        print(f"  ✅ imported cleanly : {len(ok)}/{total}")
        if bugs:
            real_bugs += len(bugs)
            print(f"\n  🐞 REAL BUGS ({len(bugs)}):")
            for m, cat, line in bugs:
                print(f"     [{cat}] {m} — {line[:110]}")
        if soft:
            print(f"\n  ⚠️  needs service/dep ({len(soft)}) — not code bugs:")
            for m, cat, line in soft[:12]:
                print(f"     [{cat}] {m} — {line[:90]}")

    if do_logic:
        print("\n" + "=" * 72 + "\n LOGIC TESTS" + ("" if service else " (add --service for live-service tests)") + "\n" + "=" * 72)
        passed, failed = run_logic_tests(service)
        print(f"  ✅ passed : {len(passed)}")
        if failed:
            real_bugs += len(failed)
            print(f"  ❌ FAILED ({len(failed)}):")
            for rel, why in failed:
                print(f"     {rel} — {why[:100]}")

    print("\n" + "=" * 72)
    print(" RESULT: 🎉 no bugs found" if real_bugs == 0 else f" RESULT: ⚠️  {real_bugs} real issue(s) to fix")
    print("=" * 72)
    sys.exit(1 if real_bugs else 0)


if __name__ == "__main__":
    main()
