"""Structured (JSON lines) logging.

Every log line is one JSON object, so it can be shipped to ELK / CloudWatch /
SLS and filtered by provider, account, operation, attempt, etc.

Usage:
    log = get_logger("evdc.sync")
    log.info("sync finished", extra={"ctx": {"provider": "aws", "count": 12}})
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_configured = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO", stream=None) -> None:
    global _configured
    root = logging.getLogger("evdc")
    root.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
        root.propagate = False
        _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("evdc") else f"evdc.{name}")
