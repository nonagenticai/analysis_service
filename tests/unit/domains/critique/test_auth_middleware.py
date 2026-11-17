"""Tests for auth middleware."""

import ipaddress
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import w2v_common.auth.middleware as auth_middleware
from w2v_common.auth.middleware import (
    PUBLIC_ENDPOINTS,
    PUBLIC_PREFIXES,
    AuthMiddleware,
    _introspect_license,
    _is_trusted_network,
)
from w2v_common.auth.models import User
from w2v_common.tenant import MissingTenantError


@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestIsTrustedNetwork:
    """Test _is_trusted_network helper."""

    def test_localhost_ipv4(self):
        assert _is_trusted_network("127.0.0.1") is True

    def test_localhost_ipv6(self):
        assert _is_trusted_network("::1") is True

    def test_private_10(self):
        assert _is_trusted_network("10.0.0.1") is True

    def test_private_172(self):
        assert _is_trusted_network("172.16.0.1") is True

    def test_private_192_not_trusted_by_default(self):
        # 192.168.0.0/16 is not among the configured lanes (SSR-proxy bypass
        # vector, 2026-04-18) and must not be trusted.
        assert _is_trusted_network("192.168.1.1") is False

    def test_public_ip(self):
        assert _is_trusted_network("8.8.8.8") is False

    def test_none(self):
        assert _is_trusted_network(None) is False

    def test_invalid_ip(self):
        assert _is_trusted_network("not-an-ip") is False


