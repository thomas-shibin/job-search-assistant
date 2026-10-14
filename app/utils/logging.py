from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """Renders each log record as one JSON line: {timestamp, level, event, logger, ...fields}.
    Never include secrets or full personal documents in `fields` — only identifiers
    (job_id, source name, counts) per the project's logging policy.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        extra_fields = getattr(record, "extra_fields", None)
        if extra_fields:
            payload.update(extra_fields)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    root.addHandler(handler)
    # httpx logs every request URL at INFO, and some carry secrets in the URL
    # (the Telegram bot token, the job-board app key): keep only its warnings.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """Emit a structured event, e.g. log_event(logger, "JOB_DISCOVERED", job_id=42, source="greenhouse")."""
    logger.log(level, event, extra={"extra_fields": fields})
