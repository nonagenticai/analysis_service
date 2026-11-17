"""API route for the synchronous SEO discoverability audit.

``POST /api/seo/audit`` — the **authenticated** one-shot SEO scorer. Auth is
enforced by the shared ``AuthMiddleware`` (any ``/api/*`` path that is not
health/docs requires ``X-Tenant-Id: Bearer <license>``); this route is
deliberately NOT exempt.

Fail-closed: any fetch/scan failure raises ``502`` — a fabricated score is never
returned.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from w2v_common.auth import LICENSE_GATE

from src.domains.scorecards.errors import ScorecardError
from src.domains.scorecards.http import scorecard_responses, tenant_id_from_request
from src.domains.seo_audit.schemas import SeoAuditRequest, SeoAuditResponse
from src.domains.seo_audit.service import audit_seo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["seo_audit"], dependencies=list(LICENSE_GATE))


@router.post(
    "/seo/audit",
    response_model=SeoAuditResponse,
    response_model_exclude_unset=True,
    responses=scorecard_responses(),
    operation_id="audit_seo",
)
async def audit_seo_route(body: SeoAuditRequest, request: Request) -> SeoAuditResponse:
    """Audit a rendered page (URL or raw HTML) for SEO discoverability.

    Runs the deterministic SEO audit scanner and returns a 0-100 SEO Health
    Score, an A-F grade, per-group breakdown, and quick-win / strategic buckets.
    With ``include_llm``, opt-in micro-LLM analyzers (search intent, E-E-A-T
    depth, competitor content gap) add ``ai.annotations[]`` without moving the
    score; their per-layer status is reported in ``ai``.
    """
    tenant_id = tenant_id_from_request(request)
    logger.info(
        "seo-audit tenant=%s source=%s include_llm=%s",
        tenant_id,
        body.url or "inline-html",
        body.include_llm,
    )
    needs_wm = (
        body.include_llm
        or body.include_recommendations
        or body.include_grounding
        or body.include_exec_summary
        or body.include_critic
    )
    wm_client = getattr(request.app.state, "wm_client", None) if needs_wm else None
    try:
        result = await audit_seo(
            html=body.html,
            url=body.url,
            include_llm=body.include_llm,
            include_priority=body.include_priority,
            include_projection=body.include_projection,
            include_evidence=body.include_evidence,
            include_unbucketed=body.include_unbucketed,
            include_recommendations=body.include_recommendations,
            include_grounding=body.include_grounding,
            include_exec_summary=body.include_exec_summary,
            include_critic=body.include_critic,
            bucket_depth_cap=body.bucket_depth_cap,
            intent=body.intent,
            sources=body.sources,
            wm_client=wm_client,
            competitor_urls=body.competitor_urls,
            cross_page_linking=body.cross_page_linking,
            max_pages=body.max_pages,
        )
    except ScorecardError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return SeoAuditResponse(**result)