class TestIntrospectLicense:
    """Test _introspect_license function."""

    async def test_introspect_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "tenant_id": "7be17748-9778-562d-864c-2fd7b5598d5c",
            "subject_id": "u-456",
        }

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Clear cache before test
            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()

            result = await _introspect_license("valid-token")
        assert result is not None
        assert result["active"] is True
        assert result["tenant_id"] == "7be17748-9778-562d-864c-2fd7b5598d5c"

    async def test_introspect_inactive(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"active": False, "reason": "expired"}

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()

            result = await _introspect_license("inactive-token")
        assert result is not None
        assert result["active"] is False

    async def test_introspect_network_error(self):
        import httpx as _httpx

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=_httpx.ConnectError("fail"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()

            result = await _introspect_license("error-token")
        assert result is None

    async def test_introspect_caching(self):
        """Test that a second call uses the cache."""
        from w2v_common.auth.middleware import _get_license_cache

        _get_license_cache().clear()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "tenant_id": "50274e59-0eb3-5c0f-9b44-9f74c90a7f54",
        }

        with patch("w2v_common.auth.middleware.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result1 = await _introspect_license("cache-test-token")
            result2 = await _introspect_license("cache-test-token")

        assert result1 == result2
        # httpx should only be called once (second call uses cache)
        assert mock_client.post.await_count == 1


@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestAuthMiddlewareDispatch:
    """Test AuthMiddleware.dispatch method."""

    def _make_middleware(self):
        """Create AuthMiddleware with a dummy app.

        Constructed via the real ``__init__`` (not ``__new__``) so instance
        state added by newer w2v_common — e.g. ``_extra_public_endpoints``
        (0.33.0), which ``dispatch`` reads — is initialised.
        """
        return AuthMiddleware(MagicMock())

    async def test_public_endpoint_passes_through(self):
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/health"

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        await middleware.dispatch(request, call_next)
        call_next.assert_awaited_once()

    async def test_docs_prefix_passes_through(self):
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/docs/openapi"

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        await middleware.dispatch(request, call_next)
        call_next.assert_awaited_once()

    async def test_valid_bearer_token(self):
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {"X-Tenant-Id": "Bearer valid-token"}
        request.state = MagicMock()

        introspect_result = {
            "active": True,
            "tenant_id": "7be17748-9778-562d-864c-2fd7b5598d5c",
            "subject_id": "u-456",
        }

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value=introspect_result,
        ):
            await middleware.dispatch(request, call_next)

        call_next.assert_awaited_once()
        assert request.state.tenant_id == "7be17748-9778-562d-864c-2fd7b5598d5c"

    async def test_invalid_token(self):
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {"X-Tenant-Id": "Bearer bad-token"}
        request.client = MagicMock()
        request.client.host = "8.8.8.8"

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value={"active": False, "reason": "expired"},
        ):
            response = await middleware.dispatch(request, AsyncMock())

        assert response.status_code == 401

    async def test_trusted_network_no_token_fail_closed_default(self):
        """w2v_common 0.6.9: trusted-network unauth fails closed (401) by default."""
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.state = MagicMock()

        call_next = AsyncMock()

        response = await middleware.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()

    async def test_trusted_network_no_token_is_401_even_opted_in(self):
        """DELIBERATE STATUS CHANGE: the trusted-network lane no longer mints a tenant.

        This asserted that an in-cluster caller with NO credential was let
        through. It was let through only because the middleware substituted
        ``config.auth.default_tenant_id`` (the nil-UUID sentinel), which
        w2v_common 0.67.0 DELETED — the network vouches for the CALLER, never
        for the TENANT. With nothing to substitute, an unidentified caller is a
        401 wherever it comes from. The sibling
        ``test_trusted_network_with_tenant_header_opt_in`` still proves the
        opt-in admits a trusted caller that NAMES a tenant, so the lane itself
        is still covered; only the tenant-less case changed.
        """
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "127.0.0.1"
        request.state = MagicMock()

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            response = await middleware.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()

    async def test_trusted_network_with_tenant_header_opt_in(self):
        """Legacy X-Tenant-Id-as-tenant fallback: opt-in via AUTH_TRUST_INCLUSTER_IDENTITY=true."""
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {"X-Tenant-Id": "2a048c5b-d2ff-5e09-b2f2-30f08e82a39d"}
        request.client = MagicMock()
        request.client.host = "10.0.0.5"
        request.state = MagicMock()

        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            await middleware.dispatch(request, call_next)
        call_next.assert_awaited_once()
        assert request.state.tenant_id == "2a048c5b-d2ff-5e09-b2f2-30f08e82a39d"

    async def test_no_token_untrusted_network(self):
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "8.8.8.8"

        response = await middleware.dispatch(request, AsyncMock())
        assert response.status_code == 401

    async def test_bearer_in_x_tenant_id(self):
        middleware = self._make_middleware()
        request = MagicMock()
        request.url.path = "/api/critique"
        request.headers = {"X-Tenant-Id": "Bearer my-license-token"}
        request.state = MagicMock()
        request.client = MagicMock()
        request.client.host = "8.8.8.8"

        introspect_result = {
            "active": True,
            "tenant_id": "77cdd018-577a-5cb9-a16d-673a06659d4a",
            "subject_id": "s-1",
        }
        call_next = AsyncMock(return_value=MagicMock(status_code=200))

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value=introspect_result,
        ):
            await middleware.dispatch(request, call_next)

        call_next.assert_awaited_once()
        assert request.state.tenant_id == "77cdd018-577a-5cb9-a16d-673a06659d4a"


#: A structurally JWT-shaped bearer. Assembled from segments rather than
#: written as one literal: `_looks_like_jwt` only checks for three non-empty
#: dot-separated parts, and a literal would trip the detect-secrets JWT rule
#: for a value that is not a secret and was never signed.
JWT_SHAPED = ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJ1LTEifQ", "c2ln"])


