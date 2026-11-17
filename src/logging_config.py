"""Structured logging configuration with JSON support."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": "analysis_service",
        }
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        for field in ("job_id", "tenant_id"):
            value = getattr(record, field, None)
            if value:
                log_entry[field] = value
        return json.dumps(log_entry, default=str)


SERVICE_NAME = "analysis_service"


def configure_logging(json_logs: bool = False, level: str = "INFO") -> None:
    """Configure root logger with either JSON or text formatting."""
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level.upper())

    if json_logs:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )

    root.addHandler(handler)


def configure_structlog() -> None:
    """Configure structlog with JSON output, timestamps, and correlation_id binding.

    Sets up structlog processors for structured JSON logging with
    automatic context binding (correlation_id, service name).
    Falls back gracefully if structlog is not installed.
    """
    try:
        import structlog

        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )

        structlog.contextvars.bind_contextvars(service=SERVICE_NAME)
    except ImportError:
        logging.getLogger(__name__).warning(
            "structlog not installed, falling back to stdlib logging"
        )
