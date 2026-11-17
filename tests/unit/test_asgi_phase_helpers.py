"""Phase-helper tests for the ``lifespan`` decomposition in ``src.asgi``.

The original 36-complexity ``lifespan`` was split into per-subsystem
``_init_*`` helpers. These tests assert real behavior of the helpers we can
exercise without booting the full app.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI

import src.asgi as asgi


def _make_app() -> FastAPI:
    """Minimal stand-in for ``FastAPI`` exposing ``state`` like the real app."""
    # Cast a SimpleNamespace into FastAPI for type checking — the helpers
    # only touch ``app.state``, which SimpleNamespace mimics faithfully.
    return cast(FastAPI, SimpleNamespace(state=SimpleNamespace()))


# ---------------------------------------------------------------------------
# _build_brand_pipeline_runner: deterministic, no I/O
# ---------------------------------------------------------------------------


class TestBuildBrandPipelineRunner:
    def test_runner_has_twelve_stages(self) -> None:
        runner = asgi._build_brand_pipeline_runner()
        assert len(runner._stages) == 12

    def test_runner_stages_in_documented_order(self) -> None:
        runner = asgi._build_brand_pipeline_runner()
        stage_classes = [type(s).__name__ for s in runner._stages]
        # Order documented in the lifespan comments
        assert stage_classes[0] == "FetchStage"
        assert stage_classes[1] == "NameStage"
        assert stage_classes[2] == "LogoStage"
        assert "QualityStage" in stage_classes
        # quality is documented as the last gate
        assert stage_classes[-1] == "QualityStage"


# ---------------------------------------------------------------------------
# _init_analyze_domain: pure synchronous setup of TTLCaches
# ---------------------------------------------------------------------------


class TestInitAnalyzeDomain:
    @pytest.mark.asyncio
    async def test_attaches_analyzer_service_and_caches_to_state(self) -> None:
        app = _make_app()
        bg_tasks: list[asyncio.Task[Any]] = []
        asgi._init_analyze_domain(app, bg_tasks)
        assert hasattr(app.state, "analyzer_service")
        assert hasattr(app.state, "analysis_jobs")
        assert hasattr(app.state, "review_jobs")
        # cancellation_tokens is the dict initialized in this phase.
        assert app.state.cancellation_tokens == {}

        # Migration 0015 gave analysis_jobs a reaper; the phase must actually
        # register it, or the resolver ships and nothing ever calls it.
        assert len(bg_tasks) >= 1
        for task in bg_tasks:
            task.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# _init_critique_domain: tolerates missing redis, attaches service
# ---------------------------------------------------------------------------


class TestInitCritiqueDomain:
    @pytest.mark.asyncio
    async def test_no_redis_still_initializes_service(self) -> None:
        app = _make_app()
        app.state.redis_client = None
        bg_tasks: list[asyncio.Task[Any]] = []
        asgi._init_critique_domain(app, bg_tasks)
        # Service must be attached even when redis is unavailable.
        assert hasattr(app.state, "critique_service")
        # Cancel any scheduler/retention tasks the helper may have spawned.
        for t in bg_tasks:
            t.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# _init_review_domain: starts cleanup background task
# ---------------------------------------------------------------------------


class TestInitReviewDomain:
    @pytest.mark.asyncio
    async def test_attaches_review_service_and_creates_cleanup_task(self) -> None:
        app = _make_app()
        bg_tasks: list[asyncio.Task[Any]] = []
        asgi._init_review_domain(app, bg_tasks)
        assert hasattr(app.state, "review_service")
        # Cleanup task should have been registered.
        assert len(bg_tasks) >= 1
        # Cancel the long-running task so we don't leak.
        for t in bg_tasks:
            t.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
