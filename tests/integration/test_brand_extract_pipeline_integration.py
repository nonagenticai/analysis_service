"""Real HTTP + real-Postgres integration test for the brand domain.

Mirrors ``slide_service/tests/integration/test_deck_pipeline_integration.py``
in spirit: it drives the actual FastAPI brand routers over
``httpx.ASGITransport`` against the CI Postgres database (the ``db_engine`` /
``db_session_factory`` fixtures in ``tests/conftest.py``) so the SQL, the
persistence, and the tenant-scoped read-back all run for real.

Why the *brand extract* pipeline (a synchronous request flow) rather than an
async job-and-poll pipeline like ``review``: the analysis_service conftest
registers a per-connection SQLAlchemy ``connect`` listener that runs
``CREATE SCHEMA IF NOT EXISTS`` + ``GRANT USAGE ... TO PUBLIC`` +
``SET search_path`` on *every* new connection so each xdist worker gets an
isolated schema. Under the parallel ``-n 4`` CI run a test that keeps two
connections open at once (a detached background task plus a poll loop, each a
fresh ``NullPool`` connect) makes two of those ``GRANT``s hit the same
``pg_namespace`` catalog tuple concurrently, which Postgres rejects with
"tuple concurrently updated". The brand extract flow is strictly sequential --
POST (one connection) -> GET (one connection) -> direct verify (one
connection) -- so it never holds two connections open simultaneously and is
deterministic under xdist without relying on reruns.

The LLM gateway is never touched: ``extract_from_analysis`` is pure,
in-process logic over the posted ``AnalysisData`` (no WM / network call), so no
stubbing is required.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

from src.domains.brand.brands_routes import brands_router
from src.domains.brand.models import Brand, BrandExtractionLog
from src.domains.brand.routes import router as brand_router
from src.shared.database.session import get_db

# A URL whose netloc becomes the persisted brand domain (``www.`` is stripped by
# ``extract_from_analysis``).
_SITE_URL = "https://acme-integration.example.com/"
_EXPECTED_DOMAIN = "acme-integration.example.com"
_TENANT_A = "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
_TENANT_B = "4151df14-4770-5a91-ab9f-cbf2d14e68be"


def _make_app(factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    """Build a bare test app whose ``get_db`` yields real AsyncSessions and
    which forwards ``X-Test-Tenant`` into ``request.state.tenant_id`` -- the
    attribute the brand routes read via ``getattr(request.state, ...)``.

    The real ``AuthMiddleware`` is not mounted (the same approach the existing
    brand route unit tests take); only the routers under test are included.
    """
    app = FastAPI(title="analysis-service-brand-integration-test")

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db

    @app.middleware("http")
    async def _inject_tenant(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        tenant = request.headers.get("X-Test-Tenant")
        if tenant:
            request.state.tenant_id = tenant
        return await call_next(request)

    app.include_router(brand_router)
    app.include_router(brands_router)
    return app


def _extract_payload() -> dict[str, object]:
    return {
        "analysis": {
            "pages": [
                {
                    "url": _SITE_URL,
                    "title": "Acme — Integration Home",
                    "metadata": {"description": "Acme integration test site"},
                }
            ],
            "site": {
                "description": "Acme integration test site",
                "logo_url": "https://acme-integration.example.com/logo.svg",
            },
        }
    }


async def test_brand_extract_pipeline_persist_and_isolation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """POST extract -> assert HTTP body AND persisted rows -> GET read-back ->
    cross-tenant read is 404 / empty while the owning tenant sees the brand.
    """
    app = _make_app(db_session_factory)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Extract from analysis for tenant A -> 200, domain derived from URL.
        extract = await client.post(
            "/api/brand/extract",
            json=_extract_payload(),
            headers={"X-Test-Tenant": _TENANT_A},
        )
        assert extract.status_code == 200, extract.text
        extract_body = extract.json()
        assert extract_body["domain"] == _EXPECTED_DOMAIN

        # 2. Read the persisted brand back over HTTP for tenant A.
        read_back = await client.get(
            f"/api/brands/{_EXPECTED_DOMAIN}", headers={"X-Test-Tenant": _TENANT_A}
        )
        assert read_back.status_code == 200, read_back.text
        assert read_back.json()["domain"] == _EXPECTED_DOMAIN

        # 3. Direct DB assertions: the canonical brand row and the extraction log
        #    both landed, tenant-scoped to A.
        async with db_session_factory() as verify:
            brand_row = (
                await verify.execute(
                    select(Brand).where(
                        Brand.tenant_id == _TENANT_A, Brand.domain == _EXPECTED_DOMAIN
                    )
                )
            ).scalar_one()
            assert brand_row.tenant_id == _TENANT_A
            assert brand_row.domain == _EXPECTED_DOMAIN

            log_rows = (
                (
                    await verify.execute(
                        select(BrandExtractionLog).where(
                            BrandExtractionLog.tenant_id == _TENANT_A,
                            BrandExtractionLog.source == "analysis",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(log_rows) >= 1

        # 4. Tenant isolation: tenant B cannot read tenant A's brand (get is
        #    tenant-scoped in the repository -> 404).
        cross_get = await client.get(
            f"/api/brands/{_EXPECTED_DOMAIN}", headers={"X-Test-Tenant": _TENANT_B}
        )
        assert cross_get.status_code == 404

        # 5. Tenant B's list is empty; tenant A's list contains the brand.
        cross_list = await client.get("/api/brands", headers={"X-Test-Tenant": _TENANT_B})
        assert cross_list.status_code == 200
        cross_body = cross_list.json()
        assert cross_body["total"] == 0
        assert cross_body["items"] == []

        own_list = await client.get("/api/brands", headers={"X-Test-Tenant": _TENANT_A})
        assert own_list.status_code == 200
        own_body = own_list.json()
        assert own_body["total"] >= 1
        assert any(item["domain"] == _EXPECTED_DOMAIN for item in own_body["items"])
