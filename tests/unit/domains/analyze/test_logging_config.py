"""Unit tests for logging configuration."""

from __future__ import annotations

import json
import logging

from src.logging_config import JSONFormatter, configure_logging


class TestJSONFormatter:
    def test_format_produces_valid_json(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello world",
            args=None,
            exc_info=None,
        )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "hello world"

    def test_format_with_exception_info(self) -> None:
        formatter = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="something failed",
            args=None,
            exc_info=exc_info,
        )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "exception" in parsed
        assert "ValueError" in parsed["exception"]
        assert "boom" in parsed["exception"]
        assert parsed["level"] == "ERROR"

    def test_format_with_request_id(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="request log",
            args=None,
            exc_info=None,
        )
        record.request_id = "req-abc-123"  # type: ignore[attr-defined]

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["request_id"] == "req-abc-123"

    def test_format_without_request_id_omits_field(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="no request id",
            args=None,
            exc_info=None,
        )

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "request_id" not in parsed

    def test_format_timestamp_is_iso(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.DEBUG,
            pathname="test.py",
            lineno=1,
            msg="check timestamp",
            args=None,
            exc_info=None,
        )

        output = formatter.format(record)
        parsed = json.loads(output)

        # ISO 8601 timestamps contain 'T' separator and timezone info
        assert "T" in parsed["timestamp"]


class TestConfigureLogging:
    def _cleanup_root_logger(self) -> None:
        """Remove all handlers from root logger to avoid test pollution."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)

    def test_json_logs_true_uses_json_formatter(self) -> None:
        self._cleanup_root_logger()
        configure_logging(json_logs=True)

        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JSONFormatter)

        self._cleanup_root_logger()

    def test_json_logs_false_uses_text_formatter(self) -> None:
        self._cleanup_root_logger()
        configure_logging(json_logs=False)

        root = logging.getLogger()
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert not isinstance(formatter, JSONFormatter)
        assert isinstance(formatter, logging.Formatter)
        # Text formatter should include asctime, name, levelname, message
        fmt_str = formatter._fmt or ""
        assert "%(asctime)s" in fmt_str
        assert "%(levelname)s" in fmt_str

        self._cleanup_root_logger()

    def test_configure_logging_sets_level_info(self) -> None:
        self._cleanup_root_logger()
        configure_logging(level="INFO")

        root = logging.getLogger()
        assert root.level == logging.INFO

        self._cleanup_root_logger()

    def test_configure_logging_sets_level_debug(self) -> None:
        self._cleanup_root_logger()
        configure_logging(level="DEBUG")

        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert root.handlers[0].level == logging.DEBUG

        self._cleanup_root_logger()

    def test_configure_logging_sets_level_warning(self) -> None:
        self._cleanup_root_logger()
        configure_logging(level="WARNING")

        root = logging.getLogger()
        assert root.level == logging.WARNING

        self._cleanup_root_logger()

    def test_configure_logging_case_insensitive_level(self) -> None:
        self._cleanup_root_logger()
        configure_logging(level="debug")

        root = logging.getLogger()
        assert root.level == logging.DEBUG

        self._cleanup_root_logger()

    def test_configure_logging_removes_existing_handlers(self) -> None:
        self._cleanup_root_logger()
        root = logging.getLogger()

        # Add a dummy handler
        dummy = logging.StreamHandler()
        root.addHandler(dummy)
        assert len(root.handlers) == 1

        configure_logging(json_logs=False)

        # The dummy handler should be removed and replaced with new one
        assert len(root.handlers) == 1
        assert root.handlers[0] is not dummy

        self._cleanup_root_logger()
