"""Real HTTP + real-Postgres ASYNC job-poll integration test for the critique domain.

Higher-fidelity companion to the review pipeline integration test: this drives
the actual *async* critique pipeline — ``POST /api/critique`` (create job,
commit, dispatch the detached ``_process_job_background``) → poll
``GET /api/critique/{id}`` until terminal → assert the persisted ``CritiqueJob``
row and tenant-scoped list isolation.

It exercises two things a strictly-sequential flow cannot:

1. **Two DB connections held open at once** — the detached
   ``_process_job_background`` task and the poll loop, each a fresh ``NullPool``
   connect on the same worker schema. This is the class of two-connection flow
   that made the per-connect ``CREATE SCHEMA``/``GRANT`` DDL race the
   ``pg_namespace`` tuple under xdist (``tuple concurrently updated``) and
   motivated the per-process schema-DDL guard in ``tests/conftest.py`` (PR #125).
   Holding both connections is SAFE now, so this test polls the live detached
   task without draining it — no rerun masking.
2. **The create-commit ordering** — ``create_job`` commits the job row *before*
   ``asyncio.create_task``; the detached task opens its own session and drives
   ``process_job`` → ``_phase_finalize_completed`` on that row, which only
   matches because of the commit. Point the background session at a different
   schema and this test hangs in ``running``.

``_process_job_background`` resolves its own session via a **function-local**
``from src.shared.database.session import get_db_session`` (see
``routes/_helpers.py``), so ``_patch_background_session`` patches the SOURCE
module attribute (``src.shared.database.session.get_db_session``) — not a
route-level re-import — pointing it at the test factory so the background task
and the poll loop share the per-worker schema.

The scanning + scoring phases (and the URL HTML fetch) are stubbed to canned
results so the job reaches ``completed`` deterministically with zero network:
``process_job`` fetches ``job.url`` via ``_resolve_html`` before running
scanners, so that too is stubbed here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

from src.domains.critique.models import CritiqueJob
from src.domains.critique.routes.jobs import read_router, write_router
from src.domains.critique.services.critique_service import CritiqueService
from src.domains.critique.services.critique_service.scoring_result import _ScoringResult
from src.shared.database.session import get_db

_TENANT_A = "a9a99f58-5c6e-505e-b7a1-8cfda7786e38"
_TENANT_B = "c6b4914b-d066-5054-be22-c98e1f6f9688"
_SITE_URL = "https://acme-critique-integration.example.com/"


def _make_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="analysis-service-critique-integration-test")

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db

    service = CritiqueService()
    service.set_adapter_registry(None)
    app.state.critique_service = service

    @app.middleware("http")
    async def _inject_tenant(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        tenant = request.headers.get("X-Test-Tenant")
        if tenant:
            request.state.tenant_id = tenant
        return await call_next(request)

    # write_router: POST /critique (+ tags/rerun/progress); read_router:
    # GET /critique/{id}, GET /critique, DELETE. Both are prefix-less, so mount
    # under /api to yield the production /api/critique... paths. write first.
    app.include_router(write_router, prefix="/api")
    app.include_router(read_router, prefix="/api")
    return app


@pytest.fixture
def _patch_background_session(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Point ``get_db_session`` (resolved function-locally inside the detached
    ``_process_job_background``) at the test factory so the background task and
    the poll loop share the same per-worker schema — otherwise the task would
    write to a different schema and the poll would never see ``completed``.

    The import in ``routes/_helpers.py`` is function-local
    (``from src.shared.database.session import get_db_session`` inside the task
    body), so patching a route-level name would not take — patch the SOURCE.
    """

    @contextlib.asynccontextmanager
    async def _fake_get_db_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session
            await session.commit()

    monkeypatch.setattr("src.shared.database.session.get_db_session", _fake_get_db_session)


def _critique_payload() -> dict[str, object]:
    return {"url": _SITE_URL, "categories": ["seo"]}


