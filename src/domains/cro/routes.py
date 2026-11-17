"""API route for synchronous CRO (conversion) scoring.

``POST /api/cro/score`` — the **authenticated** one-shot conversion scorer. Auth
is enforced by the shared ``AuthMiddleware`` (any ``/api/*`` path that is not
health/docs requires ``X-Tenant-Id: Bearer <license>``); this route is
deliberately NOT exempt.

Fail-closed: any fetch/scan failure raises ``502`` — a fabricated score is never
returned.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from w2v_common.auth import LICENSE_GATE

from src.domains.cro.schemas import CroScoreRequest, CroScoreResponse
from src.domains.cro.service import score_cro
from src.domains.scorecards.errors import ScorecardError
from src.domains.scorecards.http import scorecard_responses, tenant_id_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["cro"], dependencies=list(LICENSE_GATE))


@router.post(
    "/cro/score",
    response_model=CroScoreResponse,
    response_model_exclude_unset=True,
    responses=scorecard_responses(),
    operation_id="score_cro",
)
async def score_cro_route(body: CroScoreRequest, request: Request) -> CroScoreResponse:
    """Score a rendered page (URL or raw HTML) for conversion optimization.

    Runs the deterministic CRO scanner (no LLM, no DB) and returns a 0-100 score,
    an A-F grade, per-sub-section breakdown, and quick-win / strategic buckets.
    """
    tenant_id = tenant_id_from_request(request)
    logger.info(
        "cro-score tenant=%s source=%s include_llm=%s",
        tenant_id,
        body.url or "inline-html",
        body.include_llm,
    )
    try:
        result = await score_cro(
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
            wm_client=getattr(request.app.state, "wm_client", None),
        )
    except ScorecardError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return CroScoreResponse(**result)
