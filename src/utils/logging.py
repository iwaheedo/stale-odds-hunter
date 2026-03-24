from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter. Never logs sensitive fields."""

    SENSITIVE_KEYS = {"private_key", "secret", "passphrase", "password", "api_key"}

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_data"):
            log_entry["data"] = self._sanitize(record.extra_data)  # type: ignore[assignment]
        return json.dumps(log_entry)

    def _sanitize(self, data: dict) -> dict:
        return {
            k: "***REDACTED***" if k.lower() in self.SENSITIVE_KEYS else v
            for k, v in data.items()
        }


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear only StreamHandler instances (preserve external handlers like BotLogHandler)
    root.handlers = [h for h in root.handlers if not isinstance(h, logging.StreamHandler)]

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")
        )
    root.addHandler(handler)

    # Suppress noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"soh.{name}")
