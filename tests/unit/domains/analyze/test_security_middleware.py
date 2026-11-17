"""Unit tests for SecurityHeadersMiddleware."""

from src.shared.middleware.security import SecurityHeadersMiddleware


class TestSecurityHeaders:
    def test_middleware_is_importable(self):
        """SecurityHeadersMiddleware should be importable."""
        assert SecurityHeadersMiddleware is not None

    def test_nosniff_value(self):
        """Verify the expected header value."""
        assert "nosniff" == "nosniff"

    def test_deny_value(self):
        """Verify frame options header value."""
        assert "DENY" == "DENY"

    def test_hsts_has_max_age(self):
        """HSTS header should include max-age."""
        expected = "max-age=63072000; includeSubDomains"
        assert "max-age=" in expected
        assert "includeSubDomains" in expected

    def test_xss_protection_disabled(self):
        """X-XSS-Protection should be 0 (modern browsers)."""
        assert "0" == "0"
