"""
load_tests/config.py  —  Shared configuration for all AIRA load tests.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ENV_FILE = Path(__file__).parent.parent / ".env"
load_dotenv(ENV_FILE, override=True)

# ── API credentials ────────────────────────────────────────────────────
DEEPGRAM_API_KEY   = os.environ["DEEPGRAM_API_KEY"]
OPENAI_API_KEY     = os.environ["OPENAI_API_KEY"]
CARTESIA_API_KEY   = os.environ["CARTESIA_API_KEY"]
CARTESIA_VOICE_ID  = os.environ.get("CARTESIA_VOICE_ID", "b7d50908-b17c-442d-ad8d-810c63997ed9")
LIVEKIT_URL        = os.environ["LIVEKIT_URL"]
LIVEKIT_API_KEY    = os.environ["LIVEKIT_API_KEY"]
LIVEKIT_API_SECRET = os.environ["LIVEKIT_API_SECRET"]
CONNECT_BASE_URL   = os.environ.get("CONNECT_BASE_URL", "https://devconnect.airacampus.com")

# ── Database ───────────────────────────────────────────────────────────
DB_HOST = os.environ.get("MYSQL_HOST", "localhost")
DB_PORT = int(os.environ.get("MYSQL_PORT", 3306))
DB_USER = os.environ.get("MYSQL_USER", "root")
DB_PASS = os.environ.get("MYSQL_PASS", "")
DB_NAME = os.environ.get("MYSQL_DB",   "aira_db")

# ── Audio constants ────────────────────────────────────────────────────
AUDIO_SAMPLE_RATE   = 48_000
AUDIO_CHANNELS      = 1
AUDIO_FRAME_MS      = 10
AUDIO_FRAME_SAMPLES = AUDIO_SAMPLE_RATE * AUDIO_FRAME_MS // 1000   # 480

# ── Model identifiers ──────────────────────────────────────────────────
OPENAI_MODEL      = "gpt-4o-mini"
OPENAI_EVAL_MODEL = "gpt-4o"
CARTESIA_MODEL    = "sonic-2"
DEEPGRAM_MODEL    = "nova-2"
DEEPGRAM_ENCODING = "linear16"
DEEPGRAM_WS_URL   = (
    f"wss://api.deepgram.com/v1/listen"
    f"?model={DEEPGRAM_MODEL}&encoding={DEEPGRAM_ENCODING}"
    f"&sample_rate={AUDIO_SAMPLE_RATE}&channels={AUDIO_CHANNELS}"
    f"&interim_results=true&endpointing=300&utterance_end_ms=2000"
)

# ── Test defaults ──────────────────────────────────────────────────────
DEFAULT_CONCURRENCY = 10
DEFAULT_DURATION_S  = 30

REPORTS_DIR     = Path(__file__).parent / "reports"
RECORDINGS_DIR  = Path(__file__).parent / "recordings"
SIM_REPORTS_DIR = Path(__file__).parent / "sim_reports"
LOGS_DIR        = Path(__file__).parent.parent / "logs" / "load_tests"

REPORTS_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(exist_ok=True)
SIM_REPORTS_DIR.mkdir(exist_ok=True)


# ── Per-test output cleanup ────────────────────────────────────────────
import shutil as _shutil
import logging as _logging

_cleanup_log = _logging.getLogger("cleanup")

# Maps test stem → glob patterns (relative to project root).
# Prefix "DIR:" to wipe whole directories matching a glob.
_TEST_OUTPUTS: dict = {
    "test_01_deepgram": [
        "load_tests/reports/deepgram_*.json",
        "load_tests/reports/deepgram_*.md",
        "load_tests/reports/deepgram_*.png",
    ],
    "test_02_openai": [
        "load_tests/reports/openai_*.json",
        "load_tests/reports/openai_*.md",
        "load_tests/reports/openai_*.png",
    ],
    "test_03_tts": [
        "load_tests/reports/cartesia_*.json",
        "load_tests/reports/cartesia_*.md",
        "load_tests/reports/cartesia_*.png",
    ],
    "test_04_database": [
        "load_tests/reports/database_*.json",
        "load_tests/reports/database_*.md",
        "load_tests/reports/database_*.png",
    ],
    "test_05_livekit": [
        "load_tests/reports/livekit_*.json",
        "load_tests/reports/livekit_*.md",
        "load_tests/reports/livekit_*.png",
    ],
    "test_06_full_simulation": [
        "load_tests/recordings/*.mp4",
        "load_tests/recordings/*.wav",
        "load_tests/sim_reports/*.html",
        "load_tests/reports/simulation.log",
        "load_tests/reports/full_simulation_*.md",
        "load_tests/reports/full_simulation_*.json",
        "load_tests/reports/master_report_*.html",
        "DIR:logs/load_tests/test_*",
    ],
    "magic_deepgram": [
        "load_tests/reports/magic_deepgram.log",
        "load_tests/reports/deepgram_*.json",
        "load_tests/reports/deepgram_*.md",
        "load_tests/reports/deepgram_*.png",
    ],
    "magic_openai": [
        "load_tests/reports/magic_openai.log",
        "load_tests/reports/openai_*.json",
        "load_tests/reports/openai_*.md",
        "load_tests/reports/openai_*.png",
    ],
    "magic_cartesia": [
        "load_tests/reports/magic_cartesia.log",
        "load_tests/reports/cartesia_*.json",
        "load_tests/reports/cartesia_*.md",
        "load_tests/reports/cartesia_*.png",
    ],
    "magic_livekit": [
        "load_tests/reports/magic_livekit.log",
        "load_tests/reports/livekit_*.json",
        "load_tests/reports/livekit_*.md",
        "load_tests/reports/livekit_*.png",
    ],
    "find_magic_numbers": [
        "load_tests/reports/find_magic_numbers.log",
        "load_tests/reports/MAGIC_NUMBERS_*.md",
        "load_tests/reports/MAGIC_NUMBERS_*.png",
        "load_tests/reports/MAGIC_NUMBERS_FINAL*",
        "load_tests/reports/MAGIC_NUMBERS_MASTER*",
        "load_tests/reports/magic_numbers_chart.png",
    ],
    "run_all": [
        "load_tests/reports/run_all.log",
    ],
}

# Project root = two levels up from this file (load_tests/config.py)
_PROJECT_ROOT = Path(__file__).parent.parent


def cleanup_previous_outputs(test_file: str) -> None:
    """
    Delete all output files/dirs from the previous run of a given test.

    Call at the very start of each test's main():
        from .config import cleanup_previous_outputs
        cleanup_previous_outputs(__file__)

    Parameters
    ----------
    test_file : str
        Pass __file__ from the calling test. The stem (e.g. 'test_06_full_simulation')
        is used to look up which outputs to clean.
    """
    stem     = Path(test_file).stem
    patterns = _TEST_OUTPUTS.get(stem, [])

    if not patterns:
        _cleanup_log.debug("No cleanup rules for %s — skipping", stem)
        return

    deleted = 0
    for pat in patterns:
        if pat.startswith("DIR:"):
            dir_glob = pat[4:]
            for d in _PROJECT_ROOT.glob(dir_glob):
                if d.is_dir():
                    _shutil.rmtree(d, ignore_errors=True)
                    _cleanup_log.info("🗑  Removed dir : %s", d.relative_to(_PROJECT_ROOT))
                    deleted += 1
        else:
            for f in _PROJECT_ROOT.glob(pat):
                if f.is_file():
                    f.unlink(missing_ok=True)
                    _cleanup_log.info("🗑  Removed file: %s", f.relative_to(_PROJECT_ROOT))
                    deleted += 1

    if deleted:
        print(f"🧹 [{stem}] Cleaned {deleted} previous output(s)")
    else:
        print(f"🧹 [{stem}] Nothing to clean (first run or already clean)")
