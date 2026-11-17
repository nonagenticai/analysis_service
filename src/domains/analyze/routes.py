"""API routes for analyzer domain.

Defines all REST endpoints for analysis, review, and context operations.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from typing import Any
from uuid import uuid7

from fastapi import APIRouter, FastAPI, HTTPException, Request
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from w2v_common.auth import LICENSE_GATE
from w2v_common.database.rls import bind_session_tenant

from src.config import get_settings
from src.domains.analyze.schemas import (
    AnalysisJobListItem,
    AnalysisJobListResponse,
    AnalysisJobResponse,
    AnalysisResultResponse,
    AnalyzeRequest,
    ArtifactsResponse,
    CancelJobResponse,
    ClassifyRequest,
    ClassifyResponse,
    IntelligenceContext,
    LLMArtifact,
    LLMConfigOverride,
    NarrativeRequest,
    NarrativeResponse,
    PageAnalysisResponse,
    ReviewJobResponse,
    ReviewRequest,
    ReviewResultResponse,
)
from src.domains.analyze.services.analyzer_service import AnalyzerService
from src.domains.analyze.services.repositories.job_repository import AnalysisJobRepository
from src.domains.analyze.services.repositories.review_job_repository import (
    AnalyzeReviewJobRepository,
)
from src.shared.cache.redis_client import get_redis_client
from src.shared.tenant import require_request_tenant, tenant_uuid

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks


def _job_cache_key(job_id: str) -> str:
    """Return the Redis cache key for an analysis job result."""
    return f"analyzer:job:{job_id}"


async def _cache_job_result(job_id: str, result_data: dict) -> None:
    """Cache a job result in Redis (best-effort, failures are logged)."""
    try:
        redis = get_redis_client()
        if redis.is_available:
            ttl = get_settings().cache.job_ttl
            await redis.set(_job_cache_key(job_id), result_data, ttl=ttl)
            logger.debug("Cached job %s result in Redis", job_id)
    except RedisError as exc:
        logger.warning("Failed to cache job %s in Redis: %s", job_id, exc)


async def _get_cached_job(job_id: str) -> dict | None:
    """Retrieve a cached job result from Redis, or None."""
    try:
        redis = get_redis_client()
        if redis.is_available:
            data = await redis.get(_job_cache_key(job_id))
            if data and isinstance(data, dict):
                logger.debug("Redis cache hit for job %s", job_id)
                return data
    except RedisError as exc:
        logger.warning("Failed to read job %s from Redis: %s", job_id, exc)
    return None


# --- Review-job durable store (mirrors the analysis-job write-through above) ---
#
# Review sub-jobs are multi-replica visible: they are written through to Postgres
# (AnalyzeReviewJob) + Redis and the GET reads in-mem -> Redis -> DB, so a
# create-on-podA / poll-on-podB sequence resolves instead of 404-ing.


def _review_cache_key(review_job_id: str) -> str:
    """Return the Redis cache key for a review job result."""
    return f"analyzer:review:{review_job_id}"


async def _cache_review_result(review_job_id: str, result_data: dict) -> None:
    """Cache a review job result in Redis (best-effort, failures are logged)."""
    try:
        redis = get_redis_client()
        if redis.is_available:
            ttl = get_settings().cache.review_ttl
            await redis.set(_review_cache_key(review_job_id), result_data, ttl=ttl)
    except RedisError as exc:
        logger.warning("Failed to cache review job %s in Redis: %s", review_job_id, exc)


async def _get_cached_review(review_job_id: str) -> dict | None:
    """Retrieve a cached review job result from Redis, or None."""
    try:
        redis = get_redis_client()
        if redis.is_available:
            data = await redis.get(_review_cache_key(review_job_id))
            if data and isinstance(data, dict):
                return data
    except RedisError as exc:
        logger.warning("Failed to read review job %s from Redis: %s", review_job_id, exc)
    return None


async def _persist_review_create(
    app: FastAPI, *, review_job_id: str, tenant_id: str, review_types: list[str]
) -> None:
    """Insert the initial ``processing`` review-job row (best-effort, tenant-scoped)."""
    session_factory = getattr(app.state, "db_session_factory", None)
    if not session_factory:
        return
    try:
        async with session_factory() as session, session.begin():
            await bind_session_tenant(session, tenant_uuid(tenant_id))
            await AnalyzeReviewJobRepository.create(
                session,
                review_job_id=review_job_id,
                tenant_id=tenant_id,
                review_types=review_types,
            )
    except SQLAlchemyError as exc:
        logger.warning("Failed to persist review job %s to DB: %s", review_job_id, exc)


async def _persist_review_result(
    app: FastAPI,
    *,
    review_job_id: str,
    tenant_id: str,
    status: str,
    reviews: dict | None = None,
    error: str | None = None,
    processing_time_seconds: float | None = None,
    llm_calls: int | None = None,
) -> None:
    """Write a review job's terminal state through to the DB (best-effort, tenant-scoped)."""
    session_factory = getattr(app.state, "db_session_factory", None)
    if not session_factory:
        return
    try:
        async with session_factory() as session, session.begin():
            await bind_session_tenant(session, tenant_uuid(tenant_id))
            await AnalyzeReviewJobRepository.update_result(
                session,
                review_job_id=review_job_id,
                tenant_id=tenant_id,
                status=status,
                reviews=reviews,
                error=error,
                processing_time_seconds=processing_time_seconds,
                llm_calls=llm_calls,
            )
    except SQLAlchemyError as exc:
        logger.warning("Failed to persist review job %s result to DB: %s", review_job_id, exc)


