"""Brand management CRUD, override, export, quality, and comparison routes."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from w2v_common.auth import LICENSE_GATE

from src.domains.brand.schemas import (
    BrandComparisonRequest,
    BrandComparisonResponse,
    BrandExportResponse,
    BrandHistoryEntry,
    BrandHistoryResponse,
    BrandListResponse,
    BrandOverrideRequest,
    BrandOverrideResponse,
    BrandResponse,
    CacheInvalidationResponse,
    FieldComparison,
)
from src.domains.brand.services.brand_comparator import BrandComparator
from src.domains.brand.services.brand_override_service import BrandOverride, BrandOverrideService
from src.domains.brand.services.brand_repository import BrandRepository
from src.domains.brand.services.output_formatter import BrandOutputFormatter
from src.shared.database.session import get_db
from src.shared.tenant import require_request_tenant
from src.shared.upstream_errors import passthrough_status_error

logger = logging.getLogger(__name__)

brands_router = APIRouter(prefix="/api/brands", tags=["brands"], dependencies=list(LICENSE_GATE))


def _get_tenant_id(request: Request) -> str:
    """Get tenant ID from request state."""
    return str(require_request_tenant(request))


@brands_router.get("", operation_id="list_brands")
async def list_brands(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    db: AsyncSession = Depends(get_db),
) -> BrandListResponse:
    """List brands for the current tenant with pagination.

    Args:
        request: FastAPI request with tenant context.
        page: Page number (1-indexed).
        page_size: Number of items per page (capped at 100).
        db: Async database session.

    Returns:
        Paginated list of brand responses.
    """
    tenant_id = _get_tenant_id(request)
    page_size = min(page_size, 100)  # Cap page size
    logger.info(
        "List brands requested, tenant=%s, page=%d, page_size=%d", tenant_id, page, page_size
    )

    brands, total = await BrandRepository.list_brands(db, tenant_id, page, page_size)

    items = []
    for brand in brands:
        items.append(
            BrandResponse(
                name=brand.name or "",
                domain=brand.domain,
                logo_url=brand.logo_url or "",
                primary_color=brand.primary_color,
                secondary_color=brand.secondary_color,
                has_logo=brand.has_logo,
            )
        )

    logger.info(
        "List brands completed, tenant=%s, total=%d, returned=%d", tenant_id, total, len(items)
    )
    return BrandListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_next=(page * page_size) < total,
    )


@brands_router.get("/{domain}", operation_id="get_brand")
async def get_brand(
    domain: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandResponse:
    """Get brand for a specific domain with cache-first lookup.

    Args:
        domain: The domain to look up.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Brand information for the domain.

    Raises:
        HTTPException: 404 if brand not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Get brand requested, domain=%s, tenant=%s", domain, tenant_id)

    # Check cache first
    try:
        brand_cache = getattr(request.app.state, "brand_cache", None)
        if brand_cache:
            cached = await brand_cache.get(tenant_id, domain)
            if cached:
                logger.info("Cache hit for domain=%s", domain)
                return cached
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Cache lookup failed: %s", e)

    brand = await BrandRepository.get_brand(db, tenant_id, domain)
    if not brand:
        raise HTTPException(status_code=404, detail=f"Brand not found for domain: {domain}")

    result = BrandResponse(
        name=brand.name or "",
        domain=brand.domain,
        logo_url=brand.logo_url or "",
        primary_color=brand.primary_color,
        secondary_color=brand.secondary_color,
        has_logo=brand.has_logo,
    )

    # Cache for next time
    try:
        brand_cache = getattr(request.app.state, "brand_cache", None)
        if brand_cache:
            await brand_cache.set(tenant_id, domain, result)
    except (OSError, ValueError, RuntimeError, TimeoutError) as e:
        logger.debug("Failed to cache brand: %s", e)

    return result


@brands_router.delete("/{domain}/cache", operation_id="invalidate_brand_cache")
async def invalidate_cache(
    domain: str,
    request: Request,
) -> CacheInvalidationResponse:
    """Invalidate cached brand for a domain.

    Args:
        domain: The domain whose cache entry to invalidate.
        request: FastAPI request with tenant context.

    Returns:
        Cache invalidation result.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Cache invalidation requested, domain=%s, tenant=%s", domain, tenant_id)

    brand_cache = getattr(request.app.state, "brand_cache", None)
    if not brand_cache:
        return CacheInvalidationResponse(
            domain=domain,
            invalidated=False,
            message="Cache service not available",
        )

    invalidated = await brand_cache.invalidate(tenant_id, domain)
    return CacheInvalidationResponse(
        domain=domain,
        invalidated=invalidated,
        message="Cache invalidated" if invalidated else "No cached entry found",
    )


@brands_router.get("/{domain}/history", operation_id="get_brand_history")
async def get_brand_history(
    domain: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandHistoryResponse:
    """Get brand version history for a domain.

    Args:
        domain: The domain to get history for.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Version history with current version number and snapshots.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Brand history requested, domain=%s, tenant=%s", domain, tenant_id)

    # Get current brand
    brand = await BrandRepository.get_brand(db, tenant_id, domain)
    current_version = brand.version if brand else 0

    # Get history
    versions = await BrandRepository.get_brand_history(db, tenant_id, domain)

    history = [
        BrandHistoryEntry(
            version=v.version,
            data=v.data or {},
            changed_fields=v.changed_fields or [],
            created_at=v.created_at,
        )
        for v in versions
    ]

    return BrandHistoryResponse(
        domain=domain,
        current_version=current_version,
        history=history,
    )


