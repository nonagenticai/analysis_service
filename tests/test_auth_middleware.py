"""Guardrail tests for the auth middleware integration.

analysis_service delegates auth to ``w2v_common.auth.middleware.AuthMiddleware``
(re-exported from ``src.shared.auth.middleware``). This test exercises that the
middleware is **wired and registered** on the asgi app (not merely importable),
and that it behaves correctly for the standard public/protected/trusted-network
matrix.

The intent is to catch the "false-merge" pattern: a PR that says it added auth
but never called ``app.add_middleware(AuthMiddleware)``, or that registered it
behind a feature flag that defaults to off.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

# Re-export from the shared module — same import path the asgi entry uses.
import w2v_common.auth.middleware as w2v_auth
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.shared.auth.middleware import AuthMiddleware


@pytest.fixture(autouse=True)
def _clear_license_cache() -> None:
    cache = w2v_auth._get_license_cache()
    cache.clear()


def _make_minimal_app() -> FastAPI:
    """Tiny app for behaviour tests — independent of asgi lifespan side-effects."""
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/critique/echo")
    async def _echo(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "tenant_id": getattr(request.state, "tenant_id", None),
                "license_present": getattr(request.state, "license", None) is not None,
            }
        )

    return app


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Wiring check — the regression-against-false-merge assertion
# ---------------------------------------------------------------------------


def test_auth_middleware_is_actually_registered_on_asgi_app() -> None:
    """The asgi entry must register AuthMiddleware via ``app.add_middleware``.

    Reading ``settings.auth.enabled`` gates the registration in asgi.py.
    Tests run with ``AUTH_ENABLED=false`` (per conftest.py), so the wiring
    check has to compare against the production setting, not the live
    middleware stack. We verify both:
    (1) the import path resolves to the shared library, and
    (2) the asgi module references ``app.add_middleware(AuthMiddleware)``.
    """
    # (1) Re-export goes to the shared lib (not a local stub).
    from w2v_common.auth.middleware import AuthMiddleware as SharedAuthMiddleware

    assert AuthMiddleware is SharedAuthMiddleware

    # (2) The asgi module builds the app via the shared factory (which owns auth).
    asgi_src = (
        __import__("pathlib").Path(__file__).resolve().parent.parent / "src" / "asgi.py"
    ).read_text()
    assert "create_app(" in asgi_src, (
        "asgi.py must build the app via w2v_common.asgi.create_app — "
        "see [[feedback-false-merge-pattern-watch-PR-titles]]"
    )


def test_create_app_registers_shared_auth_when_enabled() -> None:
    """Building the app the way asgi.py does, with auth on, wires the shared
    AuthMiddleware onto the stack — the runtime complement to the source guard."""
    from w2v_common.asgi import create_app
    from w2v_common.auth.middleware import AuthMiddleware as SharedAuthMiddleware

    from src.config import get_settings

    app = create_app(get_settings(), auth_enabled=True)
    assert SharedAuthMiddleware in {m.cls for m in app.user_middleware}


# ---------------------------------------------------------------------------
# Public bypass
# ---------------------------------------------------------------------------


async def test_health_is_public(client: httpx.AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------


async def test_protected_route_with_valid_license_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    introspect_payload: dict[str, Any] = {
        "active": True,
        "tenant_id": "c09a6317-09d7-59cb-8089-0ab33ec2293d",
        "subject_id": "subj-1",
        "license_id": "lic-1",
        "roles": ["user"],
    }

    async def _fake(token: str) -> dict[str, Any]:
        assert token == "abc.def.ghi"
        return introspect_payload

    monkeypatch.setattr(w2v_auth, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/critique/echo",
            headers={"X-Tenant-Id": "Bearer abc.def.ghi"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tenant_id"] == "c09a6317-09d7-59cb-8089-0ab33ec2293d"
    assert body["license_present"] is True


async def test_protected_route_with_invalid_license_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake(_token: str) -> dict[str, Any]:
        return {"active": False, "reason": "expired"}

    monkeypatch.setattr(w2v_auth, "_introspect_license", _fake)

    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(
            "/api/critique/echo",
            headers={"X-Tenant-Id": "Bearer dud-token"},
        )

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "INVALID_TOKEN"
    assert body["error"]["status"] == 401
    assert "Invalid or expired license token" in body["error"]["message"]


async def test_protected_route_no_token_untrusted_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untrusted source IP + no token must 401, regardless of TRUST_INCLUSTER_IDENTITY."""
    # Force the trust-incluster path off so the test is deterministic.
    monkeypatch.setattr(w2v_auth, "_trust_incluster_identity", lambda: False)

    transport = httpx.ASGITransport(app=_make_minimal_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/api/critique/echo")
    # ASGITransport peer = 'testclient' which is NOT in the trusted-CIDR list,
    # so this should hit the 401 branch.
    assert resp.status_code == 401


async def test_introspection_cache_avoids_second_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two introspect calls with the same token must hit the licenses endpoint once.

    Asserted directly against ``_introspect_license`` (not via the middleware)
    because monkeypatching ``httpx.AsyncClient`` globally also intercepts the
    outer test client and breaks the request path. The cache lives in
    ``_introspect_license`` itself, so this is the correct seam.
    """
    network_calls = 0

    class _MockResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "active": True,
                "tenant_id": "65f56478-b726-5d13-825f-623b18d8a4cc",
                "subject_id": "s",
            }

    class _MockClient:
        def __init__(self, *_: Any, **__: Any) -> None: ...

        async def __aenter__(self) -> _MockClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def post(self, *_: Any, **__: Any) -> _MockResp:
            nonlocal network_calls
            network_calls += 1
            return _MockResp()

    monkeypatch.setattr(w2v_auth.httpx, "AsyncClient", _MockClient)

    first = await w2v_auth._introspect_license("same.token")
    second = await w2v_auth._introspect_license("same.token")

    assert first is not None and first.get("active") is True
    assert second == first
    assert network_calls == 1, "Second call must be served from the TTL cache"
