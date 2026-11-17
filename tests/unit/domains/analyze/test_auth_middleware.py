"""Unit tests for auth middleware (src/auth/middleware.py)."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from w2v_common.auth.middleware import (
    AuthMiddleware,
    _introspect_license,
    _is_trusted_network,
)
from w2v_common.auth.models import User
from w2v_common.tenant import MissingTenantError

# ---------------------------------------------------------------------------
# _is_trusted_network
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestIsTrustedNetwork:
    def test_none_host_returns_false(self):
        assert _is_trusted_network(None) is False

    def test_empty_host_returns_false(self):
        assert _is_trusted_network("") is False

    def test_localhost_is_trusted(self):
        assert _is_trusted_network("127.0.0.1") is True

    def test_ipv6_localhost_is_trusted(self):
        assert _is_trusted_network("::1") is True

    def test_private_network_10(self):
        assert _is_trusted_network("10.0.0.1") is True

    def test_private_network_172(self):
        assert _is_trusted_network("172.16.0.1") is True

    def test_private_network_192_not_trusted_by_default(self):
        # 192.168.0.0/16 is deliberately not a configured lane: external callers routed through the
        # in-cluster SSR proxy present a 192.168.x source IP, so trusting
        # that range would re-introduce the 2026-04-18 SSR-proxy bypass.
        assert _is_trusted_network("192.168.1.1") is False

    def test_public_ip_is_not_trusted(self):
        assert _is_trusted_network("8.8.8.8") is False

    def test_invalid_ip_returns_false(self):
        assert _is_trusted_network("not-an-ip") is False


# ---------------------------------------------------------------------------
# _introspect_license
# ---------------------------------------------------------------------------


class TestIntrospectLicense:
    async def test_returns_data_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "active": True,
            "tenant_id": "9ea6a4da-9056-5a5a-bd14-9f92394a7b2c",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("w2v_common.auth.middleware.httpx.AsyncClient", return_value=mock_client):
            # Clear cache before test
            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()
            result = await _introspect_license("valid-token-123")
            assert result is not None
            assert result["active"] is True

    async def test_returns_none_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("w2v_common.auth.middleware.httpx.AsyncClient", return_value=mock_client):
            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()
            result = await _introspect_license("bad-token-abc")
            assert result is None

    async def test_returns_none_on_exception(self):
        import httpx

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("network error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("w2v_common.auth.middleware.httpx.AsyncClient", return_value=mock_client):
            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()
            result = await _introspect_license("error-token-xyz")
            assert result is None

    async def test_caches_active_license(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "active": True,
            "tenant_id": "50274e59-0eb3-5c0f-9b44-9f74c90a7f54",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("w2v_common.auth.middleware.httpx.AsyncClient", return_value=mock_client):
            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()
            result1 = await _introspect_license("cached-token-111")
            assert result1 is not None
            assert result1["active"] is True

            # Second call should use cache (client not called again)
            mock_client.post.reset_mock()
            result2 = await _introspect_license("cached-token-111")
            assert result2 is not None
            assert result2["active"] is True
            mock_client.post.assert_not_awaited()

    async def test_inactive_license_not_cached(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active": False, "reason": "expired"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("w2v_common.auth.middleware.httpx.AsyncClient", return_value=mock_client):
            from w2v_common.auth.middleware import _get_license_cache

            _get_license_cache().clear()
            result = await _introspect_license("inactive-token-222")
            assert result is not None
            assert result["active"] is False


# ---------------------------------------------------------------------------
# AuthMiddleware._extract_license_token
# ---------------------------------------------------------------------------


#: A structurally JWT-shaped bearer. Assembled from segments rather than
#: written as one literal: `_looks_like_jwt` only checks for three non-empty
#: dot-separated parts, and a literal would trip the detect-secrets JWT rule
#: for a value that is not a secret and was never signed.
JWT_SHAPED = ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJ1LTEifQ", "c2ln"])


class TestExtractLicenseToken:
    def _make_request(self, headers: dict) -> MagicMock:
        request = MagicMock()
        request.headers = headers
        return request

    def test_opaque_authorization_bearer_is_extracted_as_license(self):
        """w2v_common 0.74.0 deliberately REVERSED the old FR-401 invariant.

        `Authorization: Bearer <opaque>` is now a second licence lane, because
        the platform's `license-authz` ext-authz gate reads the token ONLY from
        `Authorization`. The old assertion here was `is None`.
        """
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"Authorization": "Bearer my-token-456"})
        assert mw._extract_license_token(request) == "my-token-456"

    def test_jwt_shaped_authorization_bearer_is_not_a_license_token(self):
        """The half of FR-401 that is still load-bearing.

        A JWT-shaped bearer must never reach licence introspection (the
        2026-04-26 prod 401s). 0.74.0 keeps this by gating the new
        Authorization lane on `not _looks_like_jwt(bearer)`.
        """
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"Authorization": f"Bearer {JWT_SHAPED}"})
        assert mw._extract_license_token(request) is None
        assert mw._extract_jwt_bearer(request) == JWT_SHAPED

    def test_x_tenant_id_bearer_wins_over_authorization_bearer(self):
        """Header PRECEDENCE, not just presence.

        `BaseServiceClient` puts the CALLER's token on `X-Tenant-Id` and its own
        service-account token on `Authorization`. If the new Authorization lane
        were checked first, every delegated call would be silently re-attributed
        to the service account's tenant.
        """
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request(
            {"X-Tenant-Id": "Bearer caller-token", "Authorization": "Bearer service-account-token"}
        )
        assert mw._extract_license_token(request) == "caller-token"

    def test_authorization_bearer_extracted_as_jwt_candidate(self):
        """The same Authorization header IS surfaced via _extract_jwt_bearer."""
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"Authorization": "Bearer my-token-456"})
        assert mw._extract_jwt_bearer(request) == "my-token-456"

    def test_extracts_from_x_tenant_id_bearer(self):
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"X-Tenant-Id": "Bearer tenant-token-789"})
        assert mw._extract_license_token(request) == "tenant-token-789"

    def test_returns_none_when_no_token(self):
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({})
        assert mw._extract_license_token(request) is None

    def test_x_tenant_id_non_bearer_not_extracted(self):
        mw = AuthMiddleware(app=MagicMock())
        request = self._make_request({"X-Tenant-Id": "ec809a39-c45c-5ffb-99c3-25cae07e518d"})
        assert mw._extract_license_token(request) is None


# ---------------------------------------------------------------------------
# AuthMiddleware.dispatch
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestAuthMiddlewareDispatch:
    def _make_request(
        self,
        path: str = "/api/analyze",
        headers: dict | None = None,
        client_host: str = "1.2.3.4",
    ) -> MagicMock:
        request = MagicMock()
        request.url.path = path
        request.headers = headers or {}
        # Make headers dict-like with .get()
        request.headers = MagicMock()
        request.headers.get = MagicMock(
            side_effect=lambda key, default=None: (headers or {}).get(key, default)
        )
        request.client = MagicMock()
        request.client.host = client_host
        request.state = MagicMock()
        return request

    async def test_public_endpoint_bypasses_auth(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=MagicMock())

        request = self._make_request(path="/health")
        await mw.dispatch(request, call_next)
        call_next.assert_awaited_once()

    async def test_public_prefix_bypasses_auth(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=MagicMock())

        request = self._make_request(path="/docs/openapi")
        await mw.dispatch(request, call_next)
        call_next.assert_awaited_once()

    async def test_valid_token_sets_user_state(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=MagicMock())

        request = self._make_request(
            headers={"X-Tenant-Id": "Bearer valid-token"},
            client_host="8.8.8.8",
        )

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value={
                "active": True,
                "tenant_id": "29161dbd-657f-515d-99b4-62d787def011",
                "subject_id": "user1",
            },
        ):
            await mw.dispatch(request, call_next)
            call_next.assert_awaited_once()
            # Verify user and tenant state was set
            assert request.state.tenant_id == "29161dbd-657f-515d-99b4-62d787def011"
            assert request.state.user is not None

    async def test_invalid_token_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()

        request = self._make_request(
            headers={"X-Tenant-Id": "Bearer bad-token"},
            client_host="8.8.8.8",
        )

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value={"active": False, "reason": "expired"},
        ):
            response = await mw.dispatch(request, call_next)
            assert response.status_code == 401
            call_next.assert_not_awaited()

    async def test_introspect_returns_none_gives_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()

        request = self._make_request(
            headers={"X-Tenant-Id": "Bearer failed-token"},
            client_host="8.8.8.8",
        )

        with patch(
            "w2v_common.auth.middleware._introspect_license",
            new_callable=AsyncMock,
            return_value=None,
        ):
            response = await mw.dispatch(request, call_next)
            assert response.status_code == 401

    async def test_trusted_network_no_token_fail_closed_default(self):
        """w2v_common 0.6.9 default flipped to fail-closed — trusted-net unauth → 401."""
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()

        request = self._make_request(
            headers={},
            client_host="127.0.0.1",
        )

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()

    async def test_trusted_network_without_a_tenant_is_401_even_opted_in(self):
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
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=MagicMock())

        request = self._make_request(
            headers={},
            client_host="127.0.0.1",
        )

        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            response = await mw.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()

    async def test_trusted_network_uses_x_tenant_id_header_opt_in(self):
        """Legacy X-Tenant-Id-as-tenant fallback only fires under opt-in."""
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock(return_value=MagicMock())

        request = self._make_request(
            headers={"X-Tenant-Id": "0437759e-7404-56f7-889a-f1cd12f072bd"},
            client_host="10.0.0.1",
        )

        with patch("w2v_common.auth.middleware._trust_incluster_identity", return_value=True):
            await mw.dispatch(request, call_next)
        assert request.state.tenant_id == "0437759e-7404-56f7-889a-f1cd12f072bd"

    async def test_untrusted_network_no_token_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()

        request = self._make_request(
            headers={},
            client_host="8.8.8.8",
        )

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401
        call_next.assert_not_awaited()

    async def test_no_client_returns_401(self):
        mw = AuthMiddleware(app=MagicMock())
        call_next = AsyncMock()

        request = self._make_request(headers={})
        request.client = None

        response = await mw.dispatch(request, call_next)
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


class TestUserModel:
    def test_effective_tenant_id_uses_tenant_id(self):
        # w2v_common 0.67.0: User.tenant_id is a uuid.UUID, not a slug.
        tenant = uuid.UUID("11111111-1111-4111-8111-111111111111")
        user = User(sub="sub1", tenant_id=tenant)
        assert user.effective_tenant_id == tenant

    def test_effective_tenant_id_refuses_to_fall_back_to_sub(self):
        """DELIBERATE BEHAVIOUR CHANGE, upstream in w2v_common 0.67.0.

        ``effective_tenant_id`` used to be ``self.tenant_id or self.sub`` — a
        self-named tenant, i.e. a sentinel by another name. It minted a tenant
        that was never registered, and because ``sub`` is a free-form string it
        also minted NON-UUID tenants that no downstream RLS bind or metered hop
        could use. It now raises, and this test pins that so nobody restores the
        fallback to "fix" a None.
        """
        user = User(sub="sub1", tenant_id=None)
        with pytest.raises(MissingTenantError):
            _ = user.effective_tenant_id

    def test_effective_tenant_id_returns_a_real_tenant_as_a_uuid(self):
        tenant = uuid.UUID("29161dbd-657f-515d-99b4-62d787def011")
        user = User(sub="sub1", tenant_id=tenant)
        assert user.effective_tenant_id == tenant