class TestExtractLicenseToken:
    """Test _extract_license_token method."""

    def test_opaque_authorization_bearer_is_a_license_token(self):
        """w2v_common 0.74.0 deliberately REVERSED the old FR-401 invariant.

        `Authorization: Bearer <opaque>` is now a second licence lane, because
        the platform's `license-authz` ext-authz gate reads the token ONLY from
        `Authorization`. The old assertion here was `is None`.
        """
        middleware = AuthMiddleware.__new__(AuthMiddleware)
        request = MagicMock()
        request.headers = {"Authorization": "Bearer my-token"}
        assert middleware._extract_license_token(request) == "my-token"
        assert middleware._extract_jwt_bearer(request) == "my-token"

    def test_jwt_shaped_authorization_bearer_is_not_a_license_token(self):
        """The half of FR-401 that is still load-bearing: a JWT-shaped bearer
        must never reach licence introspection (the 2026-04-26 prod 401s).
        0.74.0 keeps this by gating the new lane on `not _looks_like_jwt(...)`.
        """
        middleware = AuthMiddleware.__new__(AuthMiddleware)
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {JWT_SHAPED}"}
        assert middleware._extract_license_token(request) is None
        assert middleware._extract_jwt_bearer(request) == JWT_SHAPED

    def test_x_tenant_id_bearer_wins_over_authorization_bearer(self):
        """`BaseServiceClient` puts the CALLER's token on `X-Tenant-Id` and its
        own service-account token on `Authorization`. Preferring the new
        Authorization lane would silently re-attribute every delegated call to
        the service account's tenant.
        """
        middleware = AuthMiddleware.__new__(AuthMiddleware)
        request = MagicMock()
        request.headers = {
            "X-Tenant-Id": "Bearer caller-token",
            "Authorization": "Bearer service-account-token",
        }
        assert middleware._extract_license_token(request) == "caller-token"

    def test_from_x_tenant_id_bearer(self):
        middleware = AuthMiddleware.__new__(AuthMiddleware)
        request = MagicMock()
        request.headers = {"X-Tenant-Id": "Bearer another-token", "Authorization": ""}
        assert middleware._extract_license_token(request) == "another-token"

    def test_no_token(self):
        middleware = AuthMiddleware.__new__(AuthMiddleware)
        request = MagicMock()
        request.headers = {}
        assert middleware._extract_license_token(request) is None

    def test_non_bearer_header(self):
        middleware = AuthMiddleware.__new__(AuthMiddleware)
        request = MagicMock()
        request.headers = {"Authorization": "Basic abc123"}
        assert middleware._extract_license_token(request) is None


class TestUserModel:
    """Test auth User model."""

    def test_user_creation(self):
        # w2v_common 0.67.0: User.tenant_id is a uuid.UUID, not a slug.
        tenant = uuid.UUID("22222222-2222-4222-8222-222222222222")
        user = User(sub="user-1", preferred_username="admin", tenant_id=tenant)
        assert user.sub == "user-1"
        assert user.preferred_username == "admin"
        assert user.tenant_id == tenant

    def test_effective_tenant_id_with_tenant(self):
        tenant = uuid.UUID("22222222-2222-4222-8222-222222222222")
        user = User(sub="user-1", tenant_id=tenant)
        assert user.effective_tenant_id == tenant

    def test_effective_tenant_id_refuses_to_fall_back_to_sub(self):
        """DELIBERATE BEHAVIOUR CHANGE, upstream in w2v_common 0.67.0.

        ``effective_tenant_id`` was ``self.tenant_id or self.sub``: a self-named
        tenant, i.e. a sentinel by another name. It minted a tenant that was
        never registered, and since ``sub`` is free-form it minted NON-UUID
        tenants no RLS bind or metered hop could use. It raises now; pinned so
        nobody restores the fallback to "fix" a None.
        """
        user = User(sub="user-1", tenant_id=None)
        with pytest.raises(MissingTenantError):
            _ = user.effective_tenant_id

    def test_effective_tenant_id_returns_a_real_tenant(self):
        tenant = uuid.UUID("29161dbd-657f-515d-99b4-62d787def011")
        assert User(sub="user-1", tenant_id=tenant).effective_tenant_id == tenant

    def test_user_default_username(self):
        user = User(sub="user-1")
        assert user.preferred_username == "anonymous"


class TestPublicEndpoints:
    """Test that PUBLIC_ENDPOINTS and PUBLIC_PREFIXES are correctly defined."""

    def test_health_is_public(self):
        assert "/health" in PUBLIC_ENDPOINTS

    def test_docs_is_public(self):
        assert "/docs" in PUBLIC_ENDPOINTS

    def test_health_prefix(self):
        assert "/health/" in PUBLIC_PREFIXES

    def test_trusted_networks_include_localhost(self, explicit_trusted_cidrs):
        """Configured lanes are what TRUSTED_NETWORKS holds — loopback included."""
        loopback = ipaddress.ip_address("127.0.0.1")
        assert len(auth_middleware.TRUSTED_NETWORKS) > 0
        assert any(loopback in net for net in auth_middleware.TRUSTED_NETWORKS)