async def _poll_until_terminal(
    client: AsyncClient, job_id: str, tenant: str, timeout_s: float = 8.0
) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/critique/{job_id}", headers={"X-Test-Tenant": tenant})
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("status") in {"completed", "failed"}:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach a terminal state in {timeout_s}s: {last}")


async def test_critique_pipeline_completes_and_is_tenant_isolated(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    _patch_background_session: None,
) -> None:
    """POST critique → detached task completes it → poll sees completed →
    persisted row + list isolation, all under real HTTP + real Postgres."""
    app = _make_app(db_session_factory)
    service = app.state.critique_service

    # Deterministic, network-free completion. ``process_job`` fetches
    # ``job.url`` (``_resolve_html`` → ``_fetch_html``) *before* the scanner
    # phase, so stub that too. The two heavy phases are replaced with canned
    # results; a small sleep keeps the job in ``running`` long enough that the
    # poll loop and the detached task genuinely hold two connections at once.
    async def _stub_resolve_html(*_args: object, **_kwargs: object) -> str:
        return "<html><head><title>stub</title></head><body></body></html>"

    async def _stub_run_scanners(
        *_args: object, **_kwargs: object
    ) -> tuple[list[dict], dict[str, float], list[str]]:
        await asyncio.sleep(0.15)
        return [], {}, []

    async def _stub_check_cache(*_args: object, **_kwargs: object) -> bool:
        # Force a cache MISS so the scanner phase (and its two-connection window)
        # always runs, regardless of any redis cache state — otherwise a cache
        # hit could short-circuit to completed before the sleep below.
        return False

    async def _stub_score_and_summarize(*_args: object, **_kwargs: object) -> _ScoringResult:
        return _ScoringResult(
            findings=[],
            scores={},
            overall_score=100.0,
            confidence={},
            summary="stub",
            report="# stub",
            token_usage=None,
        )

    monkeypatch.setattr(service, "_resolve_html", _stub_resolve_html)
    monkeypatch.setattr(service, "_phase_check_cache", _stub_check_cache)
    monkeypatch.setattr(service, "_phase_run_scanners", _stub_run_scanners)
    monkeypatch.setattr(service, "_phase_score_and_summarize", _stub_score_and_summarize)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Start a critique job for tenant A (async → returns a job_id).
        start = await client.post(
            "/api/critique", json=_critique_payload(), headers={"X-Test-Tenant": _TENANT_A}
        )
        assert start.status_code == 200, start.text
        job_id = start.json()["job_id"]
        assert job_id

        # 2. Poll until the detached background task marks it terminal.
        final = await _poll_until_terminal(client, job_id, _TENANT_A)
        assert final["status"] == "completed", final
        assert final["overall_score"] == 100.0, final

        # 3. Direct DB assertion: the row is persisted, tenant-scoped to A,
        #    completed, with the canned scoring the detached task wrote.
        async with db_session_factory() as verify:
            row = (
                await verify.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
            ).scalar_one()
            assert row.tenant_id == _TENANT_A
            assert row.status == "completed"
            assert row.overall_score == 100.0

        # 4. App-level list scoping: ``list_jobs(..., tenant_id=...)`` filters on
        #    WHERE tenant_id, so tenant B sees none of tenant A's jobs and tenant
        #    A sees its own. (The single-job GET is not app-scoped — it relies on
        #    RLS enforce, which the bare test app does not mount — so this asserts
        #    the list filter, not the full production boundary.)
        list_b = await client.get("/api/critique", headers={"X-Test-Tenant": _TENANT_B})
        assert list_b.status_code == 200, list_b.text
        assert all(j["job_id"] != job_id for j in list_b.json()["items"])

        list_a = await client.get("/api/critique", headers={"X-Test-Tenant": _TENANT_A})
        assert list_a.status_code == 200, list_a.text
        assert any(j["job_id"] == job_id for j in list_a.json()["items"])
