"""Extended tests for auth/middleware.py -- covers dispatch method, license introspection, trusted network paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from w2v_common.auth.middleware import (
    AuthMiddleware,
    _get_license_cache,
    _introspect_license,
)


class TestIntrospectLicense:
    """Test _introspect_license function."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        _get_license_cache().clear()
        yield
        _get_license_cache().clear()

    @pytest.mark.asyncio
    async def test_valid_token_cached(self) -> None:
        response_data = {
            "active": True,
            "tenant_id": "c09a6317-09d7-59cb-8089-0ab33ec2293d",
            "subject_id": "user-1",
        }
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_class:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _introspect_license("valid-token-123")
            assert result is not None
            assert result["active"] is True
            assert result["tenant_id"] == "c09a6317-09d7-59cb-8089-0ab33ec2293d"

    @pytest.mark.asyncio
    async def test_invalid_token_not_cached(self) -> None:
        response_data = {"active": False, "reason": "expired"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_class:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _introspect_license("invalid-token")
            assert result is not None
            assert result["active"] is False

    @pytest.mark.asyncio
    async def test_returns_cached_result(self) -> None:
        response_data = {"active": True, "tenant_id": "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c"}
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = response_data

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_class:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

            r1 = await _introspect_license("cache-test-token")
            r2 = await _introspect_license("cache-test-token")
            assert r1 is r2
            # Only 1 HTTP call
            assert mock_instance.post.call_count == 1

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self) -> None:
        import httpx as _httpx

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_class:
            mock_instance = AsyncMock()
            mock_instance.post.side_effect = _httpx.ConnectError("Network error")
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _introspect_license("error-token")
            assert result is None

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_class:
            mock_instance = AsyncMock()
            mock_instance.post.return_value = mock_response
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _introspect_license("server-error-token")
            assert result is None


@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestAuthMiddlewareDispatch:
    """Test AuthMiddleware.dispatch method."""

    def _make_request(
        self,
        path: str = "/api/brand/extract",
        headers: dict | None = None,
        client_host: str = "8.8.8.8",
    ) -> MagicMock:
        request = MagicMock()
        request.url.path = path
        request.headers = headers or {}
        request.client = MagicMock()
        request.client.host = client_host
        request.state = MagicMock()
        return request

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        _get_license_cache().clear()
        yield
        _get_license_cache().clear()

    @pytest.mark.asyncio
    async def test_public_endpoint_passes_through(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(path="/health")
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_public_prefix_passes_through(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(path="/docs/openapi")
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_token_passes_through(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(headers={"X-Tenant-Id": "Bearer valid-token"})
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
        ) as mock_introspect:
            mock_introspect.return_value = {
                "active": True,
                "tenant_id": "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c",
                "subject_id": "user-1",
            }
            await middleware.dispatch(request, call_next)
            call_next.assert_called_once()
            assert request.state.tenant_id == "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(headers={"X-Tenant-Id": "Bearer invalid-token"})
        call_next = AsyncMock()

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
        ) as mock_introspect:
            mock_introspect.return_value = {"active": False, "reason": "expired"}
            result = await middleware.dispatch(request, call_next)
            assert result.status_code == 401
            call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_introspect_returns_none_returns_401(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(headers={"X-Tenant-Id": "Bearer failing-token"})
        call_next = AsyncMock()

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
        ) as mock_introspect:
            mock_introspect.return_value = None
            result = await middleware.dispatch(request, call_next)
            assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_trusted_network_no_token_fail_closed_default(self) -> None:
        """w2v_common 0.6.9: trusted-network unauth fails closed (401) by default."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(client_host="10.0.0.100")
        call_next = AsyncMock()

        result = await middleware.dispatch(request, call_next)
        assert result.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_trusted_network_no_token_is_401_even_opted_in(self) -> None:
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
        middleware = AuthMiddleware(app=MagicMock())
        # 10.0.0.0/8 is in the configured trust lanes; 192.168/16 is not (see
        # SSR-proxy bypass fix 2026-04-18 in w2v-common).
        request = self._make_request(client_host="10.0.0.100")
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            response = await middleware.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_trusted_network_with_x_tenant_id_opt_in(self) -> None:
        """Legacy X-Tenant-Id-as-tenant fallback only fires under opt-in."""
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(
            headers={"X-Tenant-Id": "2a048c5b-d2ff-5e09-b2f2-30f08e82a39d"},
            client_host="10.0.0.1",
        )
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            await middleware.dispatch(request, call_next)
        call_next.assert_called_once()
        assert request.state.tenant_id == "2a048c5b-d2ff-5e09-b2f2-30f08e82a39d"

    @pytest.mark.asyncio
    async def test_untrusted_network_no_token_returns_401(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(client_host="8.8.8.8")
        call_next = AsyncMock()

        result = await middleware.dispatch(request, call_next)
        assert result.status_code == 401
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_client_returns_401(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request()
        request.client = None
        call_next = AsyncMock()

        result = await middleware.dispatch(request, call_next)
        assert result.status_code == 401

    @pytest.mark.asyncio
    async def test_trailing_slash_stripped(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(path="/health/")
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        await middleware.dispatch(request, call_next)
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_token_from_x_tenant_id_bearer(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = self._make_request(headers={"X-Tenant-Id": "Bearer license-via-x-tenant"})
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
        ) as mock_introspect:
            mock_introspect.return_value = {
                "active": True,
                "tenant_id": "acff4fd0-f60e-5ddc-be80-4a17dd04f06b",
                "subject_id": "u2",
            }
            await middleware.dispatch(request, call_next)
            call_next.assert_called_once()
            mock_introspect.assert_called_once_with("license-via-x-tenant")
