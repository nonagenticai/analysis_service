"""Tests for LLMAuditLogger."""

import logging

from src.domains.analyze.services.llm_audit import LLMAuditLogger


class TestLLMAuditLogger:
    def test_estimate_tokens(self):
        """Token estimation should approximate ~4 chars per token."""
        audit = LLMAuditLogger()
        assert audit.estimate_tokens("hello world") >= 1
        assert audit.estimate_tokens("a" * 400) == 100
        assert audit.estimate_tokens("") == 1

    def test_log_request(self, caplog):
        """log_request should emit structured log and return start time."""
        audit = LLMAuditLogger()
        with caplog.at_level(logging.INFO, logger="analyzer_service.llm_audit"):
            start = audit.log_request(
                job_id="job-1",
                model="test-model",
                prompt_tokens=250,
                prompt_preview="Analyze this website...",
            )
        assert isinstance(start, float)
        assert start > 0
        assert "LLM_REQUEST" in caplog.text
        assert "job-1" in caplog.text
        assert "test-model" in caplog.text

    def test_log_response(self, caplog):
        """log_response should emit structured log with duration."""
        audit = LLMAuditLogger()
        with caplog.at_level(logging.INFO, logger="analyzer_service.llm_audit"):
            audit.log_response(
                job_id="job-2",
                model="test-model",
                response_tokens=500,
                duration_ms=1234.5,
                raw_preview='{"description": "A test site"}',
            )
        assert "LLM_RESPONSE" in caplog.text
        assert "job-2" in caplog.text
        assert "1234.5" in caplog.text

    def test_log_failure(self, caplog):
        """log_failure should emit warning-level log."""
        audit = LLMAuditLogger()
        with caplog.at_level(logging.WARNING, logger="analyzer_service.llm_audit"):
            audit.log_failure(
                job_id="job-3",
                model="test-model",
                error="Connection timeout",
                duration_ms=5000.0,
            )
        assert "LLM_FAILURE" in caplog.text
        assert "job-3" in caplog.text
        assert "Connection timeout" in caplog.text

    def test_log_failure_without_duration(self, caplog):
        """log_failure should work without duration_ms."""
        audit = LLMAuditLogger()
        with caplog.at_level(logging.WARNING, logger="analyzer_service.llm_audit"):
            audit.log_failure(
                job_id="job-4",
                model="test-model",
                error="API key invalid",
            )
        assert "LLM_FAILURE" in caplog.text
        assert "API key invalid" in caplog.text
