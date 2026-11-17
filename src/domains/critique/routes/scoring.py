"""Scoring weights and runtime config-override endpoints (WS-9/10)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from src.domains.critique.routes._helpers import require_operator
from src.domains.critique.schemas import (
    ConfigOverridesResponse,
    ScoringWeightsResponse,
    ScoringWeightsUpdateRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/scoring/weights", operation_id="get_critique_scoring_weights")
async def get_scoring_weights_endpoint(
    request: Request, _: None = Depends(require_operator)
) -> ScoringWeightsResponse:
    """Get current scoring weights (static + runtime overrides). Operator-only."""
    from src.domains.critique.services.runtime_config import get_scoring_weights

    weights = await get_scoring_weights()
    return ScoringWeightsResponse(weights=weights)


@router.put(
    "/scoring/weights",
    responses={
        422: {"description": "Validation error"},
    },
    operation_id="update_critique_scoring_weights",
)
async def update_scoring_weights(
    body: ScoringWeightsUpdateRequest,
    request: Request,
    _: None = Depends(require_operator),
) -> ScoringWeightsResponse:
    """Update scoring weights at runtime (stored in Redis). Operator-only."""
    from src.domains.critique.services.runtime_config import get_scoring_weights, set_runtime_value

    logger.info("Scoring weights update requested: keys=%s", list(body.weights.keys()))
    for key, value in body.weights.items():
        await set_runtime_value("scoring", key, str(value))
    weights = await get_scoring_weights()
    logger.info("Scoring weights updated")
    return ScoringWeightsResponse(weights=weights)


@router.get("/config/overrides", operation_id="get_critique_config_overrides")
async def get_config_overrides(
    request: Request, _: None = Depends(require_operator)
) -> ConfigOverridesResponse:
    """Get all runtime config overrides. Operator-only."""
    from src.domains.critique.services.runtime_config import get_all_overrides

    overrides = await get_all_overrides()
    return ConfigOverridesResponse(overrides=overrides)


@router.delete(
    "/config/overrides/{section}/{key}",
    status_code=204,
    responses={
        404: {"description": "Override not found"},
    },
    operation_id="delete_critique_config_override",
)
async def delete_config_override(
    section: str,
    key: str,
    request: Request,
    _: None = Depends(require_operator),
) -> None:
    """Delete a runtime config override. Operator-only."""
    from src.domains.critique.services.runtime_config import delete_runtime_value

    deleted = await delete_runtime_value(section, key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Override not found")
