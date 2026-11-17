"""API route for synchronous artifact quality scoring.

``POST /api/brand/score-artifact`` — the **authenticated** one-shot scorer
consumed by Merqi's Branding Coach as supporting evidence on a rendered
artifact. Auth is enforced by the shared ``AuthMiddleware`` (any ``/api/*``
path that is not ``/api/health*`` requires ``X-Tenant-Id: Bearer <license>``);
this route is deliberately NOT exempt.

Fail-closed: any fetch/scan failure raises ``502`` — a fabricated score is
never returned.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from w2v_common.auth import LICENSE_GATE

from src.domains.artifact_score.schemas import (
    ArtifactScoreRequest,
    ArtifactScoreResponse,
    ScoreDeltaRequest,
    ScoreDeltaResponse,
)
from src.domains.artifact_score.service import (
    resolve_categories,
    score_artifact,
    score_delta,
)
from src.domains.scorecards.errors import ScorecardError
from src.domains.scorecards.http import scorecard_responses, tenant_id_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["artifact-score"], dependencies=list(LICENSE_GATE))


@router.post(
    "/brand/score-artifact",
    response_model=ArtifactScoreResponse,
    response_model_exclude_unset=True,
    responses=scorecard_responses("Failed to fetch or scan the artifact"),
    operation_id="score_artifact",
)
async def score_artifact_route(
    body: ArtifactScoreRequest, request: Request
) -> ArtifactScoreResponse:
    """Score a rendered artifact (URL or raw HTML) and return a deterministic score.

    Runs only the static SEO/accessibility/performance scanners — no LLM, no DB.
    """
    tenant_id = tenant_id_from_request(request)
    categories = resolve_categories(body.categories)
    if not categories:
        raise HTTPException(
            status_code=422,
            detail="no supported categories requested (allowed: seo, accessibility, performance)",
        )
    logger.info(
        "score-artifact tenant=%s source=%s categories=%s",
        tenant_id,
        body.url or "inline-html",
        categories,
    )
    try:
        result = await score_artifact(html=body.html, url=body.url, categories=categories)
    except ScorecardError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return ArtifactScoreResponse(**result)


@router.post(
    "/brand/score-delta",
    response_model=ScoreDeltaResponse,
    response_model_exclude_unset=True,
    responses=scorecard_responses("Failed to fetch or scan either artifact"),
    operation_id="score_delta",
)
async def score_delta_route(body: ScoreDeltaRequest, request: Request) -> ScoreDeltaResponse:
    """Score a before/after pair of artifacts and return the quality delta.

    Scores both sides with the same static scanners (no LLM, no DB), then
    returns ``after - before`` category/overall deltas plus the new/resolved
    finding rollups. Fail-closed: if either side fails, the whole call is 502.
    """
    tenant_id = tenant_id_from_request(request)
    categories = resolve_categories(body.categories)
    if not categories:
        raise HTTPException(
            status_code=422,
            detail="no supported categories requested (allowed: seo, accessibility, performance)",
        )
    logger.info(
        "score-delta tenant=%s before=%s after=%s categories=%s",
        tenant_id,
        body.before.url or "inline-html",
        body.after.url or "inline-html",
        categories,
    )
    try:
        result = await score_delta(
            before=body.before.model_dump(),
            after=body.after.model_dump(),
            categories=categories,
        )
    except ScorecardError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return ScoreDeltaResponse(**result)
