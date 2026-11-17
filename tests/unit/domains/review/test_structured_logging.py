"""Tests for src.logging_config -- structured logging configuration."""

import json
import logging

from src.logging_config import JSONFormatter, configure_logging, configure_structlog


class TestJSONFormatter:
    def test_json_output_format(self):
        """JSONFormatter should produce valid JSON with required fields."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "test message"
        assert data["service"] == "analysis_service"
        assert "timestamp" in data

    def test_optional_fields_included(self):
        """JSONFormatter should include job_id and tenant_id when set."""
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.job_id = "job-456"  # type: ignore[attr-defined]
        record.tenant_id = "acaae733-5126-5b47-9af1-aedd15e840e1"  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert data["job_id"] == "job-456"
        assert data["tenant_id"] == "acaae733-5126-5b47-9af1-aedd15e840e1"


class TestConfigureLogging:
    def test_configure_json_logging(self):
        """configure_logging with json_logs=True should use JSONFormatter."""
        configure_logging(json_logs=True, level="DEBUG")
        root = logging.getLogger()
        assert any(isinstance(h.formatter, JSONFormatter) for h in root.handlers)

    def test_configure_plain_logging(self):
        """configure_logging with json_logs=False should use standard formatter."""
        configure_logging(json_logs=False, level="INFO")
        root = logging.getLogger()
        assert root.handlers
        # Should not be JSONFormatter
        assert not any(isinstance(h.formatter, JSONFormatter) for h in root.handlers)


class TestConfigureStructlog:
    def test_configure_structlog_no_error(self):
        """configure_structlog should not raise even if structlog is unavailable."""
        # This tests the import guard -- should never raise
        configure_structlog()

    def test_structlog_bound_context(self):
        """After configure_structlog, service should be bound in context."""
        try:
            import structlog

            configure_structlog()
            ctx = structlog.contextvars.get_contextvars()
            assert ctx.get("service") == "analysis_service"
        except ImportError:
            pass  # structlog not installed, skip
