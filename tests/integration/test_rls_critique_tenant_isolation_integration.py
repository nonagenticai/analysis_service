"""Postgres-RLS-ENFORCE tenant-isolation test for the single-job critique GET.

The async critique-pipeline test
(``tests/integration/test_critique_pipeline_integration.py``) explicitly
documents a gap: the single-job ``GET /api/critique/{id}`` handler
(``get_critique`` → ``CritiqueService.get_job`` →
``select(CritiqueJob).where(CritiqueJob.job_id == job_id)``) has NO app-level
tenant filter — it relies on the Phase D RLS ``tenant_isolation`` policy to hide
another tenant's row — and the bare FastAPI test apps those tests spin up
connect as the owner/superuser DB role, which BYPASSes RLS. So that test can
only assert the *list* route's ``WHERE tenant_id`` filter, not the production
boundary that actually protects the single-GET.

This test closes that gap, mirroring the merged review template
(``test_rls_tenant_isolation_integration.py``) for the critique domain. It runs
the two assertion GETs through an app whose per-request DB session
``SET LOCAL ROLE``s to a throwaway ``NOBYPASSRLS`` non-owner role (the posture
the runtime ``w2v_analysis_app`` role has under ``TENANT_RLS_MODE=enforce``) and
binds ``app.current_tenant`` via ``set_config(..., is_local => true)`` from the
identity ContextVar. Under that role the shipped policy — applied by
``register_rls_ddl`` on ``create_all`` in the ``db_engine`` fixture — makes
tenant A's completed critique job:

* **visible** to tenant A  → ``GET /api/critique/{id}`` returns 200 + results, and
* **invisible** to tenant B → the same GET returns 404, because
  ``CritiqueService.get_job`` sees zero rows (RLS), NOT because of any app code.

The job is *arranged* through a separate OWNER app (RLS-bypassing), and its
detached ``_process_job_background`` task keeps writing as the owner (via
``_patch_background_session``, which points the function-locally-resolved
``src.shared.database.session.get_db_session`` at the OWNER factory) so the
completing UPDATE always matches and the job never hangs in ``running``. Only
the two assertion GETs are enforced. A final owner-session SELECT proves the row
still exists — so the 404 is RLS-invisibility, not deletion.

Skips loudly where the test DB user cannot ``CREATE ROLE`` (CI/hook role lacks
superuser/CREATEROLE), exactly like ``tests/test_rls_isolation.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response
from w2v_common.auth import (
    clear_identity_context,
    get_tenant_id_from_context,
    set_identity_context,
)

from src.domains.critique.models import CritiqueJob
from src.domains.critique.routes.jobs import read_router, write_router
from src.domains.critique.services.critique_service import CritiqueService
from src.domains.critique.services.critique_service.scoring_result import _ScoringResult
from src.shared.database.session import get_db

_TENANT_A = "43835e47-73c0-57cf-8119-4f26331cc570"
_TENANT_B = "e3a0c22f-5b84-5c92-9ae2-6ecd8cdadce1"
_SITE_URL = "https://acme-critique-rls-integration.example.com/"


# ---------------------------------------------------------------------------
# Stubs copied from test_critique_pipeline_integration.py — deterministic,
# network-free completion. ``process_job`` fetches ``job.url`` (_resolve_html)
# before the scanner phase, so that too is stubbed; ``_phase_check_cache`` is
# forced to MISS so the scanner phase (and its small sleep) always runs.
# ---------------------------------------------------------------------------


async def _stub_resolve_html(*_args: object, **_kwargs: object) -> str:
    return "<html><head><title>stub</title></head><body></body></html>"


async def _stub_run_scanners(
    *_args: object, **_kwargs: object
) -> tuple[list[dict], dict[str, float], list[str]]:
    await asyncio.sleep(0.15)
    return [], {}, []


async def _stub_check_cache(*_args: object, **_kwargs: object) -> bool:
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


def _apply_service_stubs(monkeypatch: pytest.MonkeyPatch, service: CritiqueService) -> None:
    """Apply the four critique pipeline stubs to a ``CritiqueService`` instance
    the way ``test_critique_pipeline_integration.py`` does."""
    monkeypatch.setattr(service, "_resolve_html", _stub_resolve_html)
    monkeypatch.setattr(service, "_phase_check_cache", _stub_check_cache)
    monkeypatch.setattr(service, "_phase_run_scanners", _stub_run_scanners)
    monkeypatch.setattr(service, "_phase_score_and_summarize", _stub_score_and_summarize)


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


@pytest.fixture
def _patch_background_session(
    monkeypatch: pytest.MonkeyPatch, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Point ``get_db_session`` (resolved function-locally inside the detached
    ``_process_job_background`` — see ``routes/_helpers.py``) at the test factory
    so the background task and the poll loop share the same per-worker schema —
    otherwise the task would write to a different schema and the poll would never
    see ``completed``. The background session is deliberately left as the OWNER
    session (no role/GUC) so the completing UPDATE always matches under
    RLS-bypass and the job never hangs in ``running``.

    The import is function-local, so patch the SOURCE module attribute
    (``src.shared.database.session.get_db_session``), not a route-level re-import.
    """

    @contextlib.asynccontextmanager
    async def _fake_get_db_session() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session
            await session.commit()

    monkeypatch.setattr("src.shared.database.session.get_db_session", _fake_get_db_session)


