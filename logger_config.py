import logging
from logging.handlers import TimedRotatingFileHandler
import os

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "application.log")

def setup_logging():
    if not any(isinstance(h, TimedRotatingFileHandler) for h in logging.getLogger().handlers):
        handler = TimedRotatingFileHandler(
            LOG_FILE, when="midnight", interval=1, backupCount=90, encoding="utf-8"
        )
        handler.suffix = "%Y-%m-%d"
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        )
        handler.setFormatter(formatter)

        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root_logger.addHandler(console)
