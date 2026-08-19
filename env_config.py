"""
env_config.py — Single source of truth for environment detection & service URLs.

Environment is determined by `AIRA_ENV` in .env (not by hostname or IP).
All service URLs use unified variable names — each VM's .env has correct values.

Usage:
    from env_config import DEPLOYMENT, SENTINEL_BASE_URL, CONNECT_BASE_URL, ...
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# Environment Detection — driven by .env, NOT by hostname or IP
# ═══════════════════════════════════════════════════════════════════════════════
AIRA_ENV = os.getenv("AIRA_ENV", "development").lower()
IS_PRODUCTION = AIRA_ENV == "production"
IS_DEVELOPMENT = not IS_PRODUCTION
DEPLOYMENT = "PRODUCTION" if IS_PRODUCTION else "DEVELOPMENT"

# ═══════════════════════════════════════════════════════════════════════════════
# Service URLs — same env var name on every VM, different values per .env
# ═══════════════════════════════════════════════════════════════════════════════

# Sentinel monitoring service
SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL", "http://localhost:8099")

# Spring Boot Engine API (used by aira_sql.py for DB operations via API)
ENGINE_API_BASE = os.getenv("ENGINE_API_BASE", "http://localhost:9090/api/engine")

# Aira Connect scheduler
AIRA_SCHEDULER_URL = os.getenv("AIRA_SCHEDULER_URL", "https://devconnect.airahr.ai/api/meetings/schedule")

# Aira Hub (Java backend)
AIRA_HUB_URL = os.getenv("AIRA_HUB_URL", "http://localhost:9090")
AIRA_HUB_SLOTS_URL = os.getenv("AIRA_HUB_SLOTS_URL", f"{AIRA_HUB_URL}/api/slots/available")

# Connect frontend base URL — used for room API calls (/api/rooms/, /api/meeting/, /api/proctor-log/)
# For VMs using path-based routing (e.g. dev.airahr.ai/connect/...), include the path prefix.
CONNECT_BASE_URL = os.getenv("CONNECT_BASE_URL", "https://devconnect.airahr.ai")

# Echo telephonic screening API base
ECHO_API_BASE = os.getenv("ECHO_API_BASE", "http://localhost:9090/api/echo")

# ═══════════════════════════════════════════════════════════════════════════════
# Operational Config
# ═══════════════════════════════════════════════════════════════════════════════
CRON_JOB_TIMING = int(os.getenv("CRON_JOB_TIMING", "30" if IS_PRODUCTION else "60"))

# ═══════════════════════════════════════════════════════════════════════════════
# Startup log
# ═══════════════════════════════════════════════════════════════════════════════
logger.info(
    "🔧 env_config loaded: AIRA_ENV=%s, DEPLOYMENT=%s, SENTINEL=%s, ENGINE_API=%s, CONNECT=%s",
    AIRA_ENV, DEPLOYMENT, SENTINEL_BASE_URL, ENGINE_API_BASE, CONNECT_BASE_URL,
)
