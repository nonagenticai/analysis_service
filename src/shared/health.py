"""Health check endpoints for analysis_service."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def _check_database(request: Request) -> dict[str, Any]:
    """Check database connectivity with latency measurement."""
    start = time.monotonic()
    try:
        engine = getattr(request.app.state, "db_engine", None)
        if engine is None:
            return {"status": "not configured"}
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"status": "healthy", "latency_ms": latency_ms}
    except (OSError, ConnectionError, TimeoutError, RuntimeError, SQLAlchemyError) as e:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"status": "unhealthy", "error": str(e), "latency_ms": latency_ms}


async def _check_redis(request: Request) -> dict[str, Any]:
    """Check Redis connectivity, distinguishing "absent" from "broken".

    Three distinct states, never collapsed into one:

    * **not configured** — no ``REDIS_URL`` and no client object. A deliberately
      cacheless deployment; folded into overall *healthy*.
    * **healthy** — configured AND a real ``ping()`` answered.
    * **unhealthy** — configured but not answering (startup connect failed and
      left ``app.state.redis_client`` as ``None``, the client was disconnected,
      or ``ping()`` raised).

    The old code returned "not configured" for a client whose ``is_available``
    had flipped to ``False``, and never attempted ``ping()`` — a dead cache was
    indistinguishable from no cache and was reported as healthy 200.
    """
    start = time.monotonic()
    redis_client = getattr(request.app.state, "redis_client", None)
    # A client object on app.state means redis was configured, even if it is now
    # dead. REDIS_URL covers the case where startup failed and left state None.
    configured = redis_client is not None or bool(os.getenv("REDIS_URL"))

    if not configured:
        return {"status": "not configured"}

    if redis_client is None:
        return {
            "status": "unhealthy",
            "error": "REDIS_URL is configured but no Redis client was initialized",
        }

    if not getattr(redis_client, "is_available", False):
        return {
            "status": "unhealthy",
            "error": "Redis client is configured but not connected",
        }

    try:
        await redis_client.ping()
    except (OSError, ConnectionError, TimeoutError, RuntimeError, RedisError) as e:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {"status": "unhealthy", "error": str(e), "latency_ms": latency_ms}

    latency_ms = round((time.monotonic() - start) * 1000, 1)
    return {"status": "healthy", "latency_ms": latency_ms}


def _check_llm() -> dict[str, Any]:
    """Report whether an LLM API key is PRESENT — not whether it works.

    This only reads an environment variable. A revoked, expired or typo'd key is
    a non-empty string, so the old "configured" verdict over-claimed: it read as
    "the LLM is usable" when all that is known is "a string exists". The state is
    named ``key present`` and carries ``verified: False`` so no consumer can
    mistake it for a liveness signal. Verifying would mean a billable upstream
    call on every probe, which a health endpoint must not do.
    """
    api_key = os.getenv("LLM_API_KEY", "")
    if api_key:
        return {"status": "key present", "verified": False}
    return {"status": "not configured"}


def _check_storage() -> dict[str, Any]:
    """Verify the configured local storage path exists and is writable."""
    try:
        from src.config import get_settings

        storage_path = Path(get_settings().storage.local_path)
        path_str = str(storage_path)
        if not storage_path.exists():
            return {"status": "path not found", "path": path_str}
        if not os.access(path_str, os.W_OK):
            return {"status": "not writable", "path": path_str}
        return {"status": "healthy", "path": path_str}
    except (OSError, PermissionError) as e:
        return {"status": "unhealthy", "error": str(e)}


@router.get("/health", response_model=None, operation_id="health_check")
async def health_check(request: Request) -> dict[str, Any] | JSONResponse:
    """Combined health check for all components with per-component latency."""
    try:
        db = await _check_database(request)
        redis = await _check_redis(request)
        llm = _check_llm()
        storage = _check_storage()

        components = {
            "database": db,
            "redis": redis,
            "llm": llm,
            "storage": storage,
            "api": {"status": "healthy"},
        }

        # Critical components: database must be healthy.
        #
        # ``llm`` is deliberately EXCLUDED from `overall`: _check_llm() only
        # observes that an env var is non-empty, so it can neither confirm the
        # LLM works nor prove it is broken. Folding an unverified signal in would
        # flip `overall` on evidence that does not exist in either direction. It
        # stays reported under `components` for operators, and nothing else.
        db_healthy = db["status"] == "healthy"
        # A configured-but-dead redis now reports "unhealthy" (not "not
        # configured") and therefore lands in `degraded` below, instead of being
        # laundered into healthy 200.
        redis_healthy = redis["status"] in ("healthy", "not configured")
        storage_healthy = storage["status"] == "healthy"

        if db_healthy and redis_healthy and storage_healthy:
            overall = "healthy"
            status_code = 200
        elif db_healthy:
            overall = "degraded"
            status_code = 200
        else:
            overall = "unhealthy"
            status_code = 503

        body = {
            "status": overall,
            "components": components,
        }

        if status_code == 503:
            return JSONResponse(status_code=503, content=body)
        return body
    except (OSError, ConnectionError, TimeoutError, RuntimeError, SQLAlchemyError) as e:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "error": str(e)},
        )


@router.get("/health/live", response_model=None, operation_id="health_live")
async def health_live() -> dict[str, str]:
    """Kubernetes liveness probe."""
    return {"status": "ok"}


@router.get("/healthz", operation_id="health.live")
async def healthz() -> dict[str, str]:
    """Modern K8s liveness probe path (alias of /health/live)."""
    return {"status": "ok"}


@router.get("/health/ready", response_model=None, operation_id="health_ready")
async def health_ready(request: Request) -> dict[str, Any] | JSONResponse:
    """Kubernetes readiness probe -- checks database connectivity."""
    try:
        engine = getattr(request.app.state, "db_engine", None)
        if engine is None:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "ready": False,
                    "checks": {"database": {"status": "not configured"}},
                },
            )
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "healthy", "ready": True, "checks": {"database": {"status": "up"}}}
    except (OSError, ConnectionError, TimeoutError, RuntimeError, SQLAlchemyError) as e:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "ready": False,
                "checks": {"database": {"status": "down", "error": str(e)}},
            },
        )


@router.get("/readyz", response_model=None, operation_id="health.ready")
async def readyz(request: Request) -> dict[str, Any] | JSONResponse:
    """Modern K8s readiness probe path (alias of /health/ready)."""
    return await health_ready(request)


@router.get("/health/startup", response_model=None, operation_id="health_startup")
async def health_startup(request: Request) -> dict[str, Any] | JSONResponse:
    """Kubernetes startup probe -- verifies all components initialized."""
    try:
        db = await _check_database(request)

        checks = {"database": db, "config_loaded": {"status": "healthy"}}

        if db["status"] != "healthy":
            return JSONResponse(
                status_code=503,
                content={"started": False, "checks": checks},
            )

        return {"started": True, "checks": checks}
    except (OSError, ConnectionError, TimeoutError, RuntimeError, SQLAlchemyError) as e:
        return JSONResponse(
            status_code=503,
            content={"started": False, "error": str(e)},
        )


@router.get("/metrics", response_model=None, operation_id="prometheus_metrics")
async def prometheus_metrics() -> PlainTextResponse:
    """Prometheus metrics endpoint.

    Attempts to render metrics from the metrics service; returns an empty
    body when the renderer is not available.
    """
    try:
        from prometheus_client import generate_latest

        def render_prometheus() -> str:
            return generate_latest().decode("utf-8")

        return PlainTextResponse(content=render_prometheus(), media_type="text/plain")
    except ImportError:
        return PlainTextResponse(content="", media_type="text/plain")
