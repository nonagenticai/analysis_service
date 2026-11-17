"""Unit tests for auth middleware.

Tests AuthMiddleware dispatch logic: public endpoints, token introspection,
trusted networks, and caching behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from w2v_common.auth.middleware import (
    AuthMiddleware,
    _get_license_cache,
    _introspect_license,
    _is_trusted_network,
)


@pytest.fixture(autouse=True)
def clear_license_cache():
    """Clear the license cache before each test."""
    _get_license_cache().clear()
    yield
    _get_license_cache().clear()


def _make_request(
    path: str = "/api/review",
    method: str = "POST",
    headers: dict | None = None,
    client_host: str = "203.0.113.50",
) -> MagicMock:
    """Create a mock Starlette Request object."""
    request = MagicMock()
    request.url.path = path
    request.method = method
    request.headers = headers or {}
    request.state = MagicMock()
    request.client = MagicMock()
    request.client.host = client_host
    return request


@pytest.mark.unit
class TestPublicEndpoints:
    """Tests for public endpoint bypass."""

    @pytest.mark.parametrize(
        "path",
        ["/health", "/docs", "/openapi.json", "/"],
    )
    async def test_public_endpoints_bypass_auth(self, path: str) -> None:
        """Test that public endpoints skip authentication entirely."""
        request = _make_request(path=path)
        expected_response = MagicMock()
        call_next = AsyncMock(return_value=expected_response)

        middleware = AuthMiddleware(app=MagicMock())
        response = await middleware.dispatch(request, call_next)

        assert response is expected_response
        call_next.assert_called_once_with(request)

    async def test_public_prefix_docs_subpath(self) -> None:
        """Test that /docs/some-subpath also bypasses auth."""
        request = _make_request(path="/docs/oauth2-redirect")
        expected_response = MagicMock()
        call_next = AsyncMock(return_value=expected_response)

        middleware = AuthMiddleware(app=MagicMock())
        response = await middleware.dispatch(request, call_next)

        assert response is expected_response


@pytest.mark.unit
class TestTokenAuthentication:
    """Tests for license token introspection."""

    async def test_valid_license_token_sets_tenant_id(self) -> None:
        """Test that a valid token sets tenant_id on request.state."""
        request = _make_request(headers={"X-Tenant-Id": "Bearer valid-token-123"})
        expected_response = MagicMock()
        call_next = AsyncMock(return_value=expected_response)

        introspect_result = {
            "active": True,
            "tenant_id": "09c2dfc7-754b-5c37-a072-228e55c38cfb",
            "subject_id": "user-1",
        }

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value=introspect_result,
        ):
            middleware = AuthMiddleware(app=MagicMock())
            response = await middleware.dispatch(request, call_next)

        assert response is expected_response
        assert request.state.tenant_id == "09c2dfc7-754b-5c37-a072-228e55c38cfb"

    async def test_invalid_token_returns_401(self) -> None:
        """Test that an invalid/expired token returns 401."""
        request = _make_request(headers={"X-Tenant-Id": "Bearer expired-token"})
        call_next = AsyncMock()

        introspect_result = {"active": False, "reason": "expired"}

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value=introspect_result,
        ):
            middleware = AuthMiddleware(app=MagicMock())
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()

    async def test_introspection_returns_none_returns_401(self) -> None:
        """Test that failed introspection (None result) returns 401 for non-trusted."""
        request = _make_request(
            headers={"X-Tenant-Id": "Bearer some-token"},
            client_host="203.0.113.50",  # external IP
        )
        call_next = AsyncMock()

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value=None,
        ):
            middleware = AuthMiddleware(app=MagicMock())
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401


@pytest.mark.unit
@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestTrustedNetworkFallback:
    """Tests for trusted network default tenant fallback."""

    async def test_missing_token_trusted_network_fail_closed_default(self) -> None:
        """w2v_common 0.6.9: trusted-network unauth fails closed (401) by default."""
        request = _make_request(
            headers={},
            client_host="127.0.0.1",
        )
        call_next = AsyncMock()

        middleware = AuthMiddleware(app=MagicMock())
        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()

    async def test_missing_token_trusted_network_is_401_even_opted_in(self) -> None:
        """DELIBERATE STATUS CHANGE: the trusted-network lane no longer mints a tenant.

        This asserted that an in-cluster caller with NO credential was let
        through. It was let through because the middleware substituted
        ``config.auth.default_tenant_id`` (the nil-UUID sentinel) — w2v_common
        0.67.0 DELETED that setting, and the ruling behind it is that the
        network vouches for the CALLER, never for the TENANT. With nothing to
        substitute, an unidentified caller is a 401 wherever it comes from.

        The opt-in itself is not dead: the sibling
        ``..._x_tenant_id...`` test still proves that a trusted caller which
        NAMES a tenant is admitted. Only the tenant-less case changed.
        """
        request = _make_request(
            headers={},
            client_host="127.0.0.1",
        )
        call_next = AsyncMock(return_value=MagicMock())

        middleware = AuthMiddleware(app=MagicMock())
        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()

    async def test_missing_token_external_network_returns_401(self) -> None:
        """Test that missing token from external IP returns 401."""
        request = _make_request(
            headers={},
            client_host="203.0.113.50",
        )
        call_next = AsyncMock()

        middleware = AuthMiddleware(app=MagicMock())
        response = await middleware.dispatch(request, call_next)

        assert response.status_code == 401
        call_next.assert_not_called()


@pytest.mark.unit
class TestIntrospectionCache:
    """Tests for license introspection caching."""

    async def test_introspection_cache_hit_on_second_call(self) -> None:
        """Test that second introspection call uses cache instead of HTTP."""
        introspect_result = {
            "active": True,
            "tenant_id": "45c09406-e5a8-59d5-ab59-a84bdfe79d85",
            "subject_id": "user-cached",
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = introspect_result

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "w2v_common.auth.middleware.httpx.AsyncClient",
            return_value=mock_client,
        ):
            # First call -- should hit HTTP
            result1 = await _introspect_license("test-token-for-cache")
            assert result1 is not None
            assert result1["active"] is True

            # Second call -- should hit cache
            result2 = await _introspect_license("test-token-for-cache")
            assert result2 is not None
            assert result2["tenant_id"] == "45c09406-e5a8-59d5-ab59-a84bdfe79d85"

            # HTTP should only have been called once
            assert mock_client.post.call_count == 1


@pytest.mark.unit
@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestTrustedNetworkHelper:
    """Tests for _is_trusted_network helper function."""

    def test_localhost_is_trusted(self) -> None:
        assert _is_trusted_network("127.0.0.1") is True

    def test_private_network_is_trusted(self) -> None:
        # 192.168.0.0/16 is not a configured lane (SSR-proxy bypass fix
        # 2026-04-18); the configured 10/8 and 172.16/12 lanes are trusted.
        assert _is_trusted_network("192.168.1.100") is False
        assert _is_trusted_network("10.0.0.5") is True
        assert _is_trusted_network("172.16.0.1") is True

    def test_external_ip_is_not_trusted(self) -> None:
        assert _is_trusted_network("203.0.113.50") is False

    def test_none_host_is_not_trusted(self) -> None:
        assert _is_trusted_network(None) is False

    def test_invalid_host_is_not_trusted(self) -> None:
        assert _is_trusted_network("not-an-ip") is False
