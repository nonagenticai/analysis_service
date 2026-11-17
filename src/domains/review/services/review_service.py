"""Main review service - generates AI-powered qualitative reviews via WM."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.config import ScoringSettings
    from src.domains.review.services.prompt_loader import PromptTemplateLoader

from pydantic import BaseModel

from src.config import get_settings
from src.domains.review.schemas import (
    AnalysisInput,
    LLMConfigOverride,
    SiteAnalysisInput,
    UserContext,
)
from src.domains.review.services.constants import DEPENDENT_REVIEW_TYPES, KEY_ALIASES, NA_VALUES
from src.domains.review.services.llm_output_models import (
    LLM_OUTPUT_MODELS,
    insufficient_data_result,
    validate_llm_output,
)
from src.llm_client import get_wm_client
from src.llm_credentials import model_override_from
from src.metrics import review_errors_total, review_llm_latency_seconds, review_llm_tokens_total

try:
    import structlog

    logger = structlog.get_logger(__name__)
except ImportError:
    logger = logging.getLogger(__name__)


def _get_tracer() -> Any:
    """Get OpenTelemetry tracer if available, else return a no-op."""
    try:
        from opentelemetry import trace

        return trace.get_tracer(__name__)
    except ImportError:
        return None


class _NullContext:
    """Minimal no-op context manager for when tracing is disabled."""

    def __enter__(self) -> _NullContext:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class ReviewOutputValue(BaseModel):
    """Typed wrapper for a single review output value, replacing isinstance() duck-typing."""

    value: str | list | dict | float | int | bool | None = None


class ReviewOutput(BaseModel):
    """Pydantic model for validating review output values in NA detection."""

    values: dict[str, ReviewOutputValue] = {}


def _is_all_na(data: dict) -> bool:
    """Check if all non-meta values in the response are N/A or empty."""
    meaningful_values = {k: v for k, v in data.items() if not k.startswith("_")}
    if not meaningful_values:
        return True

    validated = ReviewOutput.model_validate(
        {"values": {k: {"value": v} for k, v in meaningful_values.items()}}
    )

    for entry in validated.values.values():
        v = entry.value
        if v is None:
            continue
        if isinstance(v, str) and v.strip().lower() in NA_VALUES:
            continue
        if isinstance(v, list) and len(v) == 0:
            continue
        if isinstance(v, dict) and len(v) == 0:
            continue
        return False
    return True


_REVIEW_SYSTEM_PROMPT = (
    "You are a senior website reviewer. Reply with a single JSON object that "
    "matches the schema described in the prompt. Return ONLY the JSON object, "
    "no markdown fences, no commentary."
)


def _worker_for_review(review_type: str) -> str:
    """Return the WM worker name for a given review type."""
    return f"review-{review_type}"


def _coerce_response(output: object) -> str:
    """Coerce a WM ``output`` (dict or string) into a JSON string for parsing."""
    if isinstance(output, (dict, list)):
        return json.dumps(output)
    if isinstance(output, str):
        return output
    raise ValueError(f"Unexpected WM output type: {type(output).__name__}")


def estimate_tokens(prompt: str, response_text: str) -> tuple[int, int]:
    """Estimate token counts from text when usage data is unavailable.

    Uses the rough heuristic of ~4 characters per token (common for English text
    with typical LLM tokenizers).

    Returns:
        Tuple of (estimated_prompt_tokens, estimated_completion_tokens).
    """
    chars_per_token = 4
    prompt_tokens = max(1, len(prompt) // chars_per_token)
    completion_tokens = max(1, len(response_text) // chars_per_token)
    return prompt_tokens, completion_tokens


class ReviewService:
    """Generates AI-powered qualitative reviews using OpenAI-compatible API."""

    def __init__(
        self,
        timeout_seconds: int | None = None,
        prompt_loader: PromptTemplateLoader | None = None,
        scoring_settings: ScoringSettings | None = None,
        wm_client: WMClient | None = None,
    ) -> None:
        """Initialize the review service.

        Args:
            timeout_seconds: Maximum time to wait for each WM call. Defaults to
                ``settings.review.timeout_seconds`` (``REVIEW_TIMEOUT_SECONDS``).
                This used to be a hardcoded 180 while the ``WMClient`` it wraps
                is configured for 600s, so the client's own timeout was dead
                code and no environment variable could change the cap -- the
                ``REVIEW_`` knob existed but nothing read it.
            prompt_loader: Optional Jinja2 template loader for prompts.
            scoring_settings: Optional scoring weight configuration.
            wm_client: Optional shared :class:`WMClient`. Constructs one
                via :func:`get_wm_client` when omitted.
        """
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else get_settings().review.timeout_seconds
        )
        self._prompt_loader = prompt_loader
        self._scoring_settings = scoring_settings
        self._wm = wm_client if wm_client is not None else get_wm_client()

    async def generate_all(
        self,
        analysis: AnalysisInput,
        review_types: list[str],
        message: str = "",
        user_context: UserContext | None = None,
        on_progress: Callable[[dict[str, dict]], Awaitable[None]] | None = None,
        llm_config: LLMConfigOverride | None = None,
    ) -> dict[str, dict]:
        """Generate all requested review types.

        Args:
            on_progress: Optional async callback invoked with accumulated results
                after each review type completes (for SSE progress tracking).
            llm_config: Optional per-request LLM configuration override.
        """
        results: dict[str, dict] = {}

        logger.info("Starting review generation: review_types=%s", review_types)
        independent = [rt for rt in review_types if rt not in DEPENDENT_REVIEW_TYPES]
        dependent = [rt for rt in review_types if rt in DEPENDENT_REVIEW_TYPES]

        await self._run_independent_reviews(
            independent, results, analysis, message, user_context, on_progress, llm_config
        )
        await self._run_dependent_reviews(
            dependent, results, analysis, message, user_context, on_progress, llm_config
        )

        # Feature 7: Compute weighted scores for results with categories
        self._apply_computed_scores(results)

        # Aggregate token usage across all review types
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for result in results.values():
            if isinstance(result, dict) and "_token_usage" in result:
                tu = result.pop("_token_usage")
                for k in total_usage:
                    total_usage[k] += tu.get(k, 0)
        if total_usage["total_tokens"] > 0:
            results["_token_usage"] = total_usage

        logger.info(
            "Review generation complete: %d types processed, %d errors",
            len(results),
            sum(1 for r in results.values() if "error" in r),
        )
        return results

    async def _run_independent_reviews(
        self,
        review_types: list[str],
        results: dict[str, dict],
        analysis: AnalysisInput,
        message: str,
        user_context: UserContext | None,
        on_progress: Callable[[dict[str, dict]], Awaitable[None]] | None,
        llm_config: LLMConfigOverride | None = None,
    ) -> None:
        """Run independent reviews in parallel."""
        if not review_types:
            return

        tasks = [
            self._generate_review(
                rt, analysis, message=message, user_context=user_context, llm_config=llm_config
            )
            for rt in review_types
        ]
        task_results = await asyncio.gather(*tasks, return_exceptions=True)
        for rt, result in zip(review_types, task_results, strict=False):
            if isinstance(result, BaseException):
                # Cancellation is NOT a review failure — it must propagate
                # untouched. Absorbing it here wrote a FABRICATED
                # {"error": "CancelledError"} for every outstanding review, so a
                # cancelled run finished "successfully" carrying per-review
                # errors that never happened, instead of unwinding.
                # Mirrors the sibling in analyze/services/review_generator.py.
                if isinstance(result, asyncio.CancelledError):
                    raise result
                # `exc_info=result`: gather(return_exceptions=True) returns the
                # exception as a VALUE, so this is NOT inside an `except` block
                # and a bare `logger.exception()` would be blind. Without it a
                # genuine review failure left no traceback at all.
                logger.error("Review %s failed: %s", rt, result, exc_info=result)
                results[rt] = {"error": str(result)}
            else:
                results[rt] = result
            if on_progress:
                try:
                    await on_progress(results)
                except (OSError, ValueError, RuntimeError, TimeoutError) as cb_exc:
                    logger.warning("Progress callback failed: %s", cb_exc)

    async def _run_dependent_reviews(
        self,
        review_types: list[str],
        results: dict[str, dict],
        analysis: AnalysisInput,
        message: str,
        user_context: UserContext | None,
        on_progress: Callable[[dict[str, dict]], Awaitable[None]] | None,
        llm_config: LLMConfigOverride | None = None,
    ) -> None:
        """Run dependent reviews sequentially with retry."""
        for rt in review_types:
            prior_reviews = {k: v for k, v in results.items() if "error" not in v}
            try:
                result = await self._generate_review(
                    rt,
                    analysis,
                    prior_reviews=prior_reviews,
                    message=message,
                    user_context=user_context,
                    llm_config=llm_config,
                )
                results[rt] = result
            except (OSError, ValueError, RuntimeError, TimeoutError, httpx.HTTPError) as exc:
                logger.warning("First attempt for %s failed, retrying: %s", rt, exc)
                try:
                    result = await self._retry_feature_suggestions(
                        analysis,
                        prior_reviews,
                        message,
                        llm_config=llm_config,
                    )
                    results[rt] = result
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    TimeoutError,
                    httpx.HTTPError,
                ) as retry_exc:
                    logger.error("Retry for %s also failed: %s", rt, retry_exc)
                    results[rt] = {"error": str(retry_exc)}
            if on_progress:
                try:
                    await on_progress(results)
                except (OSError, ValueError, RuntimeError, TimeoutError) as cb_exc:
                    logger.warning("Progress callback failed: %s", cb_exc)

    async def _generate_review(
        self,
        review_type: str,
        analysis: AnalysisInput,
        prior_reviews: dict[str, dict] | None = None,
        message: str = "",
        user_context: UserContext | None = None,
        llm_config: LLMConfigOverride | None = None,
    ) -> dict:
        """Generate a single review type using the Anthropic API.

        Raises exceptions on failure (does NOT return error dicts).
        """
        prompt = self._build_prompt(review_type, analysis, prior_reviews, message, user_context)
        worker_name = _worker_for_review(review_type)
        model_override = model_override_from(llm_config)

        tracer = _get_tracer()
        start_time = time.monotonic()
        logger.info("WM call started: review_type=%s worker=%s", review_type, worker_name)
        try:
            span_ctx = (
                tracer.start_as_current_span(
                    f"llm.generate.{review_type}",
                    attributes={"review_type": review_type},
                )
                if tracer
                else _NullContext()
            )
            with span_ctx:
                response = await asyncio.wait_for(
                    self._wm.message(
                        worker_name=worker_name,
                        message=prompt,
                        system_prompt=_REVIEW_SYSTEM_PROMPT,
                        model_override=model_override,
                    ),
                    timeout=self.timeout_seconds,
                )
            duration = time.monotonic() - start_time
            logger.info(
                "WM call completed: review_type=%s, duration=%.2fs",
                review_type,
                duration,
            )

            review_llm_latency_seconds.labels(review_type=review_type).observe(duration)
            raw = _coerce_response(response.output)
            usage = response.usage_metrics or {}
            if usage:
                review_llm_tokens_total.labels(review_type=review_type, direction="input").inc(
                    int(usage.get("prompt_tokens") or 0)
                )
                review_llm_tokens_total.labels(review_type=review_type, direction="output").inc(
                    int(usage.get("completion_tokens") or 0)
                )
            else:
                logger.warning("Missing usage data for review_type=%s", review_type)
                estimated_input, estimated_output = estimate_tokens(prompt, raw)
                review_llm_tokens_total.labels(review_type=review_type, direction="input").inc(
                    estimated_input
                )
                review_llm_tokens_total.labels(review_type=review_type, direction="output").inc(
                    estimated_output
                )

            if not raw:
                raise ValueError("Empty response from WM")

            parse_ctx = (
                tracer.start_as_current_span(
                    f"parse_and_validate.{review_type}",
                    attributes={"review_type": review_type},
                )
                if tracer
                else _NullContext()
            )
            with parse_ctx:
                parsed = self._parse_and_validate(review_type, raw)
                if usage:
                    parsed["_token_usage"] = {
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "total_tokens": int(
                            usage.get(
                                "total_tokens",
                                (usage.get("prompt_tokens") or 0)
                                + (usage.get("completion_tokens") or 0),
                            )
                        ),
                    }
                return parsed
        except TimeoutError as exc:
            review_errors_total.labels(review_type=review_type, error_type="timeout").inc()
            logger.error(
                "WM call timed out: review_type=%s, timeout=%ds",
                review_type,
                self.timeout_seconds,
            )
            raise TimeoutError(
                f"WM call for {review_type} timed out after {self.timeout_seconds}s"
            ) from exc
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            review_errors_total.labels(review_type=review_type, error_type=type(exc).__name__).inc()
            logger.error("Review generation failed for %s: %s", review_type, exc)
            raise

    async def _retry_feature_suggestions(
        self,
        analysis: AnalysisInput,
        prior_reviews: dict[str, dict] | None,
        message: str,
        llm_config: LLMConfigOverride | None = None,
    ) -> dict:
        """Retry feature_suggestions with a simplified prompt emphasizing JSON output."""
        logger.warning("Retrying feature_suggestions with simplified prompt")
        context = prior_reviews if prior_reviews else self._analysis_to_review_context(analysis)

        site = analysis.site

        # Build prior findings summary for template
        prior_findings = ""
        if context:
            sections = []
            for name, data in context.items():
                parts = []
                if data.get("overall_score") is not None:
                    parts.append(f"Score: {data['overall_score']}")
                if "strengths" in data:
                    parts.append(f"Strengths: {'; '.join(str(s) for s in data['strengths'][:3])}")
                sections.append(f"{name}: " + ", ".join(parts))
            prior_findings = "\n".join(sections)

        # Use template if available, otherwise build inline
        if self._prompt_loader and hasattr(self._prompt_loader, "has_template"):
            if self._prompt_loader.has_template("feature_suggestions_retry"):
                prompt = self._prompt_loader.render(
                    "feature_suggestions_retry",
                    {
                        "site_description": site.description or "A website",
                        "target_audience": ", ".join(site.target_audience)
                        if site.target_audience
                        else "General",
                        "prior_findings": prior_findings,
                        "focus": message,
                    },
                )
            else:
                prompt = self._build_retry_prompt_inline(site, prior_findings, message)
        else:
            prompt = self._build_retry_prompt_inline(site, prior_findings, message)

        model_override = model_override_from(llm_config)

        response = await asyncio.wait_for(
            self._wm.message(
                worker_name=_worker_for_review("feature_suggestions"),
                message=prompt,
                system_prompt=_REVIEW_SYSTEM_PROMPT,
                model_override=model_override,
            ),
            timeout=self.timeout_seconds,
        )
        raw = _coerce_response(response.output)
        if not raw:
            raise ValueError("Empty response from WM")
        return self._parse_and_validate("feature_suggestions", raw)

    @staticmethod
    def _build_retry_prompt_inline(
        site: SiteAnalysisInput, prior_findings: str, message: str
    ) -> str:
        """Build inline retry prompt when template is not available."""
        prompt = f"""You are a product strategist. Generate 8-15 feature improvement proposals for this website.

