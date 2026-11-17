"""API routes for review domain."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime
from urllib.parse import urlparse
from uuid import uuid7

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from w2v_common.auth import LICENSE_GATE

from src.config import get_settings
from src.domains.review.schemas import (
    BatchReviewRequest,
    BatchReviewResponse,
    BatchStatusResponse,
    DomainHistoryEntry,
    DomainHistoryResponse,
    FindingsListResponse,
    ReviewComparisonResponse,
    ReviewFinding,
    ReviewJobResponse,
    ReviewListResponse,
    ReviewReportResponse,
    ReviewRequest,
    ReviewResultResponse,
    ReviewSummary,
    ReviewVideoResponse,
    UsageListResponse,
    UsageResponse,
)
from src.domains.review.services.job_manager import JobManager
from src.domains.review.services.review_service import ReviewService
from src.domains.review.services.summary import generate_summary
from src.metrics import review_active_jobs, review_jobs_total
from src.shared.database.session import get_db, get_db_session
from src.shared.tenant import require_request_tenant

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()

router = APIRouter(prefix="/api", tags=["review"], dependencies=list(LICENSE_GATE))


def _get_service(request: Request) -> ReviewService:
    """Extract the ReviewService instance from application state."""
    return request.app.state.review_service


def _get_tenant_id(request: Request) -> str:
    """The caller's tenant from request state, validated as a real uuid, or HTTP 401.

    Delegates to the single shared boundary in ``src.shared.tenant`` rather than
    reading ``request.state`` directly. Two defects are closed at once:

    * the old body fabricated the literal ``"anonymous"`` when the attribute was
      missing — a sentinel that is not a tenant, and one of exactly two dirty
      values migration 0013 had to remediate out of the live tables;
    * since 0013 ``tenant_id`` is a native ``uuid`` column, passing a non-uuid
      slug through no longer degrades to "0 rows / 404": Postgres raises
      ``InvalidTextRepresentation: invalid input syntax for type uuid`` and the
      caller gets a **500**. Malformed identity must be a 4xx.

    ``require_request_tenant`` answers 401 (not 400) for BOTH absence and a
    non-uuid value, and that is this repo's established convention — see its
    docstring: ``request.state.tenant_id`` is set exclusively by ``AuthMiddleware``
    from a verified license token, so a bad value there means the requestor was
    never correctly identified, not that their payload was wrong. Five other call
    sites in this service already answer 401 for this exact condition.
    """
    return str(require_request_tenant(request))


# ============================================================================
# Core Review Endpoints
# ============================================================================


@router.post(
    "/review",
    responses={
        400: {"description": "Invalid callback URL or page limit exceeded"},
        422: {"description": "Validation error"},
    },
    operation_id="start_review",
)
async def start_review(
    body: ReviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ReviewJobResponse:
    """Start review generation (async).

    Args:
        body: Review request with analysis data and review types.
        request: FastAPI request for tenant context.
        db: Async database session.

    Returns:
        ReviewJobResponse with job_id and initial status.

    Raises:
        HTTPException: 400 if callback URL is invalid or page limit exceeded.
    """
    logger.info(
        "Received review request: review_types=%s, pages=%d",
        body.review_types,
        len(body.analysis.pages),
    )
    settings = get_settings()

    # Phase 3: Validate callback URL for SSRF
    if body.callback_url:
        from src.shared.security.url_validator import validate_callback_url

        try:
            validate_callback_url(body.callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid callback URL: {exc}") from exc

    # Phase 3: Enforce page count limit
    if len(body.analysis.pages) > settings.review.max_pages_per_request:
        raise HTTPException(
            status_code=400,
            detail=f"Too many pages: {len(body.analysis.pages)} exceeds limit of {settings.review.max_pages_per_request}",
        )

    service = _get_service(request)
    job_id = str(uuid7())
    tenant_id = getattr(request.state, "tenant_id", "anonymous")

    # Extract URL and domain from analysis
    url = None
    domain = None
    if body.analysis.pages:
        url = body.analysis.pages[0].url
        try:
            domain = urlparse(url).netloc
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            logger.warning("Failed to parse domain from URL %s: %s", url, exc)

    # Persist job to database
    await JobManager.create_job(
        db,
        job_id=job_id,
        tenant_id=tenant_id,
        review_types=body.review_types,
        url=url,
        domain=domain,
        user_context=body.user_context.model_dump() if body.user_context else None,
    )

    # Commit the job row before dispatching the background task — the task opens
    # its own session and UPDATEs this row; without this commit it can race the
    # (post-handler) INSERT commit and update a not-yet-visible row.
    await db.commit()

    review_jobs_total.labels(tenant_id=tenant_id, status="created").inc()
    review_active_jobs.inc()

    task = asyncio.create_task(_process_review(service, job_id, tenant_id, body))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    logger.info("Review job %s created for tenant %s", job_id, tenant_id)
    return ReviewJobResponse(
        job_id=job_id,
        status="processing",
        review_types=body.review_types,
    )


async def _cache_completed_result(
    tenant_id: str, job_id: str, results: dict, token_usage: object
) -> None:
    """Best-effort write of a completed review result to the cache."""
    settings = get_settings()
    if not settings.cache.enabled:
        return
    from src.domains.review.services.review_cache import set_cached_result

    await set_cached_result(
        tenant_id,
        job_id,
        {
            "job_id": job_id,
            "status": "completed",
            "results": results,
            "error": None,
            "token_usage": token_usage,
        },
        ttl=settings.cache.ttl,
    )


async def _deliver_review_webhook(tenant_id: str, job_id: str, body: ReviewRequest) -> None:
    """Best-effort webhook dispatch on review completion or failure."""
    if not body.callback_url:
        return
    try:
        settings = get_settings()
        if not settings.webhook.enabled:
            return
        from src.domains.review.services.webhook import deliver_webhook

        async with get_db_session() as db:
            job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
            if job:
                payload = {
                    "job_id": job_id,
                    "status": job.status,
                    "review_types": body.review_types,
                }
                await deliver_webhook(body.callback_url, payload)
    except OSError, ValueError, RuntimeError, TimeoutError:
        logger.exception("Webhook delivery failed for job %s", job_id)


async def _process_review(
    service: ReviewService,
    job_id: str,
    tenant_id: str,
    body: ReviewRequest,
) -> None:
    """Background task to process reviews. Opens its own DB session."""
    start_time = time.monotonic()
    logger.info("Processing review job %s: review_types=%s", job_id, body.review_types)
    try:
        effective_types = body.review_types
        if body.user_context and body.user_context.critique_focus:
            effective_types = body.user_context.critique_focus

        settings = get_settings()
        on_progress = None
        if settings.feature_flags.sse_progress:

            async def _update_partial(partial_results: dict) -> None:
                async with get_db_session() as db:
                    await JobManager.update_results_partial(db, job_id, partial_results)

            on_progress = _update_partial

        results = await service.generate_all(
            analysis=body.analysis,
            review_types=effective_types,
            message=body.message,
            user_context=body.user_context,
            on_progress=on_progress,
            llm_config=body.llm_config,
        )

        token_usage = results.pop("_token_usage", None)

        async with get_db_session() as db:
            await JobManager.mark_completed(db, job_id, results)
            await JobManager.record_usage(db, tenant_id=tenant_id, is_error=False)

        review_jobs_total.labels(tenant_id=tenant_id, status="completed").inc()
        duration = time.monotonic() - start_time
        logger.info("Review job %s completed in %.2fs", job_id, duration)
        await _cache_completed_result(tenant_id, job_id, results, token_usage)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        logger.error("Review job %s failed: %s", job_id, exc)
        try:
            async with get_db_session() as db:
                await JobManager.mark_failed(db, job_id, str(exc))
                await JobManager.record_usage(db, tenant_id=tenant_id, is_error=True)
        except SQLAlchemyError:
            logger.exception("Failed to mark review job %s as failed", job_id)

        review_jobs_total.labels(tenant_id=tenant_id, status="failed").inc()
    finally:
        review_active_jobs.dec()
        await _deliver_review_webhook(tenant_id, job_id, body)


# Phase 3.2: Compare endpoint (MUST be before /{job_id} to avoid path capture)
@router.get(
    "/review/compare",
    responses={
        400: {"description": "Job not completed"},
        404: {"description": "Job not found"},
    },
    operation_id="compare_reviews",
)
async def compare_reviews_endpoint(
    request: Request,
    job_id_1: str = Query(...),
    job_id_2: str = Query(...),
    db: AsyncSession = Depends(get_db),
) -> ReviewComparisonResponse:
    """Compare two review jobs and compute score deltas.

    Args:
        request: FastAPI request.
        job_id_1: Baseline (older) job ID.
        job_id_2: Current (newer) job ID.
        db: Async database session.

    Returns:
        ReviewComparisonResponse with score deltas and qualitative diffs.

    Raises:
        HTTPException: 404 if either job not found, 400 if not completed.
    """
    logger.info("Comparing reviews: job_id_1=%s, job_id_2=%s", job_id_1, job_id_2)
    tenant_id = _get_tenant_id(request)
    # BOTH sides are scoped: the caller supplies both ids, so an unscoped
    # second slot would read any job in the estate and disclose its deltas.
    job1 = await JobManager.get_job(db, job_id_1, tenant_id=tenant_id)
    job2 = await JobManager.get_job(db, job_id_2, tenant_id=tenant_id)

    if not job1:
        raise HTTPException(status_code=404, detail=f"Job {job_id_1} not found")
    if not job2:
        raise HTTPException(status_code=404, detail=f"Job {job_id_2} not found")
    if job1.status != "completed" or not job1.results:
        raise HTTPException(status_code=400, detail=f"Job {job_id_1} is not completed")
    if job2.status != "completed" or not job2.results:
        raise HTTPException(status_code=400, detail=f"Job {job_id_2} is not completed")

    from src.domains.review.services.comparison import compare_reviews

    result = compare_reviews(job1.results, job2.results)
    logger.info(
        "Comparison complete: job_id_1=%s, job_id_2=%s, overall_delta=%s",
        job_id_1,
        job_id_2,
        result.get("overall_delta"),
    )
    return ReviewComparisonResponse(
        job_id_1=job_id_1,
        job_id_2=job_id_2,
        **result,
    )


# Phase 4.5: Batch endpoints (MUST be before /{job_id} to avoid path capture)
@router.post(
    "/review/batch",
    responses={
        400: {"description": "Invalid callback URL"},
        422: {"description": "Validation error"},
        501: {"description": "Batch reviews not enabled"},
    },
    operation_id="start_batch_review",
)
async def start_batch_review(
    body: BatchReviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BatchReviewResponse:
    """Start batch review generation (async). Requires feature flag.

    Args:
        body: Batch request containing multiple review requests.
        request: FastAPI request for tenant context.
        db: Async database session.

    Returns:
        BatchReviewResponse with batch_id and job_ids.

    Raises:
        HTTPException: 501 if batch reviews not enabled, 400 if callback URL invalid.
    """
    logger.info("Received batch review request: count=%d", len(body.reviews))
    settings = get_settings()
    if not settings.feature_flags.batch_reviews:
        raise HTTPException(status_code=501, detail="Batch reviews not enabled")

    # Phase 3: Validate batch callback URL for SSRF
    if body.callback_url:
        from src.shared.security.url_validator import validate_callback_url

        try:
            validate_callback_url(body.callback_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid callback URL: {exc}") from exc

    service = _get_service(request)
    tenant_id = getattr(request.state, "tenant_id", "anonymous")
    batch_id = str(uuid7())
    job_ids: list[str] = []

    for review_req in body.reviews:
        job_id = str(uuid7())
        job_ids.append(job_id)

        url = None
        domain = None
        if review_req.analysis.pages:
            url = review_req.analysis.pages[0].url
            try:
                domain = urlparse(url).netloc
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                logger.warning("Failed to parse domain from URL %s: %s", url, exc)

        await JobManager.create_job(
            db,
            job_id=job_id,
            tenant_id=tenant_id,
            review_types=review_req.review_types,
            url=url,
            domain=domain,
            user_context=review_req.user_context.model_dump() if review_req.user_context else None,
        )
        # Commit each job row before dispatching its background task — the task
        # opens its own session and UPDATEs this row; without this commit it can
        # race the (post-handler) INSERT commit and update a not-yet-visible row.
        await db.commit()
        review_jobs_total.labels(tenant_id=tenant_id, status="created").inc()
        review_active_jobs.inc()
        task = asyncio.create_task(_process_review(service, job_id, tenant_id, review_req))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    await JobManager.create_batch(
        db,
        batch_id=batch_id,
        tenant_id=tenant_id,
        job_ids=job_ids,
        total=len(body.reviews),
        callback_url=body.callback_url,
    )

    logger.info(
        "Batch %s created with %d jobs for tenant %s", batch_id, len(body.reviews), tenant_id
    )
    return BatchReviewResponse(
        batch_id=batch_id,
        status="processing",
        total=len(body.reviews),
        job_ids=job_ids,
    )


@router.get(
    "/review/batch/{batch_id}",
    responses={
        404: {"description": "Batch not found"},
    },
    operation_id="get_review_batch_status",
)
async def get_batch_status(
    batch_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BatchStatusResponse:
    """Get batch review status.

    Args:
        batch_id: The batch identifier.
        db: Async database session.

    Returns:
        BatchStatusResponse with completion counts.

    Raises:
        HTTPException: 404 if batch not found.
    """
    logger.debug("Fetching batch status: batch_id=%s", batch_id)
    tenant_id = _get_tenant_id(request)
    batch = await JobManager.get_batch(db, batch_id, tenant_id=tenant_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    # Update progress from individual jobs
    await JobManager.update_batch_progress(db, batch_id)
    batch = await JobManager.get_batch(db, batch_id, tenant_id=tenant_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")

    return BatchStatusResponse(
        batch_id=batch_id,
        status=batch.status,
        total=batch.total,
        completed=batch.completed,
        failed=batch.failed,
        job_ids=batch.job_ids or [],
    )


@router.post(
    "/review/{job_id}/video",
    responses={
        400: {"description": "Job not completed or no findings"},
        404: {"description": "Job not found"},
        501: {"description": "Video generation is disabled"},
        503: {"description": "Video compiler unavailable"},
    },
    operation_id="request_review_video",
)
async def request_review_video(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ReviewVideoResponse:
    """Request video generation for a completed review job.

    Extracts findings from the review results and sends them to the
    video_compiler_service for video generation.

    Args:
        job_id: The review job identifier.
        db: Async database session.

    Returns:
        ReviewVideoResponse with video job_id and poll URL.

    Raises:
        HTTPException: 404 if job not found, 400 if not completed,
            503 if video compiler is unavailable or disabled.
    """
    settings = get_settings()
    if not settings.video_compiler.enabled:
        raise HTTPException(status_code=501, detail="Video generation is disabled")

    job = await JobManager.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or not job.results:
        raise HTTPException(status_code=400, detail="Job not completed")

    from src.domains.review.services.findings import extract_findings
    from src.domains.review.services.video_client import VideoCompilerClient

    all_findings = extract_findings(job.results)
    if not all_findings:
        raise HTTPException(status_code=400, detail="No findings to generate video from")

    from w2v_common.discovery import service_url

    client: VideoCompilerClient | None = None
    try:
        client = VideoCompilerClient(
            base_url=service_url("video_production"),
            timeout=float(settings.video_compiler.timeout),
        )
        result = await client.request_findings_video(
            findings=all_findings,
            title=f"Review: {job.domain or job_id}",
            subtitle=f"Job {job_id}",
        )
    # ``request_findings_video`` documents exactly what it raises:
    # ``httpx.HTTPStatusError`` and ``httpx.TransportError``. NEITHER derives
    # from ``OSError`` or ``RuntimeError`` -- the whole httpx tree hangs off
    # ``httpx.HTTPError -> Exception`` -- and httpx's own ``TimeoutException``
    # is not the builtin ``TimeoutError``. So the old builtin-only tuple caught
    # nothing this call can throw: the documented 503 was unreachable and an
    # unreachable video_compiler surfaced as a 500 instead.
    except (httpx.HTTPError, OSError) as exc:
        logger.error("Video compiler request failed for job %s: %s", job_id, exc)
        raise HTTPException(status_code=503, detail=f"Video compiler unavailable: {exc}") from exc
    finally:
        if client:
            await client.close()

    return ReviewVideoResponse(
        review_job_id=job_id,
        video_job_id=result["job_id"],
        video_poll_url=result.get("poll_url", ""),
        findings_count=len(all_findings),
    )


@router.get(
    "/review/{job_id}",
    responses={
        404: {"description": "Job not found"},
    },
    operation_id="get_review",
)
async def get_review(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ReviewResultResponse:
    """Get review job status and results.

    Args:
        job_id: The review job identifier.
        db: Async database session.

    Returns:
        ReviewResultResponse with status and results.

    Raises:
        HTTPException: 404 if job not found.
    """
    logger.debug("Fetching review: job_id=%s", job_id)
    tenant_id = _get_tenant_id(request)
    # Phase 4.1: Check cache first. The cache stays AHEAD of the DB — that is
    # the point of it — so the tenant lives in the KEY, not in a check after
    # the read. Redis has no RLS backstop the way Postgres does.
    settings = get_settings()
    if settings.cache.enabled:
        from src.domains.review.services.review_cache import get_cached_result
        from src.metrics import review_cache_hits, review_cache_misses

        cached = await get_cached_result(tenant_id, job_id)
        if cached:
            review_cache_hits.inc()
            return ReviewResultResponse(**cached)
        review_cache_misses.inc()

    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return ReviewResultResponse(
        job_id=job_id,
        status=job.status,
        results=job.results or {},
        error=job.error,
    )


@router.get(
    "/review/{job_id}/summary",
    responses={
        400: {"description": "Job not completed"},
        404: {"description": "Job not found"},
    },
    operation_id="get_review_summary",
)
async def get_review_summary(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ReviewSummary:
    """Get executive summary with aggregate score and top findings.

    Args:
        job_id: The review job identifier.
        db: Async database session.

    Returns:
        ReviewSummary with aggregate score, grade, and top findings.

    Raises:
        HTTPException: 404 if job not found, 400 if not completed.
    """
    logger.debug("Generating summary: job_id=%s", job_id)
    # Tenant-scoped lookup FIRST: the "not completed" 400 below is a status
    # disclosure, so it must be unreachable for a job the caller does not own.
    job = await JobManager.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != "completed" or not job.results:
        raise HTTPException(
            status_code=400,
            detail=f"Job is {job.status}, summary requires completed results",
        )

    settings = get_settings()
    summary = generate_summary(
        job_id=job_id,
        results=job.results,
        aggregate_weights=settings.scoring.aggregate_weights,
    )
    return ReviewSummary(**summary)


# Phase 3.1: Findings endpoint
@router.get(
    "/review/{job_id}/findings",
    responses={
        400: {"description": "Job not completed"},
        404: {"description": "Job not found"},
    },
    operation_id="get_review_findings",
)
async def get_review_findings(
    job_id: str,
    request: Request,
    review_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> FindingsListResponse:
    """Get paginated findings from a completed review.

    Args:
        job_id: The review job identifier.
        review_type: Optional filter by review type.
        severity: Optional filter by severity level.
        page: Page number (1-indexed).
        per_page: Results per page (1-100).
        db: Async database session.

    Returns:
        FindingsListResponse with paginated findings.

    Raises:
        HTTPException: 404 if job not found, 400 if not completed.
    """
    logger.debug(
        "Fetching findings: job_id=%s, review_type=%s, severity=%s, page=%d",
        job_id,
        review_type,
        severity,
        page,
    )
    job = await JobManager.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or not job.results:
        raise HTTPException(status_code=400, detail="Job not completed")

    from src.domains.review.services.findings import extract_findings

    all_findings = extract_findings(job.results)

    # Apply filters
    if review_type:
        all_findings = [f for f in all_findings if f["review_type"] == review_type]
    if severity:
        all_findings = [f for f in all_findings if f["severity"] == severity]

    total = len(all_findings)
    start = (page - 1) * per_page
    paginated = all_findings[start : start + per_page]

    return FindingsListResponse(
        job_id=job_id,
        findings=[ReviewFinding(**f) for f in paginated],
        total=total,
        page=page,
        per_page=per_page,
    )


# Phase 3.3: Report endpoint
@router.get(
    "/review/{job_id}/report",
    response_model=None,
    responses={
        400: {"description": "Job not completed"},
        404: {"description": "Job not found"},
        501: {"description": "PDF generation not available"},
    },
    operation_id="get_review_report",
)
async def get_review_report(
    job_id: str,
    request: Request,
    fmt: str = Query(
        default="markdown", alias="format", description="Report format: markdown, html, json, pdf"
    ),
    db: AsyncSession = Depends(get_db),
) -> ReviewReportResponse | HTMLResponse | JSONResponse | Response:
    """Get review report in specified format.

    Args:
        job_id: The review job identifier.
        fmt: Report format -- markdown, html, json, or pdf.
        db: Async database session.

    Returns:
        Report in the requested format.

    Raises:
        HTTPException: 404 if job not found, 400 if not completed,
            501 if PDF generation unavailable.
    """
    logger.info("Generating report: job_id=%s, format=%s", job_id, fmt)

    job = await JobManager.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "completed" or not job.results:
        raise HTTPException(status_code=400, detail="Job not completed")

    settings = get_settings()
    summary = generate_summary(
        job_id=job_id,
        results=job.results,
        aggregate_weights=settings.scoring.aggregate_weights,
    )

    if fmt == "json":
        return JSONResponse(content=summary)

    if fmt == "html":
        from src.domains.review.services.html_report_generator import generate_html_report

        html = generate_html_report(job_id, job.results, summary)
        return HTMLResponse(content=html)

    if fmt == "pdf":
        from src.domains.review.services.pdf_report_generator import generate_pdf_report

        try:
            pdf_bytes = generate_pdf_report(job_id, job.results, summary)
            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": f'attachment; filename="review_{job_id}.pdf"'},
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=501, detail="PDF generation not available (weasyprint not installed)"
            ) from exc

    # Default: markdown
    from src.domains.review.services.report_generator import generate_markdown_report

    report = generate_markdown_report(job_id, job.results, summary)
    return ReviewReportResponse(job_id=job_id, format="markdown", report=report)


@router.get(
    "/review/{job_id}/progress",
    responses={
        404: {"description": "Job not found"},
        501: {"description": "SSE progress not enabled"},
    },
    operation_id="get_review_progress",
)
async def get_review_progress(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE endpoint for real-time review progress.

    Sends events until job completes or fails. Requires feature flag.
    """
    settings = get_settings()
    if not settings.feature_flags.sse_progress:
        raise HTTPException(status_code=501, detail="SSE progress not enabled")

    # Captured HERE, inside the request scope. ``event_stream`` below runs on a
    # fresh session AFTER the response has started, so it must close over a
    # plain local — never reach into ``request.state`` from in there.
    tenant_id = _get_tenant_id(request)

    job = await JobManager.get_job(db, job_id, tenant_id=tenant_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    review_types = job.review_types or []
    total_types = len(review_types)

    async def event_stream() -> AsyncIterator[str]:
        while True:
            async with get_db_session() as session:
                current_job = await JobManager.get_job(session, job_id, tenant_id=tenant_id)
                if not current_job:
                    break

                completed_types = []
                if current_job.results:
                    completed_types = list(current_job.results.keys())

                progress_pct = (
                    round(len(completed_types) / total_types * 100) if total_types > 0 else 0
                )

                event_data = json.dumps(
                    {
                        "status": current_job.status,
                        "completed_types": completed_types,
                        "total_types": total_types,
                        "progress_pct": progress_pct,
                    }
                )
                yield f"data: {event_data}\n\n"

                if current_job.status in ("completed", "failed"):
                    break

            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/review", operation_id="list_reviews")
async def list_reviews(
    request: Request,
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
    offset: int = 0,
) -> ReviewListResponse:
    """List review jobs (paginated).

    Args:
        request: FastAPI request for tenant context.
        db: Async database session.
        limit: Maximum number of results.
        offset: Number of results to skip.

    Returns:
        ReviewListResponse with paginated job list.
    """
    logger.debug("Listing reviews: limit=%d, offset=%d", limit, offset)
    # ``_get_tenant_id`` like every other route here, NOT a bare ``None``: an
    # unresolved tenant used to fall through ``list_jobs``' ``if tenant_id`` and
    # list every tenant's jobs. It now scopes to the "anonymous" tenant, which
    # owns no rows — a caller with no tenant gets an empty page, not the fleet.
    tenant_id = _get_tenant_id(request)
    jobs, total = await JobManager.list_jobs(db, tenant_id=tenant_id, limit=limit, offset=offset)

    return ReviewListResponse(
        jobs=[
            ReviewJobResponse(
                job_id=j.job_id,
                status=j.status,
                review_types=j.review_types or [],
                created_at=j.created_at.isoformat() if j.created_at else None,
            )
            for j in jobs
        ],
        total=total,
    )


# ============================================================================
# Phase 4.3: Usage Endpoint
# ============================================================================


@router.get("/usage", operation_id="get_review_usage")
async def get_usage(
    request: Request,
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> UsageListResponse:
    """Get usage statistics for the authenticated tenant.

    Args:
        request: FastAPI request for tenant context.
        start_date: Optional start date filter (YYYY-MM-DD).
        end_date: Optional end date filter (YYYY-MM-DD).
        db: Async database session.

    Returns:
        UsageListResponse with daily usage records.
    """
    logger.debug("Fetching usage: start_date=%s, end_date=%s", start_date, end_date)
    tenant_id = getattr(request.state, "tenant_id", "anonymous")

    parsed_start = None
    parsed_end = None
    if start_date:
        parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()

    records = await JobManager.get_usage(
        db, tenant_id=tenant_id, start_date=parsed_start, end_date=parsed_end
    )

    return UsageListResponse(
        usage=[
            UsageResponse(
                tenant_id=r.tenant_id,
                date=r.date.isoformat(),
                review_count=r.review_count,
                error_count=r.error_count,
            )
            for r in records
        ],
        total=len(records),
    )


# ============================================================================
# Phase 4.4: Domain History Endpoint
# ============================================================================


@router.get("/domain/{domain}/history", operation_id="get_review_domain_history")
async def get_domain_history(
    domain: str,
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> DomainHistoryResponse:
    """Get review history for a domain.

    Args:
        domain: The domain name to look up.
        request: FastAPI request for tenant context.
        limit: Maximum number of history entries (1-50).
        db: Async database session.

    Returns:
        DomainHistoryResponse with score trend analysis.
    """
    logger.debug("Fetching domain history: domain=%s, limit=%d", domain, limit)
    # Same as ``list_reviews``: an unresolved tenant must see nothing, not every
    # tenant's review history for this domain.
    tenant_id = _get_tenant_id(request)
    jobs = await JobManager.get_domain_history(db, domain=domain, tenant_id=tenant_id, limit=limit)

    settings = get_settings()
    from src.domains.review.services.scoring import score_to_grade

    reviews: list[DomainHistoryEntry] = []
    for j in jobs:
        agg_score = None
        grade = None
        if j.results:
            summary = generate_summary(
                job_id=j.job_id,
                results=j.results,
                aggregate_weights=settings.scoring.aggregate_weights,
            )
            agg_score = summary.get("aggregate_score")
            if agg_score is not None:
                grade = score_to_grade(agg_score)

        reviews.append(
            DomainHistoryEntry(
                job_id=j.job_id,
                aggregate_score=agg_score,
                grade=grade,
                review_types=j.review_types or [],
                created_at=j.created_at.isoformat() if j.created_at else None,
            )
        )

    # Compute trend
    trend = None
    scores = [r.aggregate_score for r in reviews if r.aggregate_score is not None]
    if len(scores) >= 2:
        if scores[0] > scores[-1]:
            trend = "improving"
        elif scores[0] < scores[-1]:
            trend = "declining"
        else:
            trend = "stable"

    return DomainHistoryResponse(
        domain=domain,
        reviews=reviews,
        total=len(reviews),
        trend=trend,
    )