@brands_router.get(
    "/{domain}/logo",
    responses={
        404: {"description": "No logo found"},
    },
    operation_id="get_brand_domain_logo",
)
async def serve_domain_logo(
    domain: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve the stored logo for a domain.

    Args:
        domain: The domain whose logo to serve.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Raw image response with appropriate content type.

    Raises:
        HTTPException: 404 if no logo found, 503 if logo service unavailable.
    """
    tenant_id = _get_tenant_id(request)
    logger.debug("Domain logo serve requested, domain=%s, tenant=%s", domain, tenant_id)
    logo_service = getattr(request.app.state, "logo_service", None)
    if not logo_service:
        raise HTTPException(status_code=503, detail="Logo service not available")

    record = await BrandRepository.get_logo_for_domain(db, tenant_id, domain)
    if not record:
        raise HTTPException(status_code=404, detail=f"No logo found for domain: {domain}")

    data = await logo_service.retrieve(record.storage_path)
    if data is None:
        raise HTTPException(status_code=404, detail="Logo file not found on disk")

    return Response(content=data, media_type=record.content_type)


# --- Round 2: New endpoints ---

_override_service = BrandOverrideService()
_comparator = BrandComparator()
_formatter = BrandOutputFormatter()


@brands_router.put("/{domain}/override", operation_id="override_brand")
async def override_brand(
    domain: str,
    body: BrandOverrideRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandOverrideResponse:
    """Apply manual brand overrides to a domain.

    Args:
        domain: The domain to override.
        body: Override request containing fields to update.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Override result with overridden fields and new version.

    Raises:
        HTTPException: 404 if brand not found, 422 on validation errors.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Brand override requested, domain=%s, tenant=%s", domain, tenant_id)

    # Build override object from request dict. ``overrides`` is a free-form dict
    # on the wire, so this splat is where its contents are first checked —
    # unguarded it raised ValidationError straight out of the handler and
    # returned a 500, contradicting the 422 this route's docstring promises.
    try:
        override = BrandOverride(**body.overrides)
    except (ValidationError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"Invalid overrides: {e}") from e

    # Validate
    errors = _override_service.validate_override(override)
    if errors:
        raise HTTPException(status_code=422, detail={"validation_errors": errors})

    # Get existing brand
    brand_record = await BrandRepository.get_brand(db, tenant_id, domain)
    if not brand_record:
        raise HTTPException(status_code=404, detail=f"Brand not found: {domain}")

    # Build BrandResponse from DB record
    original = BrandResponse(
        name=brand_record.name or "",
        domain=brand_record.domain,
        logo_url=brand_record.logo_url or "",
        primary_color=brand_record.primary_color,
        secondary_color=brand_record.secondary_color,
        has_logo=brand_record.has_logo,
        tagline=brand_record.tagline or "",
        brand_voice=brand_record.brand_voice or "",
    )

    # Apply overrides
    result = _override_service.apply_overrides(original, override)
    result.is_user_override = True

    # Diff to find overridden fields
    diff = _override_service.diff(original, result)

    # Persist
    brand_data = result.model_dump(mode="json")
    updated = await BrandRepository.upsert_brand(
        db, tenant_id, domain, brand_data, source="override"
    )

    # Publish event
    event_publisher = getattr(request.app.state, "event_publisher", None)
    if event_publisher:
        try:
            await event_publisher.publish_override(
                domain, tenant_id, list(diff.keys()), updated.version
            )
        # WORST of the three publish guards: this runs AFTER
        # ``upsert_brand`` has already committed, so an escape here returns 500
        # to a caller whose override DID land. ``RedisClient.publish`` does not
        # swallow ``RedisError``, and the old tuple named only builtins --
        # redis-py's ``TimeoutError`` is a ``RedisError``, not the builtin of
        # the same name -- so the guard was inert.
        except (RedisError, OSError, TypeError, ValueError) as e:
            logger.debug("Failed to publish override event: %s", e)

    logger.info(
        "Brand override applied, domain=%s, overridden_fields=%s, new_version=%d",
        domain,
        list(diff.keys()),
        updated.version,
    )
    return BrandOverrideResponse(
        domain=domain,
        overridden_fields=list(diff.keys()),
        new_version=updated.version,
        brand=result,
    )


@brands_router.get("/{domain}/export", operation_id="export_brand")
async def export_brand(
    domain: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandExportResponse:
    """Pipeline-optimized flat export for downstream consumers.

    Args:
        domain: The domain to export.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Flat export with CSS variables, Google Fonts URL, and social profiles.

    Raises:
        HTTPException: 404 if brand not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Brand export requested, domain=%s, tenant=%s", domain, tenant_id)

    # Check cache first
    brand_cache = getattr(request.app.state, "brand_cache", None)
    brand_response = None
    if brand_cache:
        try:
            brand_response = await brand_cache.get(tenant_id, domain)
        except OSError, ValueError, RuntimeError, TimeoutError:
            logger.debug(
                "Brand cache lookup failed for %s/%s, falling back to DB", tenant_id, domain
            )

    if not brand_response:
        brand_record = await BrandRepository.get_brand(db, tenant_id, domain)
        if not brand_record:
            raise HTTPException(status_code=404, detail=f"Brand not found: {domain}")
        brand_response = BrandResponse(
            name=brand_record.name or "",
            domain=brand_record.domain,
            logo_url=brand_record.logo_url or "",
            primary_color=brand_record.primary_color,
            secondary_color=brand_record.secondary_color,
            has_logo=brand_record.has_logo,
            tagline=brand_record.tagline or "",
            brand_voice=brand_record.brand_voice or "",
            font_family_primary=brand_record.font_family_primary or "",
            font_family_secondary=brand_record.font_family_secondary or "",
        )

    export_data = _formatter.to_export(brand_response)
    return BrandExportResponse(**export_data)


@brands_router.get("/{domain}/quality", operation_id="get_brand_quality_report")
async def get_quality_report(
    domain: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get brand quality and consistency report for a domain.

    Args:
        domain: The domain to evaluate.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Quality report with overall, identity, visual, metadata, and content scores.

    Raises:
        HTTPException: 404 if brand not found.
    """
    tenant_id = _get_tenant_id(request)
    logger.info("Quality report requested, domain=%s, tenant=%s", domain, tenant_id)

    brand_record = await BrandRepository.get_brand(db, tenant_id, domain)
    if not brand_record:
        raise HTTPException(status_code=404, detail=f"Brand not found: {domain}")

    # Return stored quality score if available
    if brand_record.quality_score:
        return {"domain": domain, **brand_record.quality_score}

    # Otherwise compute on the fly
    from src.domains.brand.services.consistency_scorer import BrandConsistencyScorer

    brand_response = BrandResponse(
        name=brand_record.name or "",
        domain=brand_record.domain,
        logo_url=brand_record.logo_url or "",
        primary_color=brand_record.primary_color,
        secondary_color=brand_record.secondary_color,
        has_logo=brand_record.has_logo,
    )
    scorer = BrandConsistencyScorer()
    report = scorer.score(brand_response)
    return {"domain": domain, **report.to_dict()}


async def _extract_and_persist(
    request: Request,
    db: AsyncSession,
    tenant_id: str,
    domain: str,
) -> Any:
    """Extract a brand from URL and persist it, returning the DB record.

    Raises:
        UpstreamStatusError: When ``worker_management`` answered with a status in
            ``PASSTHROUGH_STATUSES`` (today ``402 byo_llm_not_configured``). See
            :func:`compare_brands` for why a compare fails whole rather than
            per-brand on that one condition.
    """
    from src.domains.brand.pipeline.context import PipelineContext
    from src.domains.brand.pipeline.runner import PipelineRunner

    runner: PipelineRunner = request.app.state.pipeline_runner
    http_client = getattr(request.app.state, "http_client", None)
    logo_service = getattr(request.app.state, "logo_service", None)
    url = f"https://{domain}"
    try:
        ctx = PipelineContext(
            url=url,
            tenant_id=tenant_id,
            http_client=http_client,
            logo_service=logo_service,
        )
        result, _ = await runner.run(ctx)
        if result.domain:
            await BrandRepository.upsert_brand(
                db,
                tenant_id,
                result.domain,
                result.model_dump(mode="json"),
                source="compare_extract",
            )
            return await BrandRepository.get_brand(db, tenant_id, result.domain)
    except httpx.HTTPStatusError as e:
        # ``httpx.HTTPStatusError`` is NOT an ``OSError``/``ValueError``/
        # ``RuntimeError``/``TimeoutError`` — its MRO is
        # ``HTTPStatusError -> HTTPError -> Exception`` — so once
        # ``BaseStage.execute`` started re-raising passthrough statuses instead of
        # absorbing them, this helper had no handler for the only exception that
        # can now escape ``runner.run``, and the compare route 500'd. Widening the
        # tuple below to swallow it again would restore the 500's predecessor: a
        # 200 carrying a brand the tenant's unconfigured LLM never analysed.
        upstream = passthrough_status_error(e, upstream="worker_management")
        if upstream is not None:
            raise upstream from e
        # A non-passthrough upstream status (401/403/404/5xx) is OUR wiring or a
        # real outage; it already degrades inside the stage, and if one ever
        # reaches here it degrades the same way the tuple below does.
        logger.warning("On-demand extraction failed for %s: %s", domain, e)
    except (OSError, ValueError, RuntimeError, TimeoutError) as e:
        logger.warning("On-demand extraction failed for %s: %s", domain, e)
    return None


@brands_router.post(
    "/compare",
    name="compare_brands",
    responses={
        # Passed through from worker_management by ``_extract_and_persist``, not
        # synthesized here: the tenant has no LLM provider configured. Only
        # reachable with ``extract_if_missing=true``.
        402: {"description": "Tenant action required upstream (e.g. LLM provider not configured)"},
        404: {"description": "Brand not found"},
    },
    operation_id="compare_brands",
)
async def compare_brands(
    body: BrandComparisonRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrandComparisonResponse:
    """Compare brand identities between two domains.

    Args:
        body: Request with two domains and optional extract_if_missing flag.
        request: FastAPI request with tenant context.
        db: Async database session.

    Returns:
        Comparison result with similarity score and field-level comparisons.

    Raises:
        HTTPException: 404 if either brand not found; 402 when an on-demand
            extraction hit a caller-fixable upstream condition (see below).

    Why a passthrough 402 fails the WHOLE compare rather than being reported
    per-brand: it is not a fact about ``domain_a``, it is a fact about the
    TENANT — ``byo_llm_not_configured`` means *no* extraction in this request,
    or any other, can run the LLM stage, so a per-brand error channel would say
    the same sentence twice and still leave us unable to build the comparison.
    And there is nowhere to say it: ``BrandComparisonResponse`` requires two
    fully-populated brands and has no per-brand error field, so the only
    alternatives are the 402 or a **404 "Brand not found"** — which is a lie
    that sends the tenant looking for a missing brand instead of to Settings.
    A batch endpoint, whose contract IS per-URL partial success, resolves the
    same trade-off the other way (see ``routes.batch_extract``).
    """
    tenant_id = _get_tenant_id(request)
    logger.info(
        "Brand comparison requested, domain_a=%s, domain_b=%s, tenant=%s",
        body.domain_a,
        body.domain_b,
        tenant_id,
    )

    # Fetch both brands
    brand_a_record = await BrandRepository.get_brand(db, tenant_id, body.domain_a)
    brand_b_record = await BrandRepository.get_brand(db, tenant_id, body.domain_b)

    if not brand_a_record and body.extract_if_missing:
        brand_a_record = await _extract_and_persist(request, db, tenant_id, body.domain_a)
    if not brand_b_record and body.extract_if_missing:
        brand_b_record = await _extract_and_persist(request, db, tenant_id, body.domain_b)

    if not brand_a_record:
        raise HTTPException(status_code=404, detail=f"Brand not found: {body.domain_a}")
    if not brand_b_record:
        raise HTTPException(status_code=404, detail=f"Brand not found: {body.domain_b}")

    brand_a = BrandResponse(
        name=brand_a_record.name or "",
        domain=brand_a_record.domain,
        logo_url=brand_a_record.logo_url or "",
        primary_color=brand_a_record.primary_color,
        secondary_color=brand_a_record.secondary_color,
        has_logo=brand_a_record.has_logo,
        tagline=brand_a_record.tagline or "",
        brand_voice=brand_a_record.brand_voice or "",
    )
    brand_b = BrandResponse(
        name=brand_b_record.name or "",
        domain=brand_b_record.domain,
        logo_url=brand_b_record.logo_url or "",
        primary_color=brand_b_record.primary_color,
        secondary_color=brand_b_record.secondary_color,
        has_logo=brand_b_record.has_logo,
        tagline=brand_b_record.tagline or "",
        brand_voice=brand_b_record.brand_voice or "",
    )

    result = _comparator.compare(brand_a, brand_b)

    return BrandComparisonResponse(
        domain_a=body.domain_a,
        domain_b=body.domain_b,
        brand_a=brand_a,
        brand_b=brand_b,
        overall_similarity=result["overall_similarity"],
        field_comparisons=[FieldComparison(**fc) for fc in result["field_comparisons"]],
        color_distance=result["color_distance"],
    )
