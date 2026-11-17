"""ICP route -> webscraper under ``WEBSCRAPER_AUTH_MODE`` (w2v_common TenantLicenceLane).

Real ``WebscraperClient`` with its real event hooks; only the network is replaced.
Default (``shared``) must send the shared ``DOWNSTREAM_LICENSE_TOKEN``;
``tenant_licence`` must send the bound tenant's own minted ``w2v`` licence and
never the shared token or ``X-Effective-Tenant-Id``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from w2v_common.auth.bundle_tokens import (
    BundleMinterNotConfiguredError,
    BundleTokenMinter,
    reset_bundle_token_minter,
    set_bundle_token_minter,
)
from w2v_common.auth.identity_context import clear_identity_context, set_identity_context
from w2v_common.auth.tenant_licence_lane import TenantLicenceLane
from w2v_common.tenant import TenantSource

from src.domains.icp.routes import extract_icp_from_url
from src.domains.icp.schemas import ICPFromUrlRequest
from tests.llm_test_doubles import FakeWMClient

TENANT = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
SHARED = "shared-downstream-licence"  # pragma: allowlist secret
TOKEN_URL = "http://kc.test/realms/secure-apps/protocol/openid-connect/token"
_REAL_ASYNC_CLIENT = httpx.AsyncClient
_ICP = {
    "company": "Merqi B.V.",
    "industry": "Marketing technology",
    "target_audience": "Agencies",
    "value_proposition": "One GTM OS",
    "pricing_signals": "From 49/mo",
    "brand_identity": {"name": "Merqi"},
    "positioning": "The GTM OS for agencies",
    "category": "Marketing automation",
    "competitors": ["HubSpot"],
    "pains": ["Fragmented tools"],
    "problems": ["Manual ops"],
}


class _Wire:
    def __init__(self) -> None:
        self.scraper: list[httpx.Request] = []
        self.minted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "minter-sa", "expires_in": 300})
        if request.url.host == "lic.test":
            app = request.url.params["app"]
            self.minted.append(app)
            return httpx.Response(
                201,
                json={
                    "token": f"lic-{app}-{len(self.minted)}",
                    "expires_at": datetime.fromtimestamp(4_000_000_000, tz=UTC).isoformat(),
                    "tenant_id": request.url.path.split("/")[3],
                    "app": app,
                    "license_id": f"lid-{app}-{len(self.minted)}",
                },
            )
        self.scraper.append(request)
        return httpx.Response(
            200,
            json={
                "url": "https://merqi.nl",
                "status": "completed",
                "title": "Merqi",
                "text_content": "Merqi copy",
            },
        )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Wire]:
    monkeypatch.setenv("DOWNSTREAM_LICENSE_TOKEN", SHARED)
    monkeypatch.delenv("WEBSCRAPER_AUTH_MODE", raising=False)
    w = _Wire()

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(w.handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    reset_bundle_token_minter()
    set_bundle_token_minter(
        BundleTokenMinter(
            licenses_url="http://lic.test",
            token_url=TOKEN_URL,
            client_id="w2v-bundle-minter",
            client_secret="s",  # pragma: allowlist secret
            transport=httpx.MockTransport(w.handler),
        )
    )
    set_identity_context(
        TENANT, "inbound-licence", "subject-1", tenant_source=TenantSource.LICENSE_TOKEN
    )
    yield w
    clear_identity_context()
    reset_bundle_token_minter()


def _request(lane: TenantLicenceLane) -> MagicMock:
    request = MagicMock()
    request.state.tenant_id = TENANT
    request.state.license_token = None
    request.app.state.wm_client = FakeWMClient(outputs=[dict(_ICP)])
    request.app.state.icp_cache = None
    request.app.state.webscraper_lane = lane
    return request


@pytest.fixture(autouse=True)
def _no_db_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("src.domains.icp.routes._persist_icp", _noop)


async def test_default_lane_sends_shared_token_unchanged(wire: _Wire) -> None:
    await extract_icp_from_url(
        ICPFromUrlRequest(url="https://merqi.nl"), _request(TenantLicenceLane("webscraper"))
    )
    assert wire.scraper, "the route never reached webscraper"
    assert wire.scraper[0].url.path == "/api/scrape/url"
    assert wire.scraper[0].headers["X-Tenant-Id"] == f"Bearer {SHARED}"
    # webscraper is not a metered destination: nothing is delegated.
    assert "X-Effective-Tenant-Id" not in wire.scraper[0].headers
    assert wire.minted == []


async def test_tenant_licence_lane_sends_tenants_w2v_licence(wire: _Wire) -> None:
    lane = TenantLicenceLane("webscraper", mode="tenant_licence")
    await extract_icp_from_url(ICPFromUrlRequest(url="https://merqi.nl"), _request(lane))
    req = wire.scraper[0]
    assert req.headers["X-Tenant-Id"] == "Bearer lic-w2v-1"
    assert req.headers["X-License-Id"] == "lid-w2v-1"
    assert "X-Effective-Tenant-Id" not in req.headers
    assert SHARED not in str(req.headers)
    assert wire.minted == ["w2v"]


def test_tenant_licence_without_minter_config_fails_at_lane_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The lane is built in _startup: this is the boot failure, not a request one.
    for name in (
        "BUNDLE_MINTER_CLIENT_ID",
        "BUNDLE_MINTER_CLIENT_SECRET",
        "BUNDLE_MINTER_TOKEN_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEBSCRAPER_AUTH_MODE", "tenant_licence")
    reset_bundle_token_minter()
    with pytest.raises(BundleMinterNotConfiguredError):
        TenantLicenceLane("webscraper")


async def test_shared_lane_without_a_licence_is_a_502_not_an_unlicensed_scrape(
    wire: _Wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    # webscraper bills a request without X-Tenant-Id: Bearer to a default tenant,
    # so WebscraperClient refuses to exist without a licence. The route must turn
    # that into a fail-closed 502, never a 500 and never a request on the wire.
    monkeypatch.delenv("DOWNSTREAM_LICENSE_TOKEN", raising=False)
    with pytest.raises(HTTPException) as raised:
        await extract_icp_from_url(
            ICPFromUrlRequest(url="https://merqi.nl"), _request(TenantLicenceLane("webscraper"))
        )
    assert raised.value.status_code == 502
    assert "not configured" in str(raised.value.detail)
    assert wire.scraper == []
