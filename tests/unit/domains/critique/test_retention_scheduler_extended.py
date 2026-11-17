"""Extended tests for retention and the scan scheduler — covers background loop paths."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.critique.services.retention import run_retention_loop
from src.domains.critique.services.scheduler import run_scheduler


class TestRetentionLoop:
    """Test run_retention_loop background loop."""

    async def test_retention_disabled(self):
        """Loop returns immediately when retention is disabled."""
        with patch("src.domains.critique.services.retention.get_settings") as ms:
            ms.return_value.retention.enabled = False
            # Should return immediately, not loop forever
            result = await run_retention_loop()
            assert result is None
            ms.assert_called_once()  # settings were checked

    async def test_retention_loop_runs_cleanup(self):
        """Loop runs cleanup then sleeps. We break the loop via CancelledError."""
        call_count = 0

        async def mock_cleanup(db):
            nonlocal call_count
            call_count += 1
            return {"jobs": 0, "findings": 0, "audit_logs": 0}

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.domains.critique.services.retention.get_settings") as ms,
            patch(
                "src.domains.critique.services.retention.cleanup_old_data", side_effect=mock_cleanup
            ),
            patch("src.shared.database.session.get_db_session", return_value=mock_db),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            ms.return_value.retention.enabled = True
            ms.return_value.retention.cleanup_interval_hours = 24
            ms.return_value.retention.job_retention_days = 90
            ms.return_value.retention.audit_retention_days = 365

            with pytest.raises(asyncio.CancelledError):
                await run_retention_loop()

        assert call_count == 1

    async def test_retention_loop_handles_error(self):
        """Loop continues after cleanup error."""
        call_count = 0

        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=False)

        async def mock_get_session():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("DB connection failed")
            raise asyncio.CancelledError

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB error"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.domains.critique.services.retention.get_settings") as ms,
            patch("src.shared.database.session.get_db_session", return_value=mock_cm),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            ms.return_value.retention.enabled = True
            ms.return_value.retention.cleanup_interval_hours = 24
            ms.return_value.retention.job_retention_days = 90
            ms.return_value.retention.audit_retention_days = 365

            with pytest.raises(asyncio.CancelledError):
                await run_retention_loop()


class TestRunScheduler:
    """Test run_scheduler background loop.

    (Atomic-claim / cross-tenant behaviour of ``check_and_run_schedules`` is
    covered against a real Postgres in ``test_scheduler_claim.py``.)
    """

    async def test_scheduler_disabled(self):
        """Loop returns immediately when scheduled scans disabled."""
        with patch("src.config.get_settings") as ms:
            ms.return_value.features.scheduled_scans = False
            result = await run_scheduler(MagicMock())
            assert result is None
            ms.assert_called_once()  # settings were checked

    async def test_scheduler_loop_runs(self):
        """Scheduler loop calls check_and_run_schedules(service) then sleeps."""
        mock_service = MagicMock()

        with (
            patch("src.config.get_settings") as ms,
            patch(
                "src.domains.critique.services.scheduler.check_and_run_schedules",
                new_callable=AsyncMock,
                return_value=0,
            ) as mock_check,
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            ms.return_value.features.scheduled_scans = True

            with pytest.raises(asyncio.CancelledError):
                await run_scheduler(mock_service)

        mock_check.assert_awaited_once_with(mock_service)

    async def test_scheduler_loop_error_handling(self):
        """Scheduler loop continues after errors."""
        with (
            patch("src.config.get_settings") as ms,
            patch(
                "src.domains.critique.services.scheduler.check_and_run_schedules",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB error"),
            ),
            patch("asyncio.sleep", side_effect=asyncio.CancelledError),
        ):
            ms.return_value.features.scheduled_scans = True

            with pytest.raises(asyncio.CancelledError):
                await run_scheduler(MagicMock())
