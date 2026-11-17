"""Structured audit logging for LLM interactions."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("analyzer_service.llm_audit")


class LLMAuditLogger:
    """Log LLM request/response pairs for debugging and compliance."""

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough token estimate: ~4 chars per token for English text."""
        return max(1, len(text) // 4)

    @staticmethod
    def log_request(
        job_id: str,
        model: str,
        prompt_tokens: int,
        prompt_preview: str,
    ) -> float:
        """Log an LLM request. Returns the start time for duration tracking.

        Args:
            job_id: The analysis job ID.
            model: The LLM model name.
            prompt_tokens: Estimated prompt token count.
            prompt_preview: First N chars of the prompt.

        Returns:
            Monotonic start time for duration calculation.
        """
        logger.info(
            "LLM_REQUEST: job_id=%s model=%s prompt_tokens=%d prompt_preview=%s",
            job_id,
            model,
            prompt_tokens,
            prompt_preview[:500],
        )
        return time.monotonic()

    @staticmethod
    def log_response(
        job_id: str,
        model: str,
        response_tokens: int,
        duration_ms: float,
        raw_preview: str,
        prompt_tokens_actual: int | None = None,
        completion_tokens_actual: int | None = None,
        total_tokens_actual: int | None = None,
    ) -> None:
        """Log an LLM response.

        Args:
            job_id: The analysis job ID.
            model: The LLM model name.
            response_tokens: Estimated response token count (heuristic fallback).
            duration_ms: Time elapsed in milliseconds.
            raw_preview: First N chars of the response.
            prompt_tokens_actual: Actual prompt tokens from API usage, if available.
            completion_tokens_actual: Actual completion tokens from API usage, if available.
            total_tokens_actual: Actual total tokens from API usage, if available.
        """
        usage_extra = ""
        if total_tokens_actual is not None:
            usage_extra = (
                f" prompt_tokens_actual={prompt_tokens_actual}"
                f" completion_tokens_actual={completion_tokens_actual}"
                f" total_tokens_actual={total_tokens_actual}"
            )
        logger.info(
            "LLM_RESPONSE: job_id=%s model=%s response_tokens=%d duration_ms=%.1f%s raw_preview=%s",
            job_id,
            model,
            response_tokens,
            duration_ms,
            usage_extra,
            raw_preview[:500],
        )

    @staticmethod
    def log_failure(
        job_id: str,
        model: str,
        error: str,
        duration_ms: float | None = None,
    ) -> None:
        """Log an LLM failure.

        Args:
            job_id: The analysis job ID.
            model: The LLM model name.
            error: Error description.
            duration_ms: Time elapsed before failure, if available.
        """
        extra = f" duration_ms={duration_ms:.1f}" if duration_ms is not None else ""
        logger.warning(
            "LLM_FAILURE: job_id=%s model=%s error=%s%s",
            job_id,
            model,
            error,
            extra,
        )