# ---------------------------------------------------------------------------
# Owner app (RLS-bypass) — clone of the pipeline test's _make_app, used only to
# ARRANGE the completed job (it is the app whose detached task runs process_job).
# ---------------------------------------------------------------------------


def _make_owner_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI(title="analysis-service-critique-rls-owner-app")

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

    # write_router first (POST /critique), then read_router (GET /critique/{id}),
    # both under /api → production /api/critique... paths.
    app.include_router(write_router, prefix="/api")
    app.include_router(read_router, prefix="/api")
    return app


# ---------------------------------------------------------------------------
# Enforcing app (SET LOCAL ROLE + app.current_tenant GUC) — used ONLY for the
# two assertion GETs. This is the app that actually exercises the RLS boundary.
# ---------------------------------------------------------------------------


def _make_rls_app(factory: async_sessionmaker[AsyncSession], role: str) -> FastAPI:
    app = FastAPI(title="analysis-service-critique-rls-enforce-app")
    # ``role`` is a trusted per-worker constant, but it is an SQL identifier and
    # cannot be a bind parameter — quote it as an identifier, never interpolate
    # user input here (same posture as tests/test_rls_isolation.py).
    set_role_stmt = text(f'SET LOCAL ROLE "{role}"')

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            # Transaction-scoped, in this exact order:
            # 1. drop to the restricted NOBYPASSRLS role so RLS is enforced,
            # 2. read the tenant bound onto the ContextVar by the middleware,
            # 3. bind app.current_tenant for the RLS policy (is_local => true),
            # 4. hand the session to the route, then commit.
            await session.execute(set_role_stmt)
            # w2v_common 0.67.0: this ContextVar is a ``uuid.UUID | None``.
            # ``str(...)`` is load-bearing — psycopg adapts a bare UUID to the
            # postgres ``uuid`` type and ``set_config(text, text, bool)`` has no
            # such overload, so the bind dies with "function set_config(unknown,
            # uuid, boolean) does not exist". The GUC is text and the tenant
            # columns are varchar, so the tenant must cross as text.
            #
            # An absent tenant is NOT bound to "": that is the blank-tenant
            # substitution 0.67.0 removed, and under RLS it would silently widen
            # this assertion from "isolated" to "matched nothing". The route is
            # given an unbound session instead, which fails closed.
            tenant = get_tenant_id_from_context()
            if tenant is not None:
                await session.execute(
                    text("SELECT set_config('app.current_tenant', :t, true)"),
                    {"t": str(tenant)},
                )
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
            # override_db resolves the tenant from the identity ContextVar, so it
            # MUST be set before call_next. Cleared in finally so the ContextVar
            # never leaks across requests/tests under ``-n4``.
            set_identity_context(tenant, None, None)
        try:
            return await call_next(request)
        finally:
            clear_identity_context()

    app.include_router(write_router, prefix="/api")
    app.include_router(read_router, prefix="/api")
    return app


