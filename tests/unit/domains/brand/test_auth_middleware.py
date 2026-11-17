"""Tests for auth middleware."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from w2v_common.auth.middleware import (
    PUBLIC_ENDPOINTS,
    AuthMiddleware,
    _is_trusted_network,
)


class TestTrustDefaultIsEmpty:
    """w2v-common >= 0.89.0: with TRUSTED_CIDRS unset, NO peer is trusted.

    Every other trusted-network test here opts in via the
    ``explicit_trusted_cidrs`` fixture; this one pins the default itself, so a
    regression back to an implicit loopback/private-range default goes red.
    """

    def test_unset_env_trusts_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        import w2v_common.auth.middleware as auth_middleware

        saved = os.environ.get("TRUSTED_CIDRS")
        monkeypatch.delenv("TRUSTED_CIDRS", raising=False)
        try:
            mod = importlib.reload(auth_middleware)
            assert mod.TRUSTED_NETWORKS == []
            for host in ("127.0.0.1", "::1", "10.0.0.1", "10.42.5.10", "172.16.0.1", "192.168.1.1"):
                assert mod._is_trusted_network(host) is False, host
        finally:
            if saved is not None:
                os.environ["TRUSTED_CIDRS"] = saved
            importlib.reload(auth_middleware)


@pytest.mark.usefixtures("explicit_trusted_cidrs")
class TestIsTrustedNetwork:
    def test_localhost_ipv4(self) -> None:
        assert _is_trusted_network("127.0.0.1") is True

    def test_localhost_ipv6(self) -> None:
        assert _is_trusted_network("::1") is True

    def test_private_10(self) -> None:
        assert _is_trusted_network("10.0.0.1") is True

    def test_private_172(self) -> None:
        assert _is_trusted_network("172.16.0.1") is True

    def test_private_192_not_trusted_by_default(self) -> None:
        # 192.168.0.0/16 is not among the configured lanes (SSR-proxy bypass
        # vector, 2026-04-18) and must not be trusted.
        assert _is_trusted_network("192.168.1.1") is False

    def test_public_ip(self) -> None:
        assert _is_trusted_network("8.8.8.8") is False

    def test_none(self) -> None:
        assert _is_trusted_network(None) is False

    def test_empty(self) -> None:
        assert _is_trusted_network("") is False

    def test_invalid(self) -> None:
        assert _is_trusted_network("not-an-ip") is False


class TestPublicEndpoints:
    def test_health_is_public(self) -> None:
        assert "/health" in PUBLIC_ENDPOINTS

    def test_docs_is_public(self) -> None:
        assert "/docs" in PUBLIC_ENDPOINTS

    def test_root_is_public(self) -> None:
        assert "/" in PUBLIC_ENDPOINTS


#: A structurally JWT-shaped bearer. Assembled from segments rather than
#: written as one literal: `_looks_like_jwt` only checks for three non-empty
#: dot-separated parts, and a literal would trip the detect-secrets JWT rule
#: for a value that is not a secret and was never signed.
JWT_SHAPED = ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJ1LTEifQ", "c2ln"])


class TestExtractLicenseToken:
    def test_opaque_authorization_bearer_is_a_license_token(self) -> None:
        """w2v_common 0.74.0 deliberately REVERSED the old FR-401 invariant.

        `Authorization: Bearer <opaque>` is now a second licence lane, because
        the platform's `license-authz` ext-authz gate reads the token ONLY from
        `Authorization`. The old assertion here was `is None`.
        """
        middleware = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {"Authorization": "Bearer test-token-123"}
        assert middleware._extract_license_token(request) == "test-token-123"
        assert middleware._extract_jwt_bearer(request) == "test-token-123"

    def test_jwt_shaped_authorization_bearer_is_not_a_license_token(self) -> None:
        """The half of FR-401 that is still load-bearing: a JWT-shaped bearer
        must never reach licence introspection (the 2026-04-26 prod 401s).
        0.74.0 keeps this by gating the new lane on `not _looks_like_jwt(...)`.
        """
        middleware = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {"Authorization": f"Bearer {JWT_SHAPED}"}
        assert middleware._extract_license_token(request) is None
        assert middleware._extract_jwt_bearer(request) == JWT_SHAPED

    def test_x_tenant_id_bearer_wins_over_authorization_bearer(self) -> None:
        """`BaseServiceClient` puts the CALLER's token on `X-Tenant-Id` and its
        own service-account token on `Authorization`. Preferring the new
        Authorization lane would silently re-attribute every delegated call to
        the service account's tenant.
        """
        middleware = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {
            "X-Tenant-Id": "Bearer caller-token",
            "Authorization": "Bearer service-account-token",
        }
        assert middleware._extract_license_token(request) == "caller-token"

    def test_bearer_from_x_tenant_id(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {"X-Tenant-Id": "Bearer license-token"}
        token = middleware._extract_license_token(request)
        assert token == "license-token"

    def test_no_token(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {}
        token = middleware._extract_license_token(request)
        assert token is None

    def test_non_bearer_x_tenant_id(self) -> None:
        middleware = AuthMiddleware(app=MagicMock())
        request = MagicMock()
        request.headers = {"X-Tenant-Id": "just-a-tenant-id"}
        token = middleware._extract_license_token(request)
        assert token is None
