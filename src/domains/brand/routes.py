"""API routes for brand extraction, logo management, and batch operations."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from w2v_common.auth import LICENSE_GATE

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.runner import PipelineRunner, extract_from_analysis
from src.domains.brand.schemas import (
    BatchExtractRequest,
    BatchExtractResponse,
    BatchUpstreamError,
    BrandExtractRequest,
    BrandFromUrlRequest,
    BrandResponse,
    ExtractionReport,
    LogoAssetResponse,
    LogoDownloadRequest,
    StageIssueSchema,
)
from src.shared.database.session import get_db
from src.shared.tenant import require_request_tenant, tenant_uuid
from src.shared.upstream_errors import passthrough_status_error, upstream_http_exception

if TYPE_CHECKING:
    from w2v_common.clients import WMClient

    from src.domains.brand.services.logo_service import LogoService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["brand"], dependencies=list(LICENSE_GATE))


def _get_pipeline_runner(request: Request) -> PipelineRunner:
    """Get PipelineRunner from app state."""
    return request.app.state.pipeline_runner


def _get_tenant_id(request: Request) -> str:
    """Get tenant ID from request state."""
    return str(require_request_tenant(request))


#: The pipeline stage that calls ``worker_management``. Named once because three
#: things key off it: the cacheability predicate, the batch retry that skips it,
#: and the report entry that retry writes.
_LLM_ANALYSIS_STAGE = "llm_analysis"

# Stages whose failure GUTS an extraction rather than costing it one field.
#
# The criterion — the ONLY one, apply it before adding a name here: does a
# failure of this stage remove a whole *class* of the brand's substance, such
# that serving what is left from Redis for the full ``brand_ttl`` (3600s) would
# be worse than re-running the pipeline?
#
# * ``fetch`` — nothing was fetched, so ``PipelineRunner._execute_stages`` breaks
#   out of the loop on the spot and returns a brand carrying only ``domain``.
#   Caching that turns one Playwright timeout into an hour of "this site has no
#   brand", for every caller, with no error anywhere.
# * ``llm_analysis`` — no tagline, no voice, no industry, no keywords. This is
#   the exact shape that hid ``byo_llm_not_configured`` behind a 200 in prod.
#
# Everything else — ``logo``, ``fonts``, ``colors``, ``social``, ``metadata``,
# ``palette`` — costs one field and IS the ordinary partial extraction this
# pipeline exists to return. Those stay cacheable; a guard that keys off "any
# failed stage" would empty the cache and pay a browser launch per request.
_CACHE_CRITICAL_STAGES: frozenset[str] = frozenset({"fetch", _LLM_ANALYSIS_STAGE})


def _is_cacheable(result: BrandResponse) -> bool:
    """Whether *result* is complete enough to serve for the whole cache TTL.

    The single authority on cacheability — every write path goes through
    :func:`_cache_extraction`, which is the only caller. Keeping the decision in
    one predicate is the point: the first version of this guard lived inline in
    ``extract_from_url`` and the batch path kept its own ``brand_cache.set``, so
    exactly the results the guard rejected were still being cached by the sibling.

    Args:
        result: The brand a pipeline run produced.

    Returns:
        ``False`` when a stage in :data:`_CACHE_CRITICAL_STAGES` failed.
    """
    report = result.extraction_report
    if report is None:
        # No pipeline ran (the ``/brand/extract`` from-analysis path). ``None`` is
        # the absence of evidence, not evidence of degradation.
        return True
    return not _CACHE_CRITICAL_STAGES.intersection(report.stages_failed)


@router.post(
    "/brand/extract",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="extract_brand",
)
async def extract_brand(
    body: BrandExtractRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandResponse:
    """Extract brand from analysis data."""
    start = time.monotonic()
    tenant_id = _get_tenant_id(request)
    logger.info("Brand extraction from analysis requested, tenant=%s", tenant_id)
    result = await extract_from_analysis(body.analysis)

    # Log extraction and upsert brand
    try:
        from src.domains.brand.services.brand_repository import BrandRepository

        await BrandRepository.log_extraction(
            db, tenant_id, None, "analysis", result.model_dump(mode="json")
        )
        if result.domain:
            await BrandRepository.upsert_brand(
                db, tenant_id, result.domain, result.model_dump(mode="json"), source="analysis"
            )
    except (OSError, SQLAlchemyError) as e:
        await db.rollback()
        logger.warning("Failed to persist extraction: %s", e)

    # Cache result
    try:
        brand_cache = getattr(request.app.state, "brand_cache", None)
        if brand_cache and result.domain:
            await brand_cache.set(tenant_id, result.domain, result)
    # ``BrandCacheService`` already swallows its own ``_CACHE_FAILURES``
    # (``RedisError, OSError, ValidationError, TypeError, ValueError``) and
    # reports the failure as a return value, so nothing a Redis outage raises
    # reaches here: this guard is REDUNDANT rather than dangerous, unlike the
    # publish guard below it. Kept as a boundary for a stubbed//future facade
    # that does raise, but the tuple is corrected -- the builtin
    # ``ConnectionError``/``TimeoutError`` it used to name are unrelated to
    # redis-py's same-named classes and could never have fired.
    except (RedisError, OSError) as e:
        logger.debug("Failed to cache result: %s", e)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Brand extraction from analysis completed, domain=%s, duration_ms=%d",
        result.domain,
        elapsed_ms,
    )
    return result


async def _check_url_cache(request: Request, tenant_id: str, url: str) -> BrandResponse | None:
    """Return a cached BrandResponse for ``url`` if present (else ``None``)."""
    try:
        brand_cache = getattr(request.app.state, "brand_cache", None)
        if brand_cache:
            cached = await brand_cache.get_by_url(tenant_id, url)
            if cached:
                logger.info("Cache hit for url=%s, returning cached result", url)
                return cached
    # ``BrandCacheService`` already swallows its own ``_CACHE_FAILURES``
    # (``RedisError, OSError, ValidationError, TypeError, ValueError``) and
    # reports the failure as a return value, so nothing a Redis outage raises
    # reaches here: this guard is REDUNDANT rather than dangerous, unlike the
    # publish guard below it. Kept as a boundary for a stubbed//future facade
    # that does raise, but the tuple is corrected -- the builtin
    # ``ConnectionError``/``TimeoutError`` it used to name are unrelated to
    # redis-py's same-named classes and could never have fired.
    except (RedisError, OSError) as e:
        logger.debug("Cache lookup failed: %s", e)
    return None


def _build_pipeline_context(
    request: Request, body: BrandFromUrlRequest, tenant_id: str
) -> PipelineContext:
    """Construct a ``PipelineContext`` for an extract-from-url request.

    Raises:
        HTTPException: 422 when ``body.config`` does not describe a valid
            ``BrandExtractConfig``. The field is typed as a free-form dict on the
            wire, so the splat below is the first place its contents are checked;
            unguarded, a caller's typo surfaced as an unhandled ``ValidationError``
            and a 500, telling them the server was broken when their request was.
    """
    extract_config = None
    if body.config:
        from src.domains.brand.services.extraction_config import BrandExtractConfig

        try:
            extract_config = BrandExtractConfig(**body.config)
        except (ValidationError, TypeError) as e:
            # TypeError as well as ValidationError: the splat itself raises
            # TypeError before pydantic sees anything if a JSON object arrives
            # with a non-string key, so validation would never get the chance.
            raise HTTPException(status_code=422, detail=f"Invalid config: {e}") from e

    http_client = getattr(request.app.state, "http_client", None)
    logo_service = getattr(request.app.state, "logo_service", None)
    wm_client = getattr(request.app.state, "wm_client", None)
    llm_model_override = body.llm_config.model if body.llm_config else None
    ctx = PipelineContext(
        url=body.url,
        tenant_id=tenant_id,
        http_client=http_client,
        logo_service=logo_service,
        wm_client=wm_client,
        llm_model_override=llm_model_override,
        download_logo=body.download_logo,
        include_fonts=body.include_fonts,
        include_colors=body.include_colors,
        include_metadata=body.include_metadata,
    )
    if extract_config:
        ctx.config = extract_config
    return ctx


async def _persist_extraction(
    db: AsyncSession, tenant_id: str, url: str, result: BrandResponse
) -> None:
    """Persist an extraction record + upsert the brand row."""
    try:
        from src.domains.brand.services.brand_repository import BrandRepository

        await BrandRepository.log_extraction(
            db, tenant_id, url, "url", result.model_dump(mode="json")
        )
        if result.domain:
            await BrandRepository.upsert_brand(
                db, tenant_id, result.domain, result.model_dump(mode="json"), source="url"
            )
    except (OSError, SQLAlchemyError) as e:
        await db.rollback()
        logger.warning("Failed to persist extraction: %s", e)


async def _cache_extraction(request: Request, tenant_id: str, result: BrandResponse) -> None:
    """Best-effort cache write of a *successful* extraction.

    "Successful" used to mean only "the pipeline did not raise", so a run whose
    ``llm_analysis`` stage degraded — no tagline, no voice, no industry — was
    written to Redis under a 3600s TTL (``config/infra.py`` ``brand_ttl``) and
    then served to every subsequent caller for the next hour. One transient WM
    failure became an hour of quietly-wrong brands with no error anywhere.

    Which results are gutted rather than merely partial is :func:`_is_cacheable`
    / :data:`_CACHE_CRITICAL_STAGES`, and **every** cache write in this module
    goes through here — including the batch path, which used to call
    ``brand_cache.set`` inline and so bypassed the guard entirely.

    Write-side guard ONLY, deliberately: a matching read-side "is this cached
    entry degraded" check would turn any transient upstream outage into a full
    Playwright re-fetch on every request for the rest of the TTL — paying a
    browser launch to re-discover the same outage. Not caching the degraded
    result is enough; the next call re-runs the pipeline anyway. There is also a
    manual purge route for the operator who wants the entry gone sooner.
    """
    if not _is_cacheable(result):
        report = result.extraction_report
        logger.info(
            "Skipping cache write for domain=%s: critical stage(s) %s failed, "
            "a gutted brand must not be served for the whole TTL",
            result.domain,
            sorted(_CACHE_CRITICAL_STAGES.intersection(report.stages_failed)) if report else [],
        )
        return
    try:
        brand_cache = getattr(request.app.state, "brand_cache", None)
        if brand_cache and result.domain:
            await brand_cache.set(tenant_id, result.domain, result)
    # ``BrandCacheService`` already swallows its own ``_CACHE_FAILURES``
    # (``RedisError, OSError, ValidationError, TypeError, ValueError``) and
    # reports the failure as a return value, so nothing a Redis outage raises
    # reaches here: this guard is REDUNDANT rather than dangerous, unlike the
    # publish guard below it. Kept as a boundary for a stubbed//future facade
    # that does raise, but the tuple is corrected -- the builtin
    # ``ConnectionError``/``TimeoutError`` it used to name are unrelated to
    # redis-py's same-named classes and could never have fired.
    except (RedisError, OSError) as e:
        logger.debug("Failed to cache result: %s", e)


async def _publish_extraction_event(
    request: Request, tenant_id: str, result: BrandResponse
) -> None:
    """Best-effort publish of a brand-extraction event."""
    try:
        event_publisher = getattr(request.app.state, "event_publisher", None)
        if event_publisher and result.domain:
            quality = result.metadata.confidence if result.metadata else 0.0
            await event_publisher.publish_extraction(
                result.domain, tenant_id, result.model_dump(mode="json"), quality
            )
    # ``RedisClient.publish`` is one of the methods that does NOT swallow
    # ``RedisError`` internally. The old tuple was three builtins -- redis-py's
    # ``ConnectionError``/``TimeoutError`` are ``RedisError`` subclasses that
    # merely share a NAME with the builtins -- so the guard was inert and a
    # Redis outage 500ed a request whose extraction had already succeeded.
    except (RedisError, OSError, TypeError, ValueError) as e:
        logger.debug("Failed to publish event: %s", e)


@router.post(
    "/brand/from-url",
    responses={
        # Passed through from worker_management, not synthesized here: the tenant
        # has no LLM provider configured. The envelope's ``error.code`` carries
        # the upstream's machine-readable code (e.g. ``byo_llm_not_configured``)
        # so the SPA can link straight to Settings instead of showing a 502.
        402: {"description": "Tenant action required upstream (e.g. LLM provider not configured)"},
        422: {"description": "Validation error"},
        502: {"description": "Failed to extract brand from URL"},
    },
    operation_id="extract_brand_from_url",
)
async def extract_from_url(
    body: BrandFromUrlRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandResponse:
    """Extract brand from a URL by fetching and parsing the page."""
    start = time.monotonic()
    runner = _get_pipeline_runner(request)
    tenant_id = _get_tenant_id(request)
    logger.info("Brand extraction from URL requested, url=%s, tenant=%s", body.url, tenant_id)

    cached = await _check_url_cache(request, tenant_id, body.url)
    if cached is not None:
        return cached

    ctx = _build_pipeline_context(request, body, tenant_id)
    try:
        result, _ = await runner.run(ctx)
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "Brand extraction failed for url=%s, duration_ms=%d: %s",
            body.url,
            elapsed_ms,
            e,
        )
        # NOT a blanket 502. worker_management answers 402 with a
        # machine-readable ``byo_llm_not_configured`` when the TENANT has not
        # configured an LLM provider — a condition only the caller can fix. The
        # pipeline re-raises exactly that status out of ``BaseStage.execute``
        # (everything else still degrades one stage), so it arrives here.
        #
        # ``upstream_http_exception`` re-emits ONLY the statuses that still mean
        # the same thing when we say them (today: 402). Everything else — 5xx,
        # timeouts, connect errors, and 401/403/404/429, which are OUR
        # credential/wiring/quota problems — maps to the original 502 with the
        # original detail string, so real outages keep looking like outages.
        raise upstream_http_exception(
            e, upstream="worker_management", fallback_detail=f"Failed to extract brand: {e}"
        ) from e

    await _persist_extraction(db, tenant_id, body.url, result)
    await _cache_extraction(request, tenant_id, result)
    await _publish_extraction_event(request, tenant_id, result)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Brand extraction from URL completed, url=%s, domain=%s, duration_ms=%d",
        body.url,
        result.domain,
        elapsed_ms,
    )
    return result


def _caller_safe_error(exc: BaseException) -> str:
    """Per-URL error text for the batch response, with internals stripped.

    ``str(httpx.HTTPStatusError)`` embeds the full request URL, and for the
    ``llm_analysis`` stage that URL is the INTERNAL worker_management endpoint
    (``http://worker-management…:8887/workers/brand-analysis/message``). The
    batch response crosses a trust boundary to the caller — and, unlike a log
    line, nothing redacts it on the way out — so an upstream HTTP failure is
    reported by status/class only. The detail stays in our logs, where
    ``w2v_common.clients.base`` already redacts credentials before writing it.

    Args:
        exc: The exception a single URL's extraction failed with.

    Returns:
        A string safe to hand to the caller.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else 0
        return f"Upstream request failed with status {status}"
    if isinstance(exc, httpx.HTTPError):
        # Transport errors (``ConnectError``, ``ReadTimeout``, …) carry a
        # ``.request``; their ``str`` is usually bare but is not contractually so.
        return f"Upstream request failed: {type(exc).__name__}"
    return str(exc)


def _mark_llm_stage_failed(result: BrandResponse, message: str) -> None:
    """Record on *result* that ``llm_analysis`` did not produce anything.

    The batch retry deliberately SKIPS the stage rather than re-running it, so
    nothing else would put it in ``stages_failed`` — and both the caller and
    :func:`_is_cacheable` would then read an LLM-less brand as complete and
    cache it for the hour. Marking it here keeps ONE cacheability predicate
    authoritative instead of adding a second "except on the retry path" rule,
    and it is simply true: the analysis is missing.

    Args:
        result: The brand produced by the LLM-less retry.
        message: Caller-safe explanation, recorded as the stage error.
    """
    report = result.extraction_report or ExtractionReport()
    if _LLM_ANALYSIS_STAGE not in report.stages_failed:
        report.stages_failed.append(_LLM_ANALYSIS_STAGE)
    report.errors.append(
        StageIssueSchema(stage=_LLM_ANALYSIS_STAGE, severity="error", message=message)
    )
    result.extraction_report = report


@dataclass
class _BatchRun:
    """Mutable state shared by every URL task in one ``batch_extract`` call.

    This exists because the four helpers below used to be nested ``def``s inside
    :func:`batch_extract`, closing over its locals. They are hoisted so ruff's
    C901 measures each on its own (it charges a nested ``def``'s complexity to
    the enclosing function), and the closure becomes explicit state instead.

    :attr:`llm_disabled` is the load-bearing part: it is flipped by the FIRST
    URL that proves this tenant's LLM cannot be used, and every URL still queued
    behind the browser gate reads the flag when its own task starts, so it skips
    the ``llm_analysis`` stage instead of paying its own doomed WM round-trip.
    One instance is shared by all tasks, so that write is observable exactly as
    the old ``nonlocal`` was. Single-threaded event loop, so no lock.
    """

    request: Request
    runner: PipelineRunner
    tenant_id: str
    http_client: httpx.AsyncClient | None
    logo_service: LogoService | None
    wm_client: WMClient | None
    llm_model_override: str | None
    results: dict[str, BrandResponse] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    #: The one tenant-level condition, reported once (see ``batch_extract``).
    upstream_error: BatchUpstreamError | None = None
    llm_disabled: bool = False

    def build_ctx(self, url: str, *, with_llm: bool) -> PipelineContext:
        """Build the pipeline context for *url*, optionally without the LLM stage."""
        ctx = PipelineContext(
            url=url,
            tenant_id=self.tenant_id,
            http_client=self.http_client,
            logo_service=self.logo_service,
            wm_client=self.wm_client,
            llm_model_override=self.llm_model_override,
        )
        if not with_llm:
            ctx.config = ctx.config.model_copy(
                update={"methods": [m for m in ctx.config.methods if m != _LLM_ANALYSIS_STAGE]}
            )
        return ctx

    async def persist(self, url: str, result: BrandResponse) -> None:
        """Persist to database (each coroutine gets its own session)."""
        try:
            from w2v_common.database.rls import bind_session_tenant

            from src.domains.brand.services.brand_repository import BrandRepository
            from src.shared.database.session import get_session_maker

            session_maker = await get_session_maker()
            async with session_maker() as session:
                await bind_session_tenant(session, tenant_uuid(self.tenant_id))
                await BrandRepository.log_extraction(
                    session, self.tenant_id, url, "batch_url", result.model_dump(mode="json")
                )
                if result.domain:
                    await BrandRepository.upsert_brand(
                        session,
                        self.tenant_id,
                        result.domain,
                        result.model_dump(mode="json"),
                        source="batch_url",
                    )
                await session.commit()
        except (OSError, SQLAlchemyError) as e:
            logger.warning("Failed to persist batch extraction for %s: %s", url, e)

    async def run_and_store(self, url: str, *, llm_unavailable: str | None) -> None:
        """Run the pipeline for *url*, then persist and (maybe) cache the result.

        ``llm_unavailable`` is the caller-safe reason the LLM stage is being
        skipped, or ``None`` to run it. The report is marked BEFORE the cache
        write — that ordering is what lets :func:`_is_cacheable` stay the only
        rule that decides cacheability instead of this path needing its own.
        """
        result, _ = await self.runner.run(self.build_ctx(url, with_llm=llm_unavailable is None))
        if llm_unavailable is not None:
            _mark_llm_stage_failed(result, llm_unavailable)
        self.results[url] = result
        await self.persist(url, result)
        # Routed through the SHARED guard, not ``brand_cache.set``: the inline
        # write this replaced cached exactly the degraded results the from-url
        # path refuses to cache, and ``_check_url_cache`` then served them.
        await _cache_extraction(self.request, self.tenant_id, result)

    async def _retry_without_llm(self, url: str, e: httpx.HTTPStatusError) -> None:
        """Handle a caller-fixable upstream status: record it once, retry LLM-less."""
        mapped = passthrough_status_error(e, upstream="worker_management")
        if mapped is None:
            # Not caller-fixable: OUR credential/wiring, or a real outage.
            self.errors[url] = _caller_safe_error(e)
            return
        logger.warning(
            "Batch URL %s hit a caller-fixable upstream %d (code=%s); "
            "retrying without the LLM stage and keeping the rest",
            url,
            mapped.status_code,
            mapped.upstream_code,
        )
        if self.upstream_error is None:
            self.upstream_error = BatchUpstreamError(
                code=mapped.upstream_code,
                # ``or`` fallback: an upstream that sent a bare 402 with no
                # parseable message must still leave the per-URL stage error
                # and this field saying something.
                message=str(mapped.detail) or f"Upstream returned {mapped.status_code}",
                status=mapped.status_code,
                upstream=mapped.upstream,
            )
        self.llm_disabled = True
        try:
            await self.run_and_store(url, llm_unavailable=self.upstream_error.message)
        except Exception as retry_exc:  # noqa: BLE001 — same per-URL contract
            self.errors[url] = _caller_safe_error(retry_exc)

    async def extract_one(self, url: str) -> None:
        """Extract one URL, absorbing every failure into ``errors`` (per-URL contract)."""
        try:
            await self.run_and_store(
                url,
                llm_unavailable=(
                    self.upstream_error.message
                    if self.llm_disabled and self.upstream_error
                    else None
                ),
            )
        except httpx.HTTPStatusError as e:
            await self._retry_without_llm(url, e)
        except Exception as e:  # noqa: BLE001 — per-URL absorb is the documented contract
            # The handler returns a per-URL dict of results/errors and the
            # contract is "partial success is fine". A narrow tuple here was
            # letting RuntimeError/KeyError/etc. from runner.run() escape the
            # task and cancel every sibling in the gather below — one bad URL
            # took out the other 49 in a 50-URL batch.
            self.errors[url] = _caller_safe_error(e)


@router.post(
    "/brand/batch-extract",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="batch_extract_brand",
)
async def batch_extract(
    body: BatchExtractRequest,
    request: Request,
) -> BatchExtractResponse:
    """Batch extract brands from multiple URLs concurrently.

    Concurrency is bounded by the process-wide browser gate in
    ``src/shared/security/browser_gate.py``, which every pipeline run passes
    through at the fetch stage -- so the 50-URL ceiling on the request body is
    not itself a ceiling on resident browsers, and does not need to be.

    Upstream 402 — why this endpoint does NOT re-emit it, unlike its siblings
    ---------------------------------------------------------------------------
    ``/brand/from-url`` and ``/brands/compare`` answer the caller with the
    upstream 402, because a single-result endpoint has nothing to return without
    it. This one does: ``byo_llm_not_configured`` costs a brand its tagline,
    voice and industry, and NOTHING else — colors, fonts, logo and palette come
    from the page, not from the LLM. Failing the whole request would throw away
    50 completed Playwright fetches over a condition that removes a handful of
    fields, and the endpoint's documented contract is already per-URL partial
    success.

    So the LLM-less work is kept: the first URL to hit the 402 retries itself
    with the ``llm_analysis`` stage disabled, and every URL that has not yet
    built its context skips the stage from the start rather than paying another
    doomed WM call. Each such result carries ``llm_analysis`` in its
    ``extraction_report.stages_failed`` — honest per-URL, and enough for
    :func:`_is_cacheable` to keep it out of Redis without a second rule.

    The tenant-level fact is reported ONCE, structured, in
    ``upstream_error``: 50 copies of ``str(exc)`` in ``errors`` told the SPA
    nothing it could branch on and leaked the internal WM URL while doing it.
    """
    start = time.monotonic()
    tenant_id = _get_tenant_id(request)
    # No ``brand_cache`` local any more: the cache is reached only through
    # ``_cache_extraction``, which reads it off ``app.state`` itself.
    run = _BatchRun(
        request=request,
        runner=_get_pipeline_runner(request),
        tenant_id=tenant_id,
        http_client=getattr(request.app.state, "http_client", None),
        logo_service=getattr(request.app.state, "logo_service", None),
        wm_client=getattr(request.app.state, "wm_client", None),
        llm_model_override=body.llm_config.model if body.llm_config else None,
    )
    logger.info(
        "Batch brand extraction requested, url_count=%d, tenant=%s",
        len(body.urls),
        tenant_id,
    )

    tasks = [run.extract_one(url) for url in body.urls]
    # return_exceptions=True is belt-and-braces: extract_one is supposed to
    # absorb everything, but if it ever does escape (e.g. CancelledError on
    # shutdown) we still don't want one bad task to take down the batch.
    gather_results = await asyncio.gather(*tasks, return_exceptions=True)
    for url, gres in zip(body.urls, gather_results, strict=True):
        if isinstance(gres, BaseException) and url not in run.errors and url not in run.results:
            # ``repr`` before: it renders an ``HTTPStatusError``'s message, i.e.
            # the internal WM URL, straight into the caller's response body.
            run.errors[url] = _caller_safe_error(gres)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Batch extraction completed, total=%d, succeeded=%d, failed=%d, "
        "upstream_error=%s, duration_ms=%d",
        len(body.urls),
        len(run.results),
        len(run.errors),
        run.upstream_error.code if run.upstream_error else None,
        elapsed_ms,
    )
    return BatchExtractResponse(
        results=run.results,
        errors=run.errors,
        total=len(body.urls),
        succeeded=len(run.results),
        failed=len(run.errors),
        upstream_error=run.upstream_error,
    )


