"""Extended tests for scheduler module.

``check_and_run_schedules`` now claims due schedules atomically via the
``claim_due_critique_schedules`` SECURITY DEFINER resolver (FOR UPDATE SKIP
LOCKED), so its behaviour is exercised against a real Postgres in
``test_scheduler_claim.py`` rather than with ORM-select mocks. This module keeps
the loop-control tests that don't need a database.
"""

from unittest.mock import MagicMock, patch

from src.domains.critique.services.scheduler import run_scheduler


class TestRunScheduler:
    """Test run_scheduler function."""

    async def test_scheduler_disabled(self):
        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value.features.scheduled_scans = False
            # Should return immediately
            result = await run_scheduler(MagicMock())
            assert result is None