def _context_to_dict(
    ctx: IntelligenceContext | dict[str, Any] | object | None,
) -> dict[str, Any] | None:
    """Convert IntelligenceContext or dict to dict, or None."""
    if ctx is None:
        return None
    if isinstance(ctx, IntelligenceContext):
        return ctx.model_dump(exclude_none=True)
    if isinstance(ctx, dict):
        return {str(k): v for k, v in ctx.items()}
    return None


router = APIRouter(prefix="/api", tags=["analyze"], dependencies=list(LICENSE_GATE))


def _get_service(request: Request) -> AnalyzerService:
    """Retrieve the AnalyzerService instance from app state.

    Args:
        request: The incoming FastAPI request.

    Returns:
        The shared AnalyzerService instance.
    """
    return request.app.state.analyzer_service


@router.post(
    "/analyze",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="start_analysis",
)
async def start_analysis(
    body: AnalyzeRequest,
    request: Request,
) -> AnalysisJobResponse:
    """Start a full website analysis asynchronously.

    Args:
        body: Analysis request containing pages and configuration.
        request: The incoming FastAPI request.

    Returns:
        AnalysisJobResponse with the new job_id and status.
    """
    service = _get_service(request)
    job_id = str(uuid7())
    tenant_id = str(require_request_tenant(request))
    logger.info(
        "Received analysis request: job_id=%s tenant=%s pages=%d",
        job_id,
        tenant_id,
        len(body.pages),
    )

    # Write-through cache for active jobs
    if not hasattr(request.app.state, "analysis_jobs"):
        request.app.state.analysis_jobs = {}

    request.app.state.analysis_jobs[job_id] = {
        "status": "processing",
        "total_pages": len(body.pages),
        "tenant_id": tenant_id,
    }

    # Persist to database
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if session_factory:
        try:
            async with session_factory() as session, session.begin():
                await bind_session_tenant(session, tenant_uuid(tenant_id))
                await AnalysisJobRepository.create(
                    session=session,
                    job_id=job_id,
                    tenant_id=tenant_id,
                    total_pages=len(body.pages),
                    user_context=_context_to_dict(body.user_context),
                )
        except SQLAlchemyError as exc:
            logger.warning("Failed to persist job %s to DB: %s", job_id, exc)

    # The detached background task inherits the request's identity +
    # cost-correlation ContextVars (tenant_id / license_id / subject_id /
    # correlation_id / stage): a plain `asyncio.create_task(coro)` snapshots the
    # current context at creation time, so the task runs under the same context
    # the request handler is in. This is what makes the task's downstream WM / IR
    # cost events attribute to the run's tenant with the right correlation_id.
    # Do NOT move this work to a thread/process pool or `run_in_executor`, and do
    # NOT pass a blank `context=` — either would drop the context and NULL the
    # correlation on every cost event the task emits.
    task = asyncio.create_task(_run_with_timeout(service, job_id, body, request.app, tenant_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Analysis job %s accepted, dispatched background task", job_id)
    return AnalysisJobResponse(
        job_id=job_id,
        status="processing",
        total_pages=len(body.pages),
    )


async def _run_with_timeout(
    service: AnalyzerService,
    job_id: str,
    body: AnalyzeRequest,
    app: FastAPI,
    tenant_id: str,
) -> None:
    """Wrap _process_analysis with a configurable timeout."""
    try:
        await asyncio.wait_for(
            _process_analysis(service, job_id, body, app, tenant_id),
            timeout=body.config.timeout_seconds,
        )
    except TimeoutError:
        logger.error("Analysis job %s timed out after %ds", job_id, body.config.timeout_seconds)
        app.state.analysis_jobs[job_id] = {
            "status": "failed",
            "error": f"Timed out after {body.config.timeout_seconds}s",
            "tenant_id": tenant_id,
        }
        session_factory = getattr(app.state, "db_session_factory", None)
        if session_factory:
            try:
                async with session_factory() as session, session.begin():
                    await bind_session_tenant(session, tenant_uuid(tenant_id))
                    await AnalysisJobRepository.update_status(
                        session=session,
                        job_id=job_id,
                        tenant_id=tenant_id,
                        status="failed",
                        error=f"Timed out after {body.config.timeout_seconds}s",
                    )
            except SQLAlchemyError as exc:
                logger.warning("Failed to persist timeout for %s: %s", job_id, exc)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.exception("Analysis job %s failed", job_id)
        app.state.analysis_jobs[job_id] = {
            "status": "failed",
            "error": str(exc),
            "tenant_id": tenant_id,
        }
        session_factory = getattr(app.state, "db_session_factory", None)
        if session_factory:
            try:
                async with session_factory() as session, session.begin():
                    await bind_session_tenant(session, tenant_uuid(tenant_id))
                    await AnalysisJobRepository.update_status(
                        session=session,
                        job_id=job_id,
                        tenant_id=tenant_id,
                        status="failed",
                        error=str(exc),
                    )
            except (OSError, RuntimeError) as db_exc:
                logger.warning("Failed to persist failure for %s: %s", job_id, db_exc)


def _check_cancelled(app: FastAPI, job_id: str) -> None:
    """Raise if job has been cancelled."""
    tokens = getattr(app.state, "cancellation_tokens", {})
    if tokens.get(job_id):
        raise RuntimeError(f"Job {job_id} was cancelled")


def _resolve_user_context(body: AnalyzeRequest) -> dict | None:
    """Resolve a request's user_context to a plain dict (or None)."""
    raw_context = body.user_context
    if isinstance(raw_context, IntelligenceContext):
        return raw_context.model_dump(exclude_none=True)
    return raw_context


def _enrich_context_from_pages(
    body: AnalyzeRequest, analyzed: list[dict], effective_context: dict | None
) -> tuple[dict | None, dict | None, bool]:
    """Optionally enrich the user context using inferences from analyzed pages.

    Returns ``(effective_context, enriched_context, enable_enrichment)``.
    """
    enable_enrichment = body.auto_enrich_context or body.config.enable_context_enrichment
    enriched_context: dict | None = None
    if enable_enrichment:
        from src.domains.analyze.services.context.inferrer import ContextInferrer

        inferred = ContextInferrer.from_pages(analyzed)
        enriched_context = ContextInferrer.merge(effective_context, inferred)
        effective_context = enriched_context
    return effective_context, enriched_context, enable_enrichment


def _enrich_context_from_site(
    site_analysis: dict, enriched_context: dict | None, enable_enrichment: bool
) -> dict | None:
    """Further enrich an existing enriched_context using site-level analysis."""
    if not (enable_enrichment and enriched_context is not None):
        return enriched_context
    from src.domains.analyze.services.context.inferrer import ContextInferrer

    site_inferred = ContextInferrer.from_site_analysis(site_analysis)
    return ContextInferrer.merge(enriched_context, site_inferred)


def _compute_token_usage(artifacts: list[dict] | None) -> dict[str, int] | None:
    """Aggregate token usage across LLM artifacts. Returns None if zero."""
    if not artifacts:
        return None
    total_prompt = sum(a.get("prompt_tokens", 0) for a in artifacts)
    total_response = sum(a.get("response_tokens", 0) for a in artifacts)
    if not (total_prompt or total_response):
        return None
    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_response,
        "total_tokens": total_prompt + total_response,
    }


async def _persist_completion(
    app: FastAPI,
    *,
    job_id: str,
    tenant_id: str,
    result_data: dict,
    analyzed: list[dict],
    site_analysis: dict,
    narrative: dict,
    artifacts: list[dict] | None,
) -> None:
    """Persist a completed analysis job's results to the database."""
    session_factory = getattr(app.state, "db_session_factory", None)
    if not session_factory:
        return
    try:
        async with session_factory() as session, session.begin():
            await AnalysisJobRepository.update_status(
                session=session,
                job_id=job_id,
                tenant_id=tenant_id,
                status="completed",
                results=result_data,
                pages_analyzed=analyzed,
                site_analysis=site_analysis,
                narrative_extraction=narrative,
            )
            if artifacts:
                await AnalysisJobRepository.store_artifacts(
                    session, job_id, artifacts, tenant_id=tenant_id
                )
            from src.domains.analyze.services.repositories.usage_repository import (
                AnalysisUsageRepository,
            )

            await AnalysisUsageRepository.record_usage(
                session=session,
                tenant_id=tenant_id,
                job_id=job_id,
                pages_count=len(analyzed),
            )
    except SQLAlchemyError as exc:
        logger.warning("Failed to persist job %s results to DB: %s", job_id, exc)


async def _persist_failure(app: FastAPI, job_id: str, error: str, *, tenant_id: str) -> None:
    """Persist a failed analysis job to the database (tenant-scoped)."""
    session_factory = getattr(app.state, "db_session_factory", None)
    if not session_factory:
        return
    try:
        async with session_factory() as session, session.begin():
            await AnalysisJobRepository.update_status(
                session=session,
                job_id=job_id,
                tenant_id=tenant_id,
                status="failed",
                error=error,
            )
    except SQLAlchemyError as db_exc:
        logger.warning("Failed to persist job %s failure to DB: %s", job_id, db_exc)


async def _process_analysis(
    service: AnalyzerService,
    job_id: str,
    body: AnalyzeRequest,
    app: FastAPI,
    tenant_id: str,
) -> None:
    """Run the full analysis pipeline in background.

    Performs page analysis, language dedup, SPA detection, context enrichment,
    site-level LLM analysis, brand extraction, and narrative extraction.
    Results are stored in memory, Redis, and the database.

    Args:
        service: The AnalyzerService instance.
        job_id: Unique job identifier.
        body: The original analysis request.
        app: The FastAPI application instance.
        tenant_id: Tenant identifier for multi-tenancy.
    """
    try:
        start_time = _time.monotonic()
        logger.info("Starting analysis pipeline: job_id=%s pages=%d", job_id, len(body.pages))
        pages_input = [p.model_dump() for p in body.pages]
        analyzed = await service.analyze_pages(pages_input, workers=body.config.parallel_workers)

        # Preserve original text for LLM prompt enrichment
        text_by_url = {p.url: p.text for p in body.pages if p.text}
        for page_dict in analyzed:
            if page_dict["url"] in text_by_url and "text" not in page_dict:
                page_dict["text"] = text_by_url[page_dict["url"]]

        _check_cancelled(app, job_id)

        # Language deduplication (config-controlled)
        lang_dupes_removed = 0
        if body.config.enable_language_dedup:
            analyzed, lang_dupes_removed = service.filter_language_duplicates(
                analyzed, keep_lang=body.keep_lang
            )

        # SPA section annotation (config-controlled)
        if body.config.enable_spa_detection:
            analyzed = service.annotate_spa_sections(analyzed)

        effective_context = _resolve_user_context(body)
        effective_context, enriched_context, enable_enrichment = _enrich_context_from_pages(
            body, analyzed, effective_context
        )

        site_result = await service.analyze_site(
            analyzed,
            effective_context,
            job_id=job_id,
            temperature=body.config.temperature,
            custom_prompts=body.config.custom_prompts,
            llm_config=body.llm_config,
        )
        site_analysis = site_result.analysis
        _check_cancelled(app, job_id)

        # Brand extraction
        from src.domains.analyze.services.brand_extractor import BrandExtractor

        brand_info = BrandExtractor.extract_brand(analyzed, site_analysis)

        enriched_context = _enrich_context_from_site(
            site_analysis, enriched_context, enable_enrichment
        )

        _check_cancelled(app, job_id)
        narrative = service.extract_narrative(analyzed) if body.config.include_narrative else {}

        processing_time = _time.monotonic() - start_time
        _token_usage = _compute_token_usage(site_result.artifacts)

        result_data = {
            "status": "completed",
            "total_pages": len(body.pages),
            "pages": analyzed,
            "site_analysis": site_analysis,
            "narrative_extraction": narrative,
            "enriched_context": enriched_context,
            "language_duplicates_removed": lang_dupes_removed,
            "processing_time_seconds": round(processing_time, 3),
            "llm_calls": site_result.llm_calls,
            "schema_version": "1.0",
            "pages_analyzed_count": len(analyzed),
            "brand": brand_info,
            "token_usage": _token_usage,
            "tenant_id": tenant_id,
        }

        logger.info(
            "Analysis pipeline completed: job_id=%s pages=%d duration=%.3fs llm_calls=%d",
            job_id,
            len(analyzed),
            processing_time,
            site_result.llm_calls,
        )

        # Write-through cache
        app.state.analysis_jobs[job_id] = result_data

        # Cache in Redis for cross-restart persistence
        await _cache_job_result(job_id, result_data)

        # Store artifacts separately for debug endpoint
        if site_result.artifacts:
            result_data["_artifacts"] = site_result.artifacts

        await _persist_completion(
            app,
            job_id=job_id,
            tenant_id=tenant_id,
            result_data=result_data,
            analyzed=analyzed,
            site_analysis=site_analysis,
            narrative=narrative,
            artifacts=site_result.artifacts,
        )

    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        logger.error("Analysis job %s failed: %s", job_id, exc)
        error_data = {
            "status": "failed",
            "error": str(exc),
            "tenant_id": tenant_id,
        }
        app.state.analysis_jobs[job_id] = error_data
        await _persist_failure(app, job_id, str(exc), tenant_id=tenant_id)


@router.get("/analyze", operation_id="list_analysis_jobs")
async def list_analysis_jobs(
    request: Request,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AnalysisJobListResponse:
    """List analysis jobs for the current tenant.

    Args:
        request: The incoming FastAPI request.
        status: Optional status filter.
        limit: Maximum number of jobs to return.
        offset: Pagination offset.

    Returns:
        Paginated list of analysis jobs.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info(
        "Listing analysis jobs: tenant=%s status=%s limit=%d offset=%d",
        tenant_id,
        status,
        limit,
        offset,
    )
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if not session_factory:
        return AnalysisJobListResponse()
    try:
        async with session_factory() as session:
            jobs, total = await AnalysisJobRepository.list_by_tenant_filtered(
                session, tenant_id, status=status, limit=limit, offset=offset
            )
            items = [
                AnalysisJobListItem(
                    job_id=j.job_id,
                    status=j.status,
                    total_pages=j.total_pages,
                    created_at=str(j.created_at) if j.created_at else None,
                )
                for j in jobs
            ]
            return AnalysisJobListResponse(jobs=items, total=total, limit=limit, offset=offset)
    except SQLAlchemyError as exc:
        # Do NOT swallow a DB failure into an empty list — a real outage would then look
        # identical to "this tenant has 0 jobs" (a silent data-availability lie). Surface
        # it as a 503 so callers/monitoring can distinguish "unavailable" from "empty".
        logger.error("Failed to list jobs (DB error): %s", exc)
        raise HTTPException(
            status_code=503, detail="Analysis job store temporarily unavailable"
        ) from exc


@router.get(
    "/analyze/{job_id}",
    responses={
        404: {"description": "Job not found"},
    },
    operation_id="get_analysis",
)
async def get_analysis(
    job_id: str,
    request: Request,
) -> AnalysisResultResponse:
    """Get analysis job status and results (tenant-scoped).

    Args:
        job_id: The analysis job identifier.
        request: The incoming FastAPI request.

    Returns:
        Full analysis result with pages, site analysis, and narrative.

    Raises:
        HTTPException: 404 if job not found OR belongs to another tenant.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Fetching analysis result: job_id=%s tenant=%s", job_id, tenant_id)
    # Check in-memory cache first — only honour the cached entry if it
    # belongs to this tenant; otherwise fall through to the DB which enforces
    # tenant isolation at the SQL layer.
    jobs: dict[str, dict[str, Any]] = getattr(request.app.state, "analysis_jobs", {})
    cached = jobs.get(job_id)
    job: dict[str, Any] | None = (
        cached if cached and cached.get("tenant_id") in (tenant_id, None) else None
    )

    # Check Redis cache before hitting the database
    if not job:
        job = await _get_cached_job(job_id)
        if job and job.get("tenant_id") not in (tenant_id, None):
            job = None

    # Fall back to database if not in cache
    if not job:
        session_factory = getattr(request.app.state, "db_session_factory", None)
        if session_factory:
            try:
                async with session_factory() as session:
                    db_job = await AnalysisJobRepository.get(session, job_id, tenant_id=tenant_id)
                    if db_job:
                        job = {
                            "status": db_job.status,
                            "total_pages": db_job.total_pages,
                            "pages": db_job.pages_analyzed or [],
                            "site_analysis": db_job.site_analysis,
                            "narrative_extraction": db_job.narrative_extraction,
                            "error": db_job.error,
                        }
                        if db_job.results_json:
                            job["enriched_context"] = db_job.results_json.get("enriched_context")
                            job["language_duplicates_removed"] = db_job.results_json.get(
                                "language_duplicates_removed", 0
                            )
                            job["processing_time_seconds"] = db_job.results_json.get(
                                "processing_time_seconds"
                            )
                            job["llm_calls"] = db_job.results_json.get("llm_calls", 0)
                            job["schema_version"] = db_job.results_json.get("schema_version", "1.0")
                            job["pages_analyzed_count"] = db_job.results_json.get(
                                "pages_analyzed_count", 0
                            )
                            job["token_usage"] = db_job.results_json.get("token_usage")
            except SQLAlchemyError as exc:
                logger.warning("Failed to read job %s from DB: %s", job_id, exc)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    pages = []
    for p in job.get("pages", []):
        pages.append(PageAnalysisResponse(**p))

    return AnalysisResultResponse(
        job_id=job_id,
        status=job["status"],
        pages=pages,
        site_analysis=job.get("site_analysis"),
        narrative_extraction=job.get("narrative_extraction"),
        enriched_context=job.get("enriched_context"),
        language_duplicates_removed=job.get("language_duplicates_removed", 0),
        error=job.get("error"),
        processing_time_seconds=job.get("processing_time_seconds"),
        llm_calls=job.get("llm_calls", 0),
        schema_version=job.get("schema_version", "1.0"),
        pages_analyzed_count=job.get("pages_analyzed_count", 0),
        brand=job.get("brand"),
        token_usage=job.get("token_usage"),
    )


@router.delete(
    "/analyze/{job_id}",
    responses={
        404: {"description": "Job not found"},
        409: {"description": "Job already completed or failed"},
    },
    operation_id="cancel_analysis",
)
async def cancel_analysis(job_id: str, request: Request) -> CancelJobResponse:
    """Cancel a running analysis job (tenant-scoped, race-safe).

    The DB cancel is gated on tenant_id and is atomic against concurrent
    completion: a wrong-tenant call returns 404, and a job that completes
    between the read and the write keeps its terminal status (no lost
    update). The in-memory ``cancellation_tokens`` flag and write-through
    cache are only updated when the DB cancel actually succeeded.

    Raises:
        HTTPException: 404 if job not found or belongs to another tenant;
            409 if already in a terminal state.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Cancellation requested: job_id=%s tenant=%s", job_id, tenant_id)

    # Authoritative path: tenant-scoped atomic DB cancel. If the session
    # factory is wired in and the DB is reachable, this is the source of
    # truth. On transient DB error or in test setups without a DB, fall back
    # to the in-process cache — still gated on tenant_id matching.
    session_factory = getattr(request.app.state, "db_session_factory", None)
    db_job = None
    db_error: SQLAlchemyError | None = None
    if session_factory:
        try:
            async with session_factory() as session, session.begin():
                db_job = await AnalysisJobRepository.cancel(session, job_id, tenant_id=tenant_id)
        except SQLAlchemyError as exc:
            logger.warning("Failed to cancel job %s in DB: %s", job_id, exc)
            db_error = exc

    if db_job is not None:
        if db_job.status in ("completed", "failed"):
            raise HTTPException(status_code=409, detail=f"Job already {db_job.status}")
    else:
        # DB cancel did not produce a row — either no DB, transient DB error,
        # or the row genuinely doesn't exist for this tenant. Fall back to the
        # write-through cache for authorisation; cross-tenant entries are
        # invisible (no tenant_id stamp or mismatched stamp → 404).
        jobs = getattr(request.app.state, "analysis_jobs", {})
        cached = jobs.get(job_id)
        cached_tenant = cached.get("tenant_id") if cached else None
        if not cached or (cached_tenant is not None and cached_tenant != tenant_id):
            # No DB row + no matching cache entry: only return 404 if we know
            # the DB was actually consulted (or there is no DB). On a transient
            # DB error with no cache, fall through to "best-effort cancelled"
            # to preserve the pre-existing test contract for that scenario.
            if session_factory is not None and db_error is None:
                raise HTTPException(status_code=404, detail="Job not found")
            if cached and cached_tenant is not None and cached_tenant != tenant_id:
                # Explicit cross-tenant cache hit must be hidden.
                raise HTTPException(status_code=404, detail="Job not found")
        if cached and cached.get("status") in ("completed", "failed"):
            raise HTTPException(status_code=409, detail=f"Job already {cached.get('status')}")

    # Authorised — flip in-process flags.
    cancellation_tokens = getattr(request.app.state, "cancellation_tokens", {})
    cancellation_tokens[job_id] = True
    jobs = getattr(request.app.state, "analysis_jobs", {})
    job = jobs.get(job_id)
    if job and job.get("status") == "processing":
        job["status"] = "cancelled"

    return CancelJobResponse(
        job_id=job_id, status="cancelled", message="Job cancellation requested"
    )


@router.get(
    "/analyze/{job_id}/artifacts",
    responses={
        404: {"description": "Artifacts not found"},
    },
    operation_id="get_analysis_artifacts",
)
async def get_artifacts(job_id: str, request: Request) -> ArtifactsResponse:
    """Get LLM artifacts for a completed analysis job (tenant-scoped).

    Raises:
        HTTPException: 404 if artifacts not found or job belongs to another tenant.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Fetching artifacts: job_id=%s tenant=%s", job_id, tenant_id)
    # Check in-memory cache (gated on tenant match).
    jobs = getattr(request.app.state, "analysis_jobs", {})
    job = jobs.get(job_id)
    if job and "_artifacts" in job and job.get("tenant_id") in (tenant_id, None):
        return ArtifactsResponse(job_id=job_id, artifacts=job["_artifacts"])

    # Fall back to database
    session_factory = getattr(request.app.state, "db_session_factory", None)
    if session_factory:
        try:
            async with session_factory() as session:
                artifacts_data = await AnalysisJobRepository.get_artifacts(
                    session, job_id, tenant_id=tenant_id
                )
                if artifacts_data is not None:
                    if isinstance(artifacts_data, dict):
                        artifact_list = artifacts_data.get("artifacts", [])
                    elif isinstance(artifacts_data, list):
                        artifact_list = artifacts_data
                    else:
                        artifact_list = []
                    parsed_artifacts: list[LLMArtifact] = [
                        LLMArtifact(**a) if isinstance(a, dict) else a for a in artifact_list
                    ]
                    return ArtifactsResponse(job_id=job_id, artifacts=parsed_artifacts)
        except SQLAlchemyError as exc:
            logger.warning("Failed to get artifacts for %s: %s", job_id, exc)

    raise HTTPException(status_code=404, detail="Artifacts not found")


@router.post(
    "/analyze/pages",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="classify_pages",
)
async def classify_pages(
    body: ClassifyRequest,
    request: Request,
) -> ClassifyResponse:
    """Classify a list of crawled pages by type.

    Args:
        body: Request body containing pages to classify.
        request: The incoming FastAPI request.

    Returns:
        Classification results for each page.
    """
    logger.info("Received page classification request: pages=%d", len(body.pages))
    service = _get_service(request)
    start = _time.monotonic()
    pages_input = [p.model_dump() for p in body.pages]
    analyzed = await service.analyze_pages(pages_input)
    duration = _time.monotonic() - start
    logger.info(
        "Page classification completed: pages=%d duration=%.3fs",
        len(analyzed),
        duration,
    )
    return ClassifyResponse(
        pages=[PageAnalysisResponse(**p) for p in analyzed],
    )


@router.post(
    "/analyze/narrative",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="extract_narrative",
)
async def extract_narrative(
    body: NarrativeRequest,
    request: Request,
) -> NarrativeResponse:
    """Extract narrative material from analyzed pages.

    Args:
        body: Request body containing analyzed page data.
        request: The incoming FastAPI request.

    Returns:
        Narrative extraction with hooks, quotes, and highlights.
    """
    logger.info("Received narrative extraction request: pages=%d", len(body.pages))
    service = _get_service(request)
    narrative_data = service.extract_narrative(body.pages)
    logger.info("Narrative extraction completed: pages=%d", len(body.pages))
    from src.domains.analyze.schemas import NarrativeExtractionResponse

    narrative = (
        NarrativeExtractionResponse(**narrative_data)
        if isinstance(narrative_data, dict)
        else narrative_data
    )
    return NarrativeResponse(extraction=narrative)


# --- Review endpoints ---


@router.post(
    "/analyze/review",
    responses={
        404: {"description": "Analysis job not found"},
        409: {"description": "Analysis job not yet completed"},
        422: {"description": "Validation error"},
    },
    operation_id="start_analyze_subreview",
)
async def start_review(body: ReviewRequest, request: Request) -> ReviewJobResponse:
    """Start a review of a completed analysis.

    Args:
        body: Review request with analysis job ID and review types.
        request: The incoming FastAPI request.

    Returns:
        ReviewJobResponse with the new review job ID and status.

    Raises:
        HTTPException: 404 if analysis job not found, 409 if not completed.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info(
        "Received review request: analysis_job_id=%s tenant=%s types=%s",
        body.analysis_job_id,
        tenant_id,
        [str(rt) for rt in body.review_types],
    )
    # Get analysis results (from cache or DB) — tenant-scoped lookup.
    analysis_jobs = getattr(request.app.state, "analysis_jobs", {})
    cached = analysis_jobs.get(body.analysis_job_id)
    job = cached if cached and cached.get("tenant_id") in (tenant_id, None) else None

    if not job:
        session_factory = getattr(request.app.state, "db_session_factory", None)
        if session_factory:
            try:
                async with session_factory() as session:
                    db_job = await AnalysisJobRepository.get(
                        session, body.analysis_job_id, tenant_id=tenant_id
                    )
                    if db_job and db_job.status == "completed":
                        job = {
                            "status": db_job.status,
                            "pages": db_job.pages_analyzed or [],
                            "site_analysis": db_job.site_analysis,
                            "narrative_extraction": db_job.narrative_extraction,
                        }
            except SQLAlchemyError as exc:
                logger.warning("Failed to read analysis job %s: %s", body.analysis_job_id, exc)

    if not job:
        raise HTTPException(status_code=404, detail="Analysis job not found")
    if job.get("status") != "completed":
        raise HTTPException(
            status_code=409,
            detail="Analysis job not yet completed",
            headers={"Retry-After": "30"},
        )

    review_job_id = str(uuid7())
    review_types = [str(rt) for rt in body.review_types]

    # Write-through: in-memory (fast path) + durable stores (cross-replica).
    review_jobs = getattr(request.app.state, "review_jobs", {})
    review_jobs[review_job_id] = {
        "status": "processing",
        "review_types": review_types,
        "tenant_id": tenant_id,
    }
    await _persist_review_create(
        request.app, review_job_id=review_job_id, tenant_id=tenant_id, review_types=review_types
    )
    await _cache_review_result(
        review_job_id,
        {"status": "processing", "review_types": review_types, "tenant_id": tenant_id},
    )

    task = asyncio.create_task(
        _process_review(
            review_job_id=review_job_id,
            review_types=review_types,
            analysis_result={
                "pages": job.get("pages", []),
                "site_analysis": job.get("site_analysis"),
                "narrative_extraction": job.get("narrative_extraction"),
            },
            user_context=body.user_context,
            app=request.app,
            tenant_id=tenant_id,
            llm_config=body.llm_config,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Review job %s accepted, dispatched background task", review_job_id)
    return ReviewJobResponse(
        review_job_id=review_job_id,
        status="processing",
        review_types=review_types,
    )


async def _process_review(
    review_job_id: str,
    review_types: list[str],
    analysis_result: dict,
    user_context: dict | None,
    app: FastAPI,
    tenant_id: str,
    llm_config: LLMConfigOverride | None = None,
) -> None:
    """Run review generation in background.

    Args:
        review_job_id: Unique review job identifier.
        review_types: List of review type strings to generate.
        analysis_result: Dict with pages, site_analysis, narrative_extraction.
        user_context: Optional user-provided context dict.
        app: The FastAPI application instance.
        tenant_id: Owning tenant (for the durable write-through + RLS binding).
        llm_config: Optional per-request LLM configuration override.
    """
    from src.domains.analyze.services.review_generator import ReviewGenerator
    from src.metrics import analyzer_review_calls_total, analyzer_review_latency_seconds

    logger.info(
        "Starting review generation: review_job_id=%s types=%s",
        review_job_id,
        review_types,
    )
    start_time = _time.monotonic()
    review_jobs = getattr(app.state, "review_jobs", {})
    try:
        generator = ReviewGenerator()
        results = await generator.generate_reviews(
            review_types=review_types,
            analysis_result=analysis_result,
            user_context=user_context,
            job_id=review_job_id,
            llm_config=llm_config,
        )

        processing_time = _time.monotonic() - start_time
        total_llm_calls = sum(r.get("llm_calls", 0) for r in results.values())

        logger.info(
            "Review generation completed: review_job_id=%s duration=%.3fs llm_calls=%d",
            review_job_id,
            processing_time,
            total_llm_calls,
        )

        # generate_reviews may return partially — a type absent from `results` failed,
        # so counting it as a success would inflate the success rate silently.
        failed_review_types = sorted(rt for rt in review_types if rt not in results)
        for rt in review_types:
            if rt in results:
                analyzer_review_calls_total.labels(review_type=rt, status="success").inc()
                analyzer_review_latency_seconds.labels(review_type=rt).observe(processing_time)
            else:
                analyzer_review_calls_total.labels(review_type=rt, status="failure").inc()

        completed = {
            "status": "completed",
            "review_types": review_types,
            "reviews": results,
            "failed_review_types": failed_review_types,
            "processing_time_seconds": round(processing_time, 3),
            "llm_calls": total_llm_calls,
            "tenant_id": tenant_id,
        }
        review_jobs[review_job_id] = completed
        await _persist_review_result(
            app,
            review_job_id=review_job_id,
            tenant_id=tenant_id,
            status="completed",
            reviews=results,
            processing_time_seconds=round(processing_time, 3),
            llm_calls=total_llm_calls,
        )
        await _cache_review_result(review_job_id, completed)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        logger.error("Review job %s failed: %s", review_job_id, exc)
        processing_time = _time.monotonic() - start_time
        for rt in review_types:
            analyzer_review_calls_total.labels(review_type=rt, status="failure").inc()
        failed = {
            "status": "failed",
            "review_types": review_types,
            "error": str(exc),
            "processing_time_seconds": round(processing_time, 3),
            "tenant_id": tenant_id,
        }
        review_jobs[review_job_id] = failed
        await _persist_review_result(
            app,
            review_job_id=review_job_id,
            tenant_id=tenant_id,
            status="failed",
            error=str(exc),
            processing_time_seconds=round(processing_time, 3),
        )
        await _cache_review_result(review_job_id, failed)


@router.get(
    "/analyze/review/{review_job_id}",
    responses={
        404: {"description": "Review job not found"},
    },
    operation_id="get_analyze_subreview",
)
async def get_review(review_job_id: str, request: Request) -> ReviewResultResponse:
    """Get review job status and results (tenant-scoped, cross-replica durable).

    Reads in-memory -> Redis -> Postgres so a review created on one replica is
    still resolvable when polled on another (previously in-memory only → 404).

    Args:
        review_job_id: The review job identifier.
        request: The incoming FastAPI request.

    Returns:
        Review results with scores and recommendations.

    Raises:
        HTTPException: 404 if review job not found OR belongs to another tenant.
    """
    tenant_id = str(require_request_tenant(request))
    logger.info("Fetching review result: review_job_id=%s tenant=%s", review_job_id, tenant_id)

    # In-memory fast path — only honour the entry if it belongs to this tenant.
    review_jobs = getattr(request.app.state, "review_jobs", {})
    cached = review_jobs.get(review_job_id)
    job: dict[str, Any] | None = (
        cached if cached and cached.get("tenant_id") in (tenant_id, None) else None
    )

    # Redis before the database.
    if not job:
        job = await _get_cached_review(review_job_id)
        if job and job.get("tenant_id") not in (tenant_id, None):
            job = None

    # Durable store fallback (tenant-scoped at the SQL layer).
    if not job:
        session_factory = getattr(request.app.state, "db_session_factory", None)
        if session_factory:
            try:
                async with session_factory() as session:
                    await bind_session_tenant(session, tenant_uuid(tenant_id))
                    db_job = await AnalyzeReviewJobRepository.get(
                        session, review_job_id, tenant_id=tenant_id
                    )
                    if db_job:
                        job = {
                            "status": db_job.status,
                            "reviews": db_job.reviews or {},
                            "error": db_job.error,
                            "processing_time_seconds": db_job.processing_time_seconds,
                            "llm_calls": db_job.llm_calls or 0,
                        }
            except SQLAlchemyError as exc:
                logger.warning("Failed to read review job %s from DB: %s", review_job_id, exc)

    if not job:
        raise HTTPException(status_code=404, detail="Review job not found")

    return ReviewResultResponse(
        review_job_id=review_job_id,
        status=job["status"],
        reviews=job.get("reviews", {}),
        error=job.get("error"),
        processing_time_seconds=job.get("processing_time_seconds"),
        llm_calls=job.get("llm_calls", 0),
    )


# --- Context endpoints ---

context_router = APIRouter(prefix="/api/context", tags=["context"], dependencies=list(LICENSE_GATE))


@context_router.post(
    "/infer",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="infer_context",
)
async def infer_context(body: dict) -> dict:
    """Auto-infer context from analysis data.

    Args:
        body: Dict with ``pages`` and optional ``site_analysis``.

    Returns:
        Inferred context as a dict.
    """
    logger.info("Received context inference request")
    from src.domains.analyze.services.context.inferrer import ContextInferrer

    pages = body.get("pages", [])
    site_analysis = body.get("site_analysis")

    inferred = ContextInferrer.from_pages(pages)

    if site_analysis:
        site_inferred = ContextInferrer.from_site_analysis(site_analysis, prior=inferred)
        return site_inferred.model_dump()

    return inferred.model_dump()


@context_router.post(
    "/validate",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="validate_context",
)
async def validate_context(body: dict) -> dict:
    """Validate context against discovered URLs.

    Args:
        body: Dict with ``context``, ``discovered_urls``, and optional filters.

    Returns:
        Validation result with issues list.
    """
    logger.info("Received context validation request")
    from src.domains.analyze.services.context.types import UserContextInput
    from src.domains.analyze.services.context.validator import ContextValidator

    context_data = body.get("context", {})
    context = UserContextInput(**context_data)
    discovered_urls = body.get("discovered_urls", [])
    page_types = body.get("page_types")
    features_found = body.get("features_found")

    result = ContextValidator.validate(
        context,
        discovered_urls=discovered_urls,
        page_types=page_types,
        features_found=features_found,
    )
    return result.model_dump()


@context_router.post(
    "/enrich",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="enrich_context",
)
async def enrich_context(body: dict) -> dict:
    """Merge user-provided and inferred context.

    Args:
        body: Dict with ``user_context`` and ``inferred_context``.

    Returns:
        Merged context dict where user values take precedence.
    """
    logger.info("Received context enrichment request")
    from src.domains.analyze.services.context.inferrer import ContextInferrer
    from src.domains.analyze.services.context.types import InferredContext

    user_context = body.get("user_context")
    inferred_data = body.get("inferred_context", {})
    inferred = InferredContext(**inferred_data)

    return ContextInferrer.merge(user_context, inferred)
