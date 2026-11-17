"""The SSE progress stream must not pin a database session for its lifetime.

``stream_critique_progress`` took ``db: AsyncSession = Depends(get_db)``.
FastAPI closes a yield-dependency only *after* the response completes, and for a
``StreamingResponse`` "complete" means the generator has finished — which for
this route means the job reached a terminal status. The session therefore stayed
open, inside the ``SET LOCAL`` transaction the RLS binding opens, for the entire
stream. Through pgbouncer that is one pooled server connection stuck ``idle in
transaction`` for as long as a browser leaves the tab open.

This is a known fleet defect class (``SessionDep`` across a
``StreamingResponse``), and it is famously easy to "measure" wrongly: sampling
``pg_stat_activity`` at rest shows zero both before and after. The assertion
below is taken from *inside* the stream instead — every poll records how many
sessions are open at that instant, and the property is that the answer is always
zero. It is deliberately written against the observable property (no session
held) rather than against any particular fix, so it cannot be satisfied by
swapping one session-acquiring mechanism for another.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.responses import Response

from src.domains.critique.routes import router as critique_router
from src.shared.database.session import get_db

pytestmark = pytest.mark.unit

_TENANT = "3d819af4-7901-56b2-b48a-67c489ca9a02"
_JOB_ID = "job-sse-1"


class _SessionLedger:
    """Counts DB sessions that are currently open, however they were acquired."""

    def __init__(self) -> None:
        self.open = 0
        self.acquired = 0

    def acquire(self) -> None:
        self.open += 1
        self.acquired += 1

    def release(self) -> None:
        self.open -= 1


def _make_app(ledger: _SessionLedger, job: object | None) -> FastAPI:
    app = FastAPI(title="critique-sse-session-test")

    async def override_db() -> AsyncIterator[AsyncMock]:
        ledger.acquire()
        try:
            yield AsyncMock()
        finally:
            ledger.release()

    app.dependency_overrides[get_db] = override_db

    service = MagicMock()
    service.get_job = AsyncMock(return_value=job)
    app.state.critique_service = service

    @app.middleware("http")
    async def _inject_tenant(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.tenant_id = _TENANT
        return await call_next(request)

    app.include_router(critique_router)
    return app


@asynccontextmanager
async def _tracked_session(ledger: _SessionLedger) -> AsyncIterator[AsyncMock]:
    """Stand-in for ``get_db_session()`` that reports into the same ledger.

    Patched at the *route module* binding: ``jobs.py`` imports the name at module
    load, so patching the source module would silently do nothing and leave the
    ledger blind to the preflight session.
    """
    ledger.acquire()
    try:
        yield AsyncMock()
    finally:
        ledger.release()


class _ProgressRedis:
    """Redis double that records how many sessions are open at each poll."""

    def __init__(self, ledger: _SessionLedger, terminal_after: int = 2) -> None:
        self._ledger = ledger
        self._terminal_after = terminal_after
        self.polls = 0
        self.open_sessions_at_poll: list[int] = []

    async def get(self, _key: str) -> str:
        self.polls += 1
        self.open_sessions_at_poll.append(self._ledger.open)
        status = "completed" if self.polls >= self._terminal_after else "running"
        return json.dumps({"status": status, "percent": self.polls * 50})


def _job() -> SimpleNamespace:
    return SimpleNamespace(
        job_id=_JOB_ID,
        tenant_id=_TENANT,
        status="processing",
        url="https://example.com/",
        created_at=datetime.now(UTC),
    )


async def _stream(app: FastAPI) -> str:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/critique/{_JOB_ID}/progress")
        assert response.status_code == 200, response.text
        return response.text


class TestSSEHoldsNoSession:
    async def test_no_db_session_is_open_while_the_stream_produces(self) -> None:
        ledger = _SessionLedger()
        redis = _ProgressRedis(ledger)
        app = _make_app(ledger, _job())

        with (
            patch("src.config.get_settings") as settings,
            patch("src.shared.cache.redis_client.get_redis_client", return_value=redis),
            patch(
                "src.domains.critique.routes.jobs.get_db_session",
                lambda: _tracked_session(ledger),
            ),
        ):
            settings.return_value.features.sse_progress = True
            body = await _stream(app)

        assert redis.polls >= 1, "the stream never polled — the assertion below would be vacuous"
        assert "completed" in body, "the stream did not run to its terminal event"
        assert redis.open_sessions_at_poll and all(n == 0 for n in redis.open_sessions_at_poll), (
            "a DB session was held open across the SSE stream "
            f"(open sessions per poll: {redis.open_sessions_at_poll})"
        )

    async def test_every_acquired_session_is_released(self) -> None:
        ledger = _SessionLedger()
        redis = _ProgressRedis(ledger)
        app = _make_app(ledger, _job())

        with (
            patch("src.config.get_settings") as settings,
            patch("src.shared.cache.redis_client.get_redis_client", return_value=redis),
            patch(
                "src.domains.critique.routes.jobs.get_db_session",
                lambda: _tracked_session(ledger),
            ),
        ):
            settings.return_value.features.sse_progress = True
            await _stream(app)

        assert ledger.acquired >= 1, "the preflight lookup never opened a session"
        assert ledger.open == 0

    async def test_unknown_job_still_404s(self) -> None:
        """The preflight ownership check must survive the session change."""
        ledger = _SessionLedger()
        redis = _ProgressRedis(ledger)
        app = _make_app(ledger, None)

        with (
            patch("src.config.get_settings") as settings,
            patch("src.shared.cache.redis_client.get_redis_client", return_value=redis),
            patch(
                "src.domains.critique.routes.jobs.get_db_session",
                lambda: _tracked_session(ledger),
            ),
        ):
            settings.return_value.features.sse_progress = True
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/api/critique/{_JOB_ID}/progress")

        assert response.status_code == 404
        # Guard against a routing 404 faking this assertion.
        assert _JOB_ID in response.json()["detail"]
        assert redis.polls == 0
        assert ledger.open == 0

    async def test_preflight_lookup_is_tenant_scoped(self) -> None:
        """The stream must not become a cross-tenant existence oracle."""
        ledger = _SessionLedger()
        redis = _ProgressRedis(ledger)
        app = _make_app(ledger, _job())

        with (
            patch("src.config.get_settings") as settings,
            patch("src.shared.cache.redis_client.get_redis_client", return_value=redis),
            patch(
                "src.domains.critique.routes.jobs.get_db_session",
                lambda: _tracked_session(ledger),
            ),
        ):
            settings.return_value.features.sse_progress = True
            await _stream(app)

        service = app.state.critique_service
        service.get_job.assert_awaited_once()
        assert service.get_job.await_args.kwargs.get("tenant_id") == _TENANT