@router.post(
    "/brand/logo/download",
    responses={
        400: {"description": "Logo validation failed"},
        422: {"description": "Validation error"},
        502: {"description": "Failed to download logo"},
        503: {"description": "Logo service not available"},
    },
    operation_id="download_brand_logo",
)
async def download_logo(
    body: LogoDownloadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> LogoAssetResponse:
    """Download and store a logo from a URL.

    The tenant is taken from the authenticated identity on the request and is
    never client-supplied. The blob is written to storage under that tenant's
    prefix before the asset row is inserted, so a body-settable tenant would let
    a caller plant bytes in another tenant's prefix even though RLS then
    refused the row — the DB check is not the only thing guarding this path.
    """
    start = time.monotonic()
    logo_service = getattr(request.app.state, "logo_service", None)
    if not logo_service:
        raise HTTPException(status_code=503, detail="Logo service not available")

    tenant_id = _get_tenant_id(request)
    logger.info("Logo download requested, url=%s, tenant=%s", body.url, tenant_id)

    try:
        asset = await logo_service.download_and_store(body.url, tenant_id)
    except ValueError as e:
        logger.warning("Logo validation failed for url=%s: %s", body.url, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("Logo download failed, url=%s, duration_ms=%d: %s", body.url, elapsed_ms, e)
        raise HTTPException(status_code=502, detail=f"Failed to download logo: {e}") from e

    # Save logo asset record to DB
    try:
        from src.domains.brand.services.brand_repository import BrandRepository

        domain = urlparse(body.url).netloc.removeprefix("www.")
        await BrandRepository.save_logo_asset(
            db,
            asset_id=asset.asset_id,
            tenant_id=tenant_id,
            domain=domain,
            original_url=body.url,
            storage_path=asset.stored_path,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
        )
    except (OSError, SQLAlchemyError) as e:
        logger.warning("Failed to save logo asset record: %s", e)

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Logo downloaded and stored, asset_id=%s, size=%d, duration_ms=%d",
        asset.asset_id,
        asset.size_bytes,
        elapsed_ms,
    )
    return LogoAssetResponse(
        asset_id=asset.asset_id,
        stored_path=asset.stored_path,
        content_type=asset.content_type,
        size_bytes=asset.size_bytes,
        serve_url=f"/api/brand/logo/{asset.asset_id}",
    )


@router.get(
    "/brand/logo/{asset_id}",
    responses={
        404: {"description": "Logo asset not found"},
        503: {"description": "Logo service not available"},
    },
    operation_id="get_brand_logo",
)
async def serve_logo(
    asset_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve a stored logo by asset ID."""
    logger.debug("Logo serve requested, asset_id=%s", asset_id)
    tenant_id = _get_tenant_id(request)
    logo_service = getattr(request.app.state, "logo_service", None)
    if not logo_service:
        raise HTTPException(status_code=503, detail="Logo service not available")

    from src.domains.brand.services.brand_repository import BrandRepository

    record = await BrandRepository.get_logo_asset(db, asset_id, tenant_id=tenant_id)
    if not record:
        raise HTTPException(status_code=404, detail="Logo asset not found")

    data = await logo_service.retrieve(record.storage_path)
    if data is None:
        raise HTTPException(status_code=404, detail="Logo file not found")

    return Response(content=data, media_type=str(record.content_type))


@router.delete(
    "/brand/logo/{asset_id}",
    responses={
        404: {"description": "Logo asset not found"},
        503: {"description": "Logo service not available"},
    },
    operation_id="delete_brand_logo",
)
async def delete_logo(
    asset_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a stored logo by asset ID."""
    logger.info("Logo delete requested, asset_id=%s", asset_id)
    tenant_id = _get_tenant_id(request)
    logo_service = getattr(request.app.state, "logo_service", None)
    if not logo_service:
        raise HTTPException(status_code=503, detail="Logo service not available")

    from src.domains.brand.services.brand_repository import BrandRepository

    record = await BrandRepository.get_logo_asset(db, asset_id, tenant_id=tenant_id)
    if not record:
        raise HTTPException(status_code=404, detail="Logo asset not found")

    await logo_service.delete(record.storage_path)
    await BrandRepository.delete_logo_asset(db, asset_id, tenant_id=tenant_id)

    logger.info("Logo deleted, asset_id=%s", asset_id)
    return {"deleted": True, "asset_id": asset_id}