# ---------------------------------------------------------------------------
# Restricted-role fixture (skip-gated) — mirrors tests/test_rls_isolation.py's
# role lifecycle, with a per-worker name distinct from review's
# ``w2v_analysis_rls_get_*`` so the two RLS tests don't collide under ``-n4``.
# ---------------------------------------------------------------------------


@pytest.fixture
async def restricted_get_role(db_engine) -> AsyncIterator[str]:
    role = f"w2v_critique_rls_get_{os.environ.get('PYTEST_XDIST_WORKER', 'gw')}"
    created = False
    try:
        async with db_engine.begin() as conn:
            try:
                await conn.execute(text(f'CREATE ROLE "{role}" NOSUPERUSER NOBYPASSRLS NOINHERIT'))
            except ProgrammingError as exc:  # CI/hook user lacks CREATEROLE
                pytest.skip(f"cannot CREATE ROLE (needs superuser/CREATEROLE): {exc.orig}")
            created = True
            # ``critique_jobs`` resolves via the per-worker search_path set by the
            # conftest ``connect`` listener; USAGE on the schema is already
            # granted TO PUBLIC there.
            await conn.execute(
                text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON critique_jobs TO "{role}"')
            )
        yield role
    finally:
        if created:
            async with db_engine.begin() as conn:
                await conn.execute(text("RESET ROLE"))
                await conn.execute(text(f'DROP OWNED BY "{role}"'))
                await conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


async def test_single_critique_get_is_rls_isolated_across_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    _patch_background_session: None,
    restricted_get_role: str,
) -> None:
    """Tenant A creates + completes a critique job (owner app, RLS-bypass). Then,
    through the ENFORCING app (restricted role + tenant GUC), tenant A's single
    ``GET /api/critique/{id}`` returns 200 while tenant B's returns 404 — the row
    is invisible under RLS, not filtered by app code. Row still exists after."""
    owner_app = _make_owner_app(db_session_factory)
    rls_app = _make_rls_app(db_session_factory, restricted_get_role)
    # Apply the four deterministic pipeline stubs to BOTH apps' services. The
    # owner app's service actually runs the detached ``process_job`` (so it needs
    # them); the rls app's service only serves GETs (get_job), but is stubbed
    # identically for symmetry.
    _apply_service_stubs(monkeypatch, owner_app.state.critique_service)
    _apply_service_stubs(monkeypatch, rls_app.state.critique_service)

    # 1. ARRANGE: create + complete the job for tenant A via the owner app.
    async with AsyncClient(
        transport=ASGITransport(app=owner_app), base_url="http://test"
    ) as owner_client:
        start = await owner_client.post(
            "/api/critique", json=_critique_payload(), headers={"X-Test-Tenant": _TENANT_A}
        )
        assert start.status_code == 200, start.text
        job_id = start.json()["job_id"]
        assert job_id

        final = await _poll_until_terminal(owner_client, job_id, _TENANT_A)
        assert final["status"] == "completed", final
        assert final["overall_score"] == 100.0, final

    # 2. ACT / ASSERT: the two single-GETs run under the ENFORCING app.
    async with AsyncClient(
        transport=ASGITransport(app=rls_app), base_url="http://test"
    ) as rls_client:
        # Tenant A: the RLS policy makes A's own row visible → 200 + results.
        resp_a = await rls_client.get(
            f"/api/critique/{job_id}", headers={"X-Test-Tenant": _TENANT_A}
        )
        assert resp_a.status_code == 200, resp_a.text
        body_a = resp_a.json()
        assert body_a["status"] == "completed", body_a
        assert body_a["overall_score"] == 100.0, body_a

        # Tenant B: the row is INVISIBLE under RLS → get_job sees nothing → 404.
        resp_b = await rls_client.get(
            f"/api/critique/{job_id}", headers={"X-Test-Tenant": _TENANT_B}
        )
        assert resp_b.status_code == 404, resp_b.text

    # 3. Prove the 404 was RLS-invisibility, not deletion: the owner (RLS-bypass)
    #    session still sees the row, tenant-scoped to A.
    async with db_session_factory() as verify:
        row = (
            await verify.execute(select(CritiqueJob).where(CritiqueJob.job_id == job_id))
        ).scalar_one()
        assert row.tenant_id == _TENANT_A
        assert row.status == "completed"
