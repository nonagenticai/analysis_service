"""Unit tests for AuditMiddleware helpers."""

from src.shared.middleware.audit import _resource_from_path


class TestAuditHelpers:
    def test_resource_from_api_path(self):
        """_resource_from_path should extract resource name."""
        assert _resource_from_path("/api/analyze") == "analyze"
        assert _resource_from_path("/api/context/profiles") == "context"

    def test_resource_from_non_api_path(self):
        """_resource_from_path should return None for non-API paths."""
        assert _resource_from_path("/health") is None
        assert _resource_from_path("/") is None