CRITICAL: Base proposals ONLY on the data provided below. Do NOT fabricate technology stack details, vulnerabilities, or server configuration findings.

Site: {site.description or "A website"}
Audience: {", ".join(site.target_audience) if site.target_audience else "General"}

IMPORTANT: Your response MUST be ONLY a valid JSON object with this exact structure:
{{
  "total_proposals": <number>,
  "proposals": [
    {{
      "id": 1,
      "title": "...",
      "category": "quick_win|strategic_improvement|long_term_investment|nice_to_have",
      "impact": "high|medium|low",
      "complexity": "high|medium|low",
      "description": "...",
      "rationale": "..."
    }}
  ]
}}

No markdown fencing, no explanatory text. Only the JSON object."""

        if prior_findings:
            prompt += "\n\nPrior findings:\n" + prior_findings

        if message:
            prompt += f"\n\nFocus: {message}"

        return prompt

    def _analysis_to_review_context(self, analysis: AnalysisInput) -> dict[str, dict]:
        """Build minimal review context from analysis data when all prior reviews failed."""
        context: dict[str, dict] = {}
        if analysis.pages:
            page_count = len(analysis.pages)
            has_forms = any(p.has_forms for p in analysis.pages)
            has_media = any(p.has_media for p in analysis.pages)
            context["analysis_summary"] = {
                "overall_score": 5,
                "strengths": [
                    f"Site has {page_count} pages analyzed",
                    f"{'Includes forms' if has_forms else 'No forms detected'}",
                    f"{'Includes media content' if has_media else 'No media detected'}",
                ],
            }
        return context

    @staticmethod
    def _format_page_list(analysis: AnalysisInput) -> str:
        return "\n".join(
            f"- **{p.title or p.url}** ({p.page_type}) -- "
            f"{p.word_count} words, {len(p.interactive_elements)} interactive elements, "
            f"{len(p.content_sections)} content sections, {len(p.headings)} headings, "
            f"{'has forms' if p.has_forms else 'no forms'}, "
            f"{'has media' if p.has_media else 'no media'}"
            for p in analysis.pages[:30]
        )

    @staticmethod
    def _format_page_content(analysis: AnalysisInput) -> str:
        excerpts: list[str] = []
        for p in analysis.pages[:10]:
            if p.text:
                excerpts.append(f"### {p.title or p.url}\n{p.text[:2000]}")
            elif p.content_sections:
                section_text = "\n".join(
                    f"[{s.get('heading', '')}] {s.get('text_preview', '')}"
                    for s in p.content_sections
                    if s.get("text_preview")
                )
                if section_text:
                    excerpts.append(f"### {p.title or p.url}\n{section_text[:2000]}")
        return "\n\n".join(excerpts)

    @staticmethod
    def _format_narrative_context(analysis: AnalysisInput) -> str:
        if not analysis.narrative:
            return ""
        n = analysis.narrative
        hooks = "\n".join(f"- {h}" for h in n.hooks[:10])
        quotes = "\n".join(f"- {q}" for q in n.key_quotes[:5])
        features = "\n".join(f"- {f}" for f in n.feature_highlights[:10])
        return (
            f"#### Narrative Hooks\n{hooks}\n\n"
            f"#### Key Quotes\n{quotes}\n\n"
            f"#### Feature Highlights\n{features}"
        )

    @staticmethod
    def _format_page_visual(p: Any) -> list[str]:
        page_visual: list[str] = []
        if p.css_colors:
            page_visual.append(f"Colors found: {', '.join(p.css_colors[:20])}")
        if p.font_families:
            page_visual.append(f"Fonts: {', '.join(p.font_families[:10])}")
        if p.media_queries:
            page_visual.append(f"Responsive breakpoints: {', '.join(p.media_queries[:10])}")
        if p.heading_structure:
            levels = [h["level"] for h in p.heading_structure]
            page_visual.append(f"Heading hierarchy: {' → '.join(levels)}")
        elif p.headings:
            page_visual.append(f"Headings: {'; '.join(p.headings[:15])}")
        if p.interactive_elements:
            buttons = [
                e.get("text", "")
                for e in p.interactive_elements
                if e.get("tag") == "button" and e.get("text")
            ]
            links = [
                e.get("text", "")
                for e in p.interactive_elements
                if e.get("tag") == "a" and e.get("text")
            ]
            if buttons:
                page_visual.append(f"Buttons/CTAs: {', '.join(buttons[:10])}")
            if links:
                page_visual.append(f"Navigation links: {', '.join(links[:10])}")
        return page_visual

    @classmethod
    def _format_visual_design(cls, analysis: AnalysisInput) -> str:
        visual_data: list[str] = []
        for p in analysis.pages[:10]:
            page_visual = cls._format_page_visual(p)
            if page_visual:
                visual_data.append(f"### {p.title or p.url}\n" + "\n".join(page_visual))
        return "\n".join(visual_data) if visual_data else "No visual data extracted."

    @staticmethod
    def _format_prior_reviews(prior_reviews: dict[str, dict]) -> str:
        sections: list[str] = []
        for name, data in prior_reviews.items():
            parts: list[str] = []
            if data.get("overall_score") is not None:
                parts.append(f"Overall score: {data['overall_score']}")
            if "strengths" in data:
                parts.append(f"Strengths: {'; '.join(str(s) for s in data['strengths'][:3])}")
            if "weaknesses" in data:
                parts.append(f"Weaknesses: {'; '.join(str(w) for w in data['weaknesses'][:3])}")
            sections.append(
                f"### {name.replace('_', ' ').title()}\n" + "\n".join(f"- {p}" for p in parts)
            )
        return "\n\n".join(sections)

    @staticmethod
    def _apply_user_context(context: dict, user_context: UserContext) -> None:
        if user_context.focus_areas:
            context["focus_areas"] = "\n".join(f"- {area}" for area in user_context.focus_areas)
        if user_context.goals:
            context["business_goals"] = "\n".join(f"- {goal}" for goal in user_context.goals)
        if user_context.tone:
            context["review_tone"] = user_context.tone
        if user_context.target_audience:
            context["user_target_audience"] = user_context.target_audience

    def _build_template_context(
        self,
        review_type: str,
        analysis: AnalysisInput,
        prior_reviews: dict[str, dict] | None = None,
        message: str = "",
        user_context: UserContext | None = None,
    ) -> dict:
        """Extract data into a template context dict."""
        site = analysis.site

        context = {
            "site_description": site.description or "N/A",
            "target_audience": ", ".join(site.target_audience)
            if site.target_audience
            else "Unknown",
            "selling_points": "\n".join(f"- {sp}" for sp in site.selling_points)
            if site.selling_points
            else "N/A",
            "use_cases": "\n".join(f"- {uc}" for uc in site.use_cases) if site.use_cases else "N/A",
            "tone": site.tone or "N/A",
            "visual_identity": json.dumps(site.visual_identity) if site.visual_identity else "N/A",
            "pages": self._format_page_list(analysis) or "No pages analyzed.",
            "page_content": self._format_page_content(analysis),
            "narrative_hooks": self._format_narrative_context(analysis)
            or "No narrative data available.",
            "visual_design_data": self._format_visual_design(analysis),
        }

        if prior_reviews:
            context["prior_reviews"] = self._format_prior_reviews(prior_reviews)
        if message:
            context["user_direction"] = message
        if user_context:
            self._apply_user_context(context, user_context)
        return context

    def _build_prompt(
        self,
        review_type: str,
        analysis: AnalysisInput,
        prior_reviews: dict[str, dict] | None = None,
        message: str = "",
        user_context: UserContext | None = None,
    ) -> str:
        """Build prompt using Jinja2 template if available, otherwise use inline prompt."""
        context = self._build_template_context(
            review_type, analysis, prior_reviews, message, user_context
        )

        # Feature 6: Use template if available
        if (
            self._prompt_loader
            and hasattr(self._prompt_loader, "has_template")
            and self._prompt_loader.has_template(review_type)
        ):
            return self._prompt_loader.render(review_type, context)

        # Fallback to inline prompt
        site = analysis.site
        model_cls = LLM_OUTPUT_MODELS.get(review_type)
        if model_cls is not None:
            schema_hint = json.dumps(
                {
                    k: v.get("type", "any")
                    for k, v in model_cls.model_json_schema().get("properties", {}).items()
                },
                indent=2,
            )
        else:
            schema_hint = "{}"

        prompt = f"""You are a professional website reviewer. Generate a {review_type.replace("_", " ")} for this website.

