"""Logo assets are addressed by a bare ``asset_id`` — scope them by tenant.

``GET /api/brand/logo/{asset_id}`` served, and ``DELETE`` destroyed, any
tenant's stored logo to any caller who held the id. The DELETE case re-reads
the row (and the stored bytes) after the refusal: a 404 status on its own does
not prove the delete did not run.

Owner-role connection, so RLS is inert here by design — this proves the
APP-level filter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import Response

from src.domains.brand.models import LogoAssetRecord
from src.domains.brand.routes import router as brand_router
from src.shared.database.session import get_db

pytestmark = pytest.mark.unit

_TENANT_A = "509d31f4-3110-50b1-b3d4-41149479c0b4"
_TENANT_B = "3bb67b63-875d-56e2-8d3d-c5f0b2add973"
_PNG = b"\x89PNG\r\n\x1a\nscoped-logo-bytes"


class _StubLogoService:
    """In-memory stand-in for the storage backend."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def retrieve(self, path: str) -> bytes | None:
        return self.store.get(path)

    async def delete(self, path: str) -> bool:
        return self.store.pop(path, None) is not None


def _make_app(factory: async_sessionmaker[AsyncSession], logo_service: _StubLogoService) -> FastAPI:
    app = FastAPI(title="brand-tenant-scoping-test")

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db
    app.state.logo_service = logo_service

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
    return app


def _h(tenant: str) -> dict[str, str]:
    return {"X-Test-Tenant": tenant}


async def _seed_logo(
    factory: async_sessionmaker[AsyncSession],
    logo_service: _StubLogoService,
    *,
    tenant_id: str,
) -> tuple[str, str]:
    asset_id = str(uuid.uuid7())
    storage_path = f"logos/{asset_id}.png"
    async with factory() as session:
        session.add(
            LogoAssetRecord(
                asset_id=asset_id,
                tenant_id=tenant_id,
                domain="scoped-brand.example.com",
                original_url="https://scoped-brand.example.com/logo.png",
                storage_path=storage_path,
                content_type="image/png",
                size_bytes=len(_PNG),
            )
        )
        await session.commit()
    logo_service.store[storage_path] = _PNG
    return asset_id, storage_path


async def test_serve_logo_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    logo_service = _StubLogoService()
    app = _make_app(db_session_factory, logo_service)
    asset_id, _path = await _seed_logo(db_session_factory, logo_service, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(f"/api/brand/logo/{asset_id}", headers=_h(_TENANT_A))
        assert ok.status_code == 200, ok.text
        assert ok.content == _PNG
        assert ok.headers["content-type"].startswith("image/png")

        stranger = await client.get(f"/api/brand/logo/{asset_id}", headers=_h(_TENANT_B))
        assert stranger.status_code == 404, (
            f"cross-tenant logo read returned {stranger.status_code} "
            f"({len(stranger.content)} bytes of another tenant's asset)"
        )

        # Byte-identical to a genuinely-missing asset: no existence oracle.
        missing = await client.get(f"/api/brand/logo/{uuid.uuid7()}", headers=_h(_TENANT_B))
        assert missing.status_code == 404
        assert stranger.content == missing.content


async def test_delete_logo_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """DESTRUCTIVE: a refused DELETE must leave both the row and the bytes."""
    logo_service = _StubLogoService()
    app = _make_app(db_session_factory, logo_service)
    asset_id, path = await _seed_logo(db_session_factory, logo_service, tenant_id=_TENANT_A)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        stranger = await client.delete(f"/api/brand/logo/{asset_id}", headers=_h(_TENANT_B))
        assert stranger.status_code == 404, (
            f"cross-tenant DELETE returned {stranger.status_code}: {stranger.text}"
        )

        # Status alone proves nothing -- re-read the row AND the stored bytes.
        async with db_session_factory() as session:
            row = (
                await session.execute(
                    select(LogoAssetRecord).where(LogoAssetRecord.asset_id == asset_id)
                )
            ).scalar_one_or_none()
            assert row is not None, "tenant B DELETED tenant A's logo record"
            assert row.tenant_id == _TENANT_A
        assert path in logo_service.store, "tenant B destroyed tenant A's stored logo bytes"

        # Paired own-tenant success on the same fixture.
        owner = await client.delete(f"/api/brand/logo/{asset_id}", headers=_h(_TENANT_A))
        assert owner.status_code == 200, owner.text
        assert owner.json() == {"deleted": True, "asset_id": asset_id}

    async with db_session_factory() as session:
        assert (
            await session.execute(
                select(LogoAssetRecord).where(LogoAssetRecord.asset_id == asset_id)
            )
        ).scalar_one_or_none() is None
    assert path not in logo_service.store


async def test_delete_logo_asset_repository_is_tenant_scoped(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drive ``BrandRepository.delete_logo_asset`` directly, not via the route.

    Through the route this predicate is unreachable — ``get_logo_asset`` refuses
    a foreign asset first, so the delete is never called with a mismatched
    tenant. That makes the repository-level filter pure defence-in-depth, and
    defence-in-depth that no test exercises is indistinguishable from dead code:
    it survives being deleted. This test is the one that kills that mutant, so
    the next caller of this method inherits a guarantee rather than a footgun.
    """
    from src.domains.brand.services.brand_repository import BrandRepository

    logo_service = _StubLogoService()
    asset_id, _path = await _seed_logo(db_session_factory, logo_service, tenant_id=_TENANT_A)

    async with db_session_factory() as session:
        assert (
            await BrandRepository.delete_logo_asset(session, asset_id, tenant_id=_TENANT_B) is False
        )
        await session.commit()

    async with db_session_factory() as session:
        row = (
            await session.execute(
                select(LogoAssetRecord).where(LogoAssetRecord.asset_id == asset_id)
            )
        ).scalar_one_or_none()
        assert row is not None, "delete_logo_asset destroyed another tenant's record"
        assert row.tenant_id == _TENANT_A

    # ...and the owner's delete does land.
    async with db_session_factory() as session:
        assert (
            await BrandRepository.delete_logo_asset(session, asset_id, tenant_id=_TENANT_A) is True
        )
        await session.commit()

    async with db_session_factory() as session:
        assert (
            await session.execute(
                select(LogoAssetRecord).where(LogoAssetRecord.asset_id == asset_id)
            )
        ).scalar_one_or_none() is None
