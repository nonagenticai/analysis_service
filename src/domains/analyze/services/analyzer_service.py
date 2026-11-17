"""Main analyzer service orchestrating page analysis, classification, and narrative extraction."""

from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from src.config import get_settings
from src.domains.analyze.services.language_dedup import detect_language_variants
from src.domains.analyze.services.llm_audit import LLMAuditLogger
from src.domains.analyze.services.narrative_extractor import NarrativeExtractor
from src.domains.analyze.services.page_analyzer import PageAnalyzer
from src.domains.analyze.services.prompt_builder import PromptBuilder
from src.domains.analyze.services.spa_detector import detect_spa_pages
from src.llm_client import get_wm_client
from src.llm_credentials import model_override_from
from src.metrics import analyzer_llm_calls_total, analyzer_llm_latency_seconds

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.analyze.schemas import LLMConfigOverride

logger = logging.getLogger(__name__)

_SITE_ANALYSIS_WORKER = "analysis-site-summary"

_SITE_ANALYSIS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "target_audience": {"type": "array", "items": {"type": "string"}},
        "use_cases": {"type": "array", "items": {"type": "string"}},
        "selling_points": {"type": "array", "items": {"type": "string"}},
        "narrative_hooks": {"type": "array", "items": {"type": "string"}},
        "tone": {"type": "string"},
        "visual_identity": {"type": "object"},
    },
    "required": ["description"],
}


def _normalize_wm_output(output: Any) -> tuple[dict[str, Any], str]:
    """Coerce a WM ``output`` payload into ``(parsed_dict, raw_preview_str)``.

    WM returns a dict for structured-output mode (with ``output_schema``)
    and a string for free-text mode. The site-analysis flow asks for a
    schema so we expect dicts; tolerate strings as a fallback for
    providers that ignored the schema.
    """
    if isinstance(output, dict):
        import json

        return output, json.dumps(output)
    if isinstance(output, str):
        from src.domains.analyze.services.response_parser import parse_json_from_response

        # required=True: an unparseable free-text response must raise, not
        # silently coerce to {}. `{}` is a dict, so a silent-empty result
        # would sail through the `isinstance(parsed, dict)` check below and
        # be treated as a successful-but-empty analysis — bypassing the
        # retry loop and `_basic_site_analysis` heuristic fallback in
        # `analyze_site`, which exist exactly to handle this case.
        parsed = parse_json_from_response(output, required=True)
        if isinstance(parsed, dict):
            return parsed, output
        raise ValueError("Site-analysis response is not a JSON object")
    raise ValueError(f"Unexpected WM output type: {type(output).__name__}")


@dataclass
class SiteAnalysisResult:
    """Enriched result from site analysis including metrics."""

    analysis: dict = field(default_factory=dict)
    llm_calls: int = 0
    artifacts: list[dict] = field(default_factory=list)


