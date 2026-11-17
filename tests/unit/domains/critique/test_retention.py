"""Tests for retention service."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.domains.critique.services.retention import cleanup_old_data


class TestRetention:
    async def test_cleanup_old_data(self, mock_db_session):
        # cleanup_old_data now calls the prune_critique_data SECURITY DEFINER resolver
        # (cross-tenant, bypasses RLS) and reads its jsonb counts.
        mock_result = MagicMock()
        mock_result.scalar.return_value = {"jobs": 5, "findings": 3, "audit_logs": 2}
        mock_db_session.execute = AsyncMock(return_value=mock_result)
        mock_db_session.commit = AsyncMock()

        with patch("src.domains.critique.services.retention.get_settings") as mock_settings:
            mock_settings.return_value.retention.job_retention_days = 90
            mock_settings.return_value.retention.audit_retention_days = 365
            counts = await cleanup_old_data(mock_db_session)

        assert counts == {"jobs": 5, "findings": 3, "audit_logs": 2}
        mock_db_session.execute.assert_awaited_once()
