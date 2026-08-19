"""
Central logging configuration for the campus engine.

One place to control log format, level, and destinations, so logging is consistent and
production-grade instead of ad-hoc per file. Call `setup_logging()` once at process start
(entry points / scripts); everywhere else just use `get_logger(__name__)`.

Env knobs:
  CAMPUS_LOG_LEVEL   — DEBUG | INFO | WARNING | ERROR         (default INFO)
  CAMPUS_LOG_FILE    — path to a rotating log file             (default: none, stderr only)
  CAMPUS_LOG_JSON    — 1 to emit JSON lines (for log aggregators)
  CAMPUS_LOG_QUIET   — comma-separated noisy loggers to pin at WARNING
                       (default: openai,httpx,httpcore,urllib3,deepgram,websockets)
"""
import os
import sys
import json
import logging
import logging.handlers

_DEF_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DEF_DATEFMT = "%Y-%m-%d %H:%M:%S"
_QUIET_DEFAULT = "openai,httpx,httpcore,urllib3,deepgram,websockets,asyncio"
_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, _DEF_DATEFMT),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = None, force: bool = False) -> None:
    """Configure the root logger once. Idempotent unless force=True."""
    global _configured
    if _configured and not force:
        return

    level_name = (level or os.getenv("CAMPUS_LOG_LEVEL", "INFO")).upper()
    lvl = getattr(logging, level_name, logging.INFO)

    if os.getenv("CAMPUS_LOG_JSON") == "1":
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(_DEF_FMT, _DEF_DATEFMT)

    handlers: list = [logging.StreamHandler(sys.stderr)]
    log_file = os.getenv("CAMPUS_LOG_FILE")
    if log_file:
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=25 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handlers.append(fh)
    for h in handlers:
        h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(lvl)
    root.handlers[:] = handlers

    # Pin noisy third-party loggers so INFO stays readable.
    quiet = os.getenv("CAMPUS_LOG_QUIET", _QUIET_DEFAULT)
    for name in (n.strip() for n in quiet.split(",") if n.strip()):
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger("logging_config").info(
        "🪵 Logging configured: level=%s json=%s file=%s",
        level_name, os.getenv("CAMPUS_LOG_JSON") == "1", log_file or "-",
    )


def get_logger(name: str) -> logging.Logger:
    """Module logger; ensures logging is configured even if setup wasn't called."""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