class AnalyzerService:
    """Orchestrates page analysis, site-level analysis, and narrative extraction.

    Attributes:
        page_analyzer: HTML page structure extractor.
        narrative_extractor: Narrative material extractor.
        prompt_builder: Jinja2-based LLM prompt renderer.
        audit: LLM call audit logger.
    """

    def __init__(self, wm_client: WMClient | None = None) -> None:
        self.page_analyzer = PageAnalyzer()
        self.narrative_extractor = NarrativeExtractor()
        self.prompt_builder = PromptBuilder()
        self.audit = LLMAuditLogger()
        # Default to a freshly-built WMClient when none is injected (e.g.
        # during unit tests that patch the factory). Production paths
        # construct a single shared WMClient in the FastAPI lifespan and
        # pass it in via the asgi initializer.
        self._wm = wm_client if wm_client is not None else get_wm_client()

    @staticmethod
    def _dedupe_pages(pages: list[dict]) -> tuple[list[dict], list[int]]:
        """Deduplicate pages by page_id (or url fallback)."""
        seen: dict[str, int] = {}
        unique_pages: list[dict] = []
        index_map: list[int] = []
        for page in pages:
            key = page.get("page_id") or page.get("url") or ""
            if key and key in seen:
                index_map.append(seen[key])
            else:
                idx = len(unique_pages)
                if key:
                    seen[key] = idx
                unique_pages.append(page)
                index_map.append(idx)
        if len(unique_pages) < len(pages):
            logger.info("Deduplicated %d → %d unique pages", len(pages), len(unique_pages))
        return unique_pages, index_map

    @staticmethod
    def _timeout_entry(page: dict, error: str) -> dict:
        """Build a placeholder result for a page whose analysis didn't complete."""
        return {
            "page_id": page.get("page_id", ""),
            "url": page.get("url", ""),
            "title": page.get("title", ""),
            "page_type": "unknown",
            "error": error,
        }

    async def _analyze_unique_pages(
        self, unique_pages: list[dict], workers: int, page_timeout: float
    ) -> list[dict | None]:
        """Run page analysis with semaphore over unique pages, with timeouts."""
        semaphore = asyncio.Semaphore(workers)

        async def _analyze_one(page: dict) -> dict:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(
                            self.page_analyzer.analyze,
                            page_id=page.get("page_id", ""),
                            url=page.get("url", ""),
                            title=page.get("title", ""),
                            html=page.get("html", ""),
                            text=page.get("text", ""),
                            word_count=page.get("word_count"),
                        ),
                        timeout=page_timeout,
                    )
                except TimeoutError:
                    logger.warning(
                        "Page analysis timed out after %ds: url=%s",
                        page_timeout,
                        page.get("url", ""),
                    )
                    return self._timeout_entry(page, "analysis timed out")
                except Exception as exc:  # noqa: BLE001 — per-page absorb, see comment below
                    # Catch every non-timeout exception type: the documented
                    # contract is "one bad page yields a placeholder entry and
                    # never takes out sibling pages". Pre-fix this caught only
                    # TimeoutError, so any other exception from
                    # page_analyzer.analyze (KeyError on a malformed dict,
                    # AttributeError on missing fields, ParserError on broken
                    # HTML, etc.) escaped, propagated through the bare
                    # asyncio.gather() below, and cancelled every other page's
                    # analysis — one bad page took out the whole batch.
                    logger.warning(
                        "Page analysis failed (%s): url=%s — %s",
                        type(exc).__name__,
                        page.get("url", ""),
                        exc,
                    )
                    return self._timeout_entry(page, f"analysis failed: {type(exc).__name__}")

        settings = get_settings()
        max_aggregate_timeout = settings.analyze.max_aggregate_timeout
        total_timeout = min(len(unique_pages) * page_timeout * 1.5, max_aggregate_timeout)

        logger.info(
            "Analyzing %d pages with %d workers (aggregate timeout=%.0fs)",
            len(unique_pages),
            workers,
            total_timeout,
        )

        unique_results: list[dict | None] = [None] * len(unique_pages)

        async def _analyze_indexed(idx: int, page: dict) -> None:
            unique_results[idx] = await _analyze_one(page)

        tasks = [_analyze_indexed(i, page) for i, page in enumerate(unique_pages)]

        try:
            # return_exceptions=True is belt-and-braces: the inner
            # `except Exception` in `_analyze_one` should always absorb, but a
            # BaseException (e.g. CancelledError from the wait_for timeout, or a
            # KeyboardInterrupt) would otherwise re-cancel siblings here.
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=total_timeout,
            )
        except TimeoutError:
            completed = sum(1 for r in unique_results if r is not None)
            logger.warning(
                "Aggregate page analysis timed out after %.0fs: %d/%d pages completed",
                total_timeout,
                completed,
                len(unique_pages),
            )
            for idx, page in enumerate(unique_pages):
                if unique_results[idx] is None:
                    unique_results[idx] = self._timeout_entry(page, "aggregate analysis timed out")
        return unique_results

    async def analyze_pages(self, pages: list[dict], workers: int = 0) -> list[dict]:
        """Analyze pages in parallel using asyncio.to_thread for CPU-bound work.

        Deduplicates input by page_id (or url fallback) so each unique page is
        analyzed only once.  Results are expanded back to match the original
        input order.

        Args:
            pages: List of page dicts with url, html, text, etc.
            workers: Parallelism limit; defaults to config value.

        Returns:
            List of analyzed page dicts with classification and structure.
        """
        settings = get_settings()
        if workers <= 0:
            workers = settings.analyze.parallel_workers
        page_timeout = settings.analyze.page_analysis_timeout

        unique_pages, index_map = self._dedupe_pages(pages)
        unique_results = await self._analyze_unique_pages(unique_pages, workers, page_timeout)

        # Expand back to original order (all None slots filled above, assert for type checker)
        final_results: list[dict] = []
        for i in range(len(pages)):
            r = unique_results[index_map[i]]
            if r is None:  # pragma: no cover — timeout handler fills all None slots
                raise RuntimeError(
                    f"Page analysis slot {i} unexpectedly None after timeout handling"
                )
            final_results.append(r)
        logger.info("Page analysis completed: %d pages processed", len(final_results))
        return final_results

    def filter_language_duplicates(
        self, pages: list[dict], keep_lang: str = "en"
    ) -> tuple[list[dict], int]:
        """Remove language-duplicate pages, returning filtered list and count removed."""
        duplicate_ids = detect_language_variants(pages, keep_lang)
        if not duplicate_ids:
            return pages, 0
        filtered = [p for p in pages if p.get("page_id") not in duplicate_ids]
        return filtered, len(duplicate_ids)

    def annotate_spa_sections(self, pages: list[dict]) -> list[dict]:
        """Annotate pages that appear to be SPA sections."""
        spa_ids = detect_spa_pages(pages)
        if not spa_ids:
            return pages
        for page in pages:
            if page.get("page_id") in spa_ids:
                page["is_spa_section"] = True
        return pages

    def extract_narrative(self, pages: list[dict]) -> dict:
        """Extract narrative material from analyzed pages."""
        return self.narrative_extractor.extract(pages)

    async def analyze_site(
        self,
        pages: list[dict],
        user_context: dict | None = None,
        job_id: str = "",
        temperature: float | None = None,
        custom_prompts: dict[str, str] | None = None,
        llm_config: LLMConfigOverride | None = None,
    ) -> SiteAnalysisResult:
        """Run site-level analysis using LLM with retry and audit logging.

        Args:
            pages: Analyzed page dicts.
            user_context: Optional user-provided context.
            job_id: Job identifier for audit logging.
            temperature: Optional LLM temperature override.
            custom_prompts: Optional custom prompt overrides.
            llm_config: Optional per-request LLM configuration override.

        Returns:
            SiteAnalysisResult with analysis dict, call count, and artifacts.
        """
        settings = get_settings()
        wm_cfg = settings.wm
        model_override = model_override_from(llm_config)
        # `model` is only used as a label for audit logs / metrics. WM
        # owns provider selection; we pass `model_override` per-call when
        # the caller asked for a specific model.
        model = model_override or "wm-default"
        logger.info(
            "Starting WM site analysis: job_id=%s worker=%s pages=%d",
            job_id,
            _SITE_ANALYSIS_WORKER,
            len(pages),
        )
        max_retries = wm_cfg.max_retries
        retry_delay = wm_cfg.retry_delay

        if custom_prompts and "site_analysis" in custom_prompts:
            prompt = custom_prompts["site_analysis"]
        else:
            prompt = self._build_site_prompt(pages, user_context)
        prompt_tokens = self.audit.estimate_tokens(prompt)

        artifacts: list[dict] = []
        llm_calls = 0
        last_exc: Exception | None = None
        del temperature  # WM owns sampling params per-worker; ignore caller hint.
        for attempt in range(1 + max_retries):
            llm_calls += 1
            start_time = _time.monotonic()
            try:
                start_time = self.audit.log_request(
                    job_id=job_id,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    prompt_preview=prompt[:500],
                )
                start = _time.monotonic()
                response = await self._wm.message(
                    worker_name=_SITE_ANALYSIS_WORKER,
                    message=prompt,
                    output_schema=_SITE_ANALYSIS_OUTPUT_SCHEMA,
                    model_override=model_override,
                )
                duration = _time.monotonic() - start
                analyzer_llm_latency_seconds.observe(duration)
                analyzer_llm_calls_total.labels(status="success").inc()
                parsed_dict, raw = _normalize_wm_output(response.output)
                duration_ms = (_time.monotonic() - start_time) * 1000
                response_tokens = self.audit.estimate_tokens(raw)

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
                    model=model,
                    response_tokens=response_tokens,
                    duration_ms=duration_ms,
                    raw_preview=raw[:500],
                    prompt_tokens_actual=prompt_tokens_actual,
                    completion_tokens_actual=completion_tokens_actual,
                    total_tokens_actual=total_tokens_actual,
                )
                artifacts.append(
                    {
                        "type": "llm_call",
                        "model": model,
                        "prompt_tokens": effective_prompt_tokens,
                        "response_tokens": effective_response_tokens,
                        "prompt_tokens_estimated": prompt_tokens,
                        "response_tokens_estimated": response_tokens,
                        "total_tokens_actual": total_tokens_actual,
                        "duration_ms": round(duration_ms, 1),
                        "status": "success",
                        "prompt_preview": prompt[:200],
                        "response_preview": raw[:200],
                    }
                )
                logger.info(
                    "WM site analysis succeeded: job_id=%s duration=%.1fms tokens=%s",
                    job_id,
                    duration_ms,
                    total_tokens_actual or "estimated",
                )
                return SiteAnalysisResult(
                    analysis=self._coerce_site_result(parsed_dict),
                    llm_calls=llm_calls,
                    artifacts=artifacts,
                )
            except (httpx.HTTPError, TimeoutError, ValueError, RuntimeError) as exc:
                last_exc = exc
                duration_ms = (_time.monotonic() - start_time) * 1000
                self.audit.log_failure(
                    job_id=job_id,
                    model=model,
                    error=str(exc),
                    duration_ms=duration_ms,
                )
                artifacts.append(
                    {
                        "type": "llm_call",
                        "model": model,
                        "prompt_tokens": prompt_tokens,
                        "duration_ms": round(duration_ms, 1),
                        "status": "failure",
                        "error": str(exc),
                    }
                )
                if attempt < max_retries:
                    logger.warning(
                        "WM attempt %d/%d failed, retrying in %.1fs: %s",
                        attempt + 1,
                        1 + max_retries,
                        retry_delay,
                        exc,
                    )
                    await asyncio.sleep(retry_delay)

        analyzer_llm_calls_total.labels(status="failure").inc()
        logger.error("LLM site analysis failed after %d attempts: %s", 1 + max_retries, last_exc)
        logger.warning(
            "Falling back to heuristic site analysis: job_id=%s pages=%d",
            job_id,
            len(pages),
        )
        return SiteAnalysisResult(
            analysis=self._basic_site_analysis(pages),
            llm_calls=llm_calls,
            artifacts=artifacts,
        )

    def _build_site_prompt(self, pages: list[dict], user_context: dict | None = None) -> str:
        return self.prompt_builder.build_site_prompt(pages, user_context)

    def _coerce_site_result(self, parsed: dict[str, Any]) -> dict:
        """Coerce a parsed WM site-analysis dict into the public output shape."""
        data: dict[str, Any] = parsed
        return {
            "description": str(data.get("description", "")),
            "target_audience": [str(x) for x in data.get("target_audience", [])],
            "use_cases": [str(x) for x in data.get("use_cases", [])],
            "selling_points": [str(x) for x in data.get("selling_points", [])],
            "narrative_hooks": [str(x) for x in data.get("narrative_hooks", [])],
            "tone": str(data.get("tone", "")),
            "visual_identity": dict(data.get("visual_identity", {})),
        }

    @staticmethod
    def _basic_description(pages: list[dict], titles: list[str]) -> tuple[str, list[str]]:
        if titles:
            description = f"Website with {len(pages)} pages. Main titles: {', '.join(titles[:3])}"
            use_cases = [f"Explore {t}" for t in titles[:3]]
        else:
            description = f"Website with {len(pages)} pages."
            use_cases = ["Explore Website"]
        return description, use_cases

    @staticmethod
    def _basic_audience(page_types: list[str]) -> list[str]:
        if "pricing" in page_types or "product" in page_types:
            return ["business decision-makers", "potential customers"]
        if "documentation" in page_types:
            return ["developers", "technical users"]
        if "blog_post" in page_types or "blog_index" in page_types:
            return ["industry professionals", "readers"]
        return ["general"]

    @staticmethod
    def _basic_tone(pages: list[dict]) -> str:
        total_words = sum(p.get("word_count", 0) for p in pages)
        avg_words = total_words / max(len(pages), 1)
        if avg_words > 500:
            return "detailed/technical"
        if avg_words < 100:
            return "concise/marketing"
        return "professional"

    @staticmethod
    def _basic_selling_points(pages: list[dict]) -> list[str]:
        selling_points: list[str] = []
        for page in pages:
            if page.get("page_type") in ("homepage", "landing"):
                for heading in page.get("headings", [])[:5]:
                    if len(heading) > 10:
                        selling_points.append(heading)
        return selling_points[:5]

    def _basic_site_analysis(self, pages: list[dict]) -> dict:
        logger.info(
            "LLM degraded: falling back to basic site analysis for %d pages",
            len(pages),
        )
        titles = [p.get("title", "") for p in pages if p.get("title")]
        page_types = [p.get("page_type", "unknown") for p in pages]
        description, use_cases = self._basic_description(pages, titles)
        narrative_hooks = [t for t in titles[:5] if len(t) > 10]

        return {
            "description": description,
            "target_audience": self._basic_audience(page_types),
            "use_cases": use_cases,
            "selling_points": self._basic_selling_points(pages),
            "narrative_hooks": narrative_hooks[:5],
            "tone": self._basic_tone(pages),
        }