CRITICAL: Base ALL observations EXCLUSIVELY on the data provided below. Do NOT infer or fabricate details about the site's technology stack, server configuration, or backend implementation. Do NOT claim the site uses WordPress, React, or any specific framework unless explicitly stated. Do NOT fabricate findings about exposed files or security vulnerabilities.

## Site Description
{site.description or "N/A"}

## Target Audience
{", ".join(site.target_audience) if site.target_audience else "Unknown"}

## Selling Points
{chr(10).join(f"- {sp}" for sp in site.selling_points) if site.selling_points else "N/A"}

## Pages
{context["pages"]}

## Narrative Context
{context["narrative_hooks"]}

## Required JSON Structure
Your response MUST be a valid JSON object with these keys:
{schema_hint}

Respond with ONLY the JSON object, no markdown fencing or extra text."""

        if prior_reviews:
            prompt += "\n\n## Prior Review Findings\n\n" + context.get("prior_reviews", "")

        if message:
            prompt += f"\n\n## User Direction\n{message}\n"

        return prompt

    @staticmethod
    def _empty_response_fallback(review_type: str) -> dict:
        logger.warning(
            "LLM returned empty JSON object for review_type=%s; returning insufficient-data fallback",
            review_type,
        )
        # NOT ``model_cls().model_dump()``: every model's score field used to
        # default to 0.0, so a review that never happened was handed downstream
        # as a real measurement and printed to the customer as "0.0/10 (F)".
        return insufficient_data_result(review_type, "LLM returned no usable content")

    @staticmethod
    def _normalize_review_data(review_type: str, data: dict) -> None:
        """Apply key alias normalization + per-review-type fixups in place."""
        for alias, canonical in KEY_ALIASES.items():
            if alias in data and canonical not in data:
                data[canonical] = data.pop(alias)
        if review_type == "feature_suggestions":
            if "proposals" in data and "total_proposals" not in data:
                data["total_proposals"] = len(data["proposals"])
            if "proposals" in data and len(data["proposals"]) == 0:
                raise ValueError("feature_suggestions returned empty proposals list")

    def _parse_and_validate(self, review_type: str, raw: str) -> dict:
        """Parse LLM response and validate/coerce via Pydantic models."""
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start < 0 or end <= start:
            raise ValueError("No JSON object found in response")

        data = json.loads(raw[start:end])
        if not isinstance(data, dict):
            raise ValueError("Response is not a JSON object")

        if not data:
            return self._empty_response_fallback(review_type)

        self._normalize_review_data(review_type, data)

        if _is_all_na(data):
            raise ValueError(f"LLM returned all N/A or empty values for {review_type}")
        return validate_llm_output(review_type, data)

    def _apply_computed_scores(self, results: dict[str, dict]) -> None:
        """Feature 7: Apply computed weighted scores to results that have categories."""
        if not self._scoring_settings:
            return

        from src.domains.review.services.scoring import compute_weighted_score

        for review_type, result in results.items():
            if "error" in result or "categories" not in result:
                continue

            weights = self._scoring_settings.get_category_weights(review_type)
            if not weights:
                continue

            computed = compute_weighted_score(result["categories"], weights)
            if computed is not None:
                result["computed_overall_score"] = computed
