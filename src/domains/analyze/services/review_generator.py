"""Review generation service using the worker_management LLM gateway."""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from typing import TYPE_CHECKING, Any

import httpx

from src.config import get_settings
from src.domains.analyze.services.llm_audit import LLMAuditLogger
from src.domains.analyze.services.prompt_builder import PromptBuilder
from src.domains.analyze.services.response_parser import parse_json_from_response
from src.llm_client import get_wm_client
from src.llm_credentials import model_override_from
from src.metrics import analyzer_llm_calls_total, analyzer_llm_latency_seconds

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.analyze.schemas import LLMConfigOverride

logger = logging.getLogger(__name__)


# Required keys per review type for schema validation
_REQUIRED_KEYS: dict[str, list[str]] = {
    "design": ["overall_score", "categories", "strengths", "weaknesses", "recommendations"],
    "usability": [
        "overall_score",
        "categories",
        "critical_issues",
        "quick_wins",
        "long_term_improvements",
    ],
    "feature": [
        "vertical",
        "feature_completeness_score",
        "present_features",
        "missing_features",
        "differentiators",
        "suggestions",
    ],
    "content_quality": [
        "overall_score",
        "categories",
        "strengths",
        "issues",
        "recommendations",
    ],
}


_REVIEW_SYSTEM_PROMPT = (
    "You are a senior website reviewer. Reply with a single JSON object that "
    "matches the structure asked for in the prompt. Output ONLY the JSON "
    "object, no markdown fences, no commentary."
)


def _worker_for(review_type: str) -> str:
    """Return the WM worker name for a given review type."""
    return f"analysis-review-{review_type}"


def _normalize_output(output: Any) -> tuple[dict[str, Any] | list[Any], str]:
    """Normalize a WM ``output`` payload into ``(parsed, raw_preview)``."""
    if isinstance(output, (dict, list)):
        return output, json.dumps(output)
    if isinstance(output, str):
        parsed = parse_json_from_response(output)
        if isinstance(parsed, (dict, list)):
            return parsed, output
        raise ValueError("Review response is not JSON object/array")
    raise ValueError(f"Unexpected WM output type: {type(output).__name__}")


class ReviewGenerator:
    """Generate scored review assessments via worker_management.

    Attributes:
        prompt_builder: Jinja2-based prompt renderer.
        audit: LLM call audit logger.
    """

    def __init__(self, wm_client: WMClient | None = None) -> None:
        self.prompt_builder = PromptBuilder()
        self.audit = LLMAuditLogger()
        self._wm = wm_client if wm_client is not None else get_wm_client()

    async def generate_review(
        self,
        review_type: str,
        analysis_result: dict,
        user_context: dict | None = None,
        job_id: str = "",
        custom_prompt: str | None = None,
        llm_config: LLMConfigOverride | None = None,
    ) -> dict:
        """Generate a single review for the given type."""
        settings = get_settings()
        wm_cfg = settings.wm
        model_override = model_override_from(llm_config)
        model_label = model_override or "wm-default"
        max_retries = wm_cfg.max_retries
        retry_delay = wm_cfg.retry_delay
        worker_name = _worker_for(review_type)

        pages = analysis_result.get("pages", [])
        site_analysis = analysis_result.get("site_analysis")
        narrative = analysis_result.get("narrative_extraction")

        if custom_prompt:
            prompt = custom_prompt
        else:
            prompt = self.prompt_builder.build_review_prompt(
                review_type=review_type,
                pages=pages,
                site_analysis=site_analysis,
                narrative=narrative,
                user_context=user_context,
            )

        logger.info(
            "Starting WM review generation: job_id=%s type=%s worker=%s",
            job_id,
            review_type,
            worker_name,
        )
        prompt_tokens = self.audit.estimate_tokens(prompt)
        artifacts: list[dict] = []
        llm_calls = 0
        last_exc: Exception | None = None

        for attempt in range(1 + max_retries):
            llm_calls += 1
            start_time = _time.monotonic()
            try:
                self.audit.log_request(
                    job_id=job_id,
                    model=model_label,
                    prompt_tokens=prompt_tokens,
                    prompt_preview=prompt[:500],
                )
                response = await self._wm.message(
                    worker_name=worker_name,
                    message=prompt,
                    system_prompt=_REVIEW_SYSTEM_PROMPT,
                    model_override=model_override,
                )
                duration = _time.monotonic() - start_time
                analyzer_llm_latency_seconds.observe(duration)
                analyzer_llm_calls_total.labels(status="success").inc()

                parsed, raw_preview = _normalize_output(response.output)
                duration_ms = duration * 1000
                response_tokens = self.audit.estimate_tokens(raw_preview)

                usage = response.usage_metrics or {}
                prompt_tokens_actual = usage.get("prompt_tokens")
                completion_tokens_actual = usage.get("completion_tokens")
                total_tokens_actual = usage.get("total_tokens")

                effective_prompt_tokens = (
                    prompt_tokens_actual if prompt_tokens_actual is not None else prompt_tokens
                )
                effective_response_tokens = (
                    completion_tokens_actual
                    if completion_tokens_actual is not None
                    else response_tokens
                )

                self.audit.log_response(
                    job_id=job_id,
                    model=model_label,
                    response_tokens=response_tokens,
                    duration_ms=duration_ms,
                    raw_preview=raw_preview[:500],
                    prompt_tokens_actual=prompt_tokens_actual,
                    completion_tokens_actual=completion_tokens_actual,
                    total_tokens_actual=total_tokens_actual,
                )
                artifacts.append(
                    {
                        "type": "llm_call",
                        "model": model_label,
                        "review_type": review_type,
                        "prompt_tokens": effective_prompt_tokens,
                        "response_tokens": effective_response_tokens,
                        "prompt_tokens_estimated": prompt_tokens,
                        "response_tokens_estimated": response_tokens,
                        "total_tokens_actual": total_tokens_actual,
                        "duration_ms": round(duration_ms, 1),
                        "status": "success",
                    }
                )

                validation_errors = self._validate_schema(review_type, parsed)
                if validation_errors:
                    logger.warning(
                        "Review %s schema validation warnings: %s",
                        review_type,
                        validation_errors,
                    )

                logger.info(
                    "WM review generation succeeded: job_id=%s type=%s duration=%.1fms",
                    job_id,
                    review_type,
                    duration_ms,
                )
                return {
                    "review_type": review_type,
                    "result": parsed,
                    "llm_calls": llm_calls,
                    "artifacts": artifacts,
                    "validation_errors": validation_errors,
                }

            except (
                OSError,
                ValueError,
                RuntimeError,
                TimeoutError,
                httpx.HTTPError,
            ) as exc:
                last_exc = exc
                duration_ms = (_time.monotonic() - start_time) * 1000
                self.audit.log_failure(
                    job_id=job_id,
                    model=model_label,
                    error=str(exc),
                    duration_ms=duration_ms,
                )
                artifacts.append(
                    {
                        "type": "llm_call",
                        "model": model_label,
                        "review_type": review_type,
                        "prompt_tokens": prompt_tokens,
                        "duration_ms": round(duration_ms, 1),
                        "status": "failure",
                        "error": str(exc),
                    }
                )
                analyzer_llm_calls_total.labels(status="failure").inc()
                if attempt < max_retries:
                    logger.warning(
                        "Review %s attempt %d/%d failed, retrying in %.1fs: %s",
                        review_type,
                        attempt + 1,
                        1 + max_retries,
                        retry_delay,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)

        logger.error(
            "Review %s failed after %d attempts: %s",
            review_type,
            1 + max_retries,
            last_exc,
        )
        logger.warning(
            "Returning empty review result as fallback: job_id=%s type=%s",
            job_id,
            review_type,
        )
        return {
            "review_type": review_type,
            "result": {},
            "llm_calls": llm_calls,
            "artifacts": artifacts,
            "error": str(last_exc),
        }

    async def generate_reviews(
        self,
        review_types: list[str],
        analysis_result: dict,
        user_context: dict | None = None,
        job_id: str = "",
        llm_config: LLMConfigOverride | None = None,
    ) -> dict[str, dict]:
        """Generate multiple reviews in parallel.

        Partial success is honoured: review types that raise are dropped from the
        returned mapping instead of discarding the (expensive) reviews that already
        completed. Only a total wipe-out re-raises, so a job can never report
        "completed" with zero reviews.
        """
        tasks = [
            self.generate_review(
                review_type=rt,
                analysis_result=analysis_result,
                user_context=user_context,
                job_id=job_id,
                llm_config=llm_config,
            )
            for rt in review_types
        ]
        settled = await asyncio.gather(*tasks, return_exceptions=True)

        reviews: dict[str, dict] = {}
        failures: list[BaseException] = []
        for review_type, outcome in zip(review_types, settled, strict=True):
            if isinstance(outcome, BaseException):
                # Cancellation is not a review failure — it must propagate untouched.
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                logger.exception(
                    "Review %s raised, dropping it from the result set: job_id=%s",
                    review_type,
                    job_id,
                    exc_info=outcome,
                )
                failures.append(outcome)
                continue
            # Key on the REQUESTED type, not the echoed `outcome["review_type"]`.
            # The caller derives the failed set as `rt not in results`, so the two
            # must use the same vocabulary — were they ever to diverge, a review
            # that succeeded would be reported missing and counted as a failure.
            reviews[review_type] = outcome

        if failures and not reviews:
            raise failures[0]
        return reviews

    @staticmethod
    def _validate_schema(review_type: str, data: dict[str, object] | list[object]) -> list[str]:
        """Validate that the review data contains required keys."""
        required = _REQUIRED_KEYS.get(review_type, [])
        errors = []
        if isinstance(data, dict):
            for key in required:
                if key not in data:
                    errors.append(f"Missing required key: {key}")
        else:
            errors.append("Top-level review payload is not an object")
        return errors
