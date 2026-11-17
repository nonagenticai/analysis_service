"""Tests for scheduler service."""

from datetime import UTC, datetime, timedelta

from src.domains.critique.services.scheduler import CRON_PRESETS, next_run_from_cron


class TestScheduler:
    def test_next_run_hourly(self):
        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = next_run_from_cron("@hourly", now)
        assert result == now + timedelta(hours=1)

    def test_next_run_daily(self):
        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = next_run_from_cron("@daily", now)
        assert result == now + timedelta(days=1)

    def test_next_run_weekly(self):
        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = next_run_from_cron("@weekly", now)
        assert result == now + timedelta(weeks=1)

    def test_next_run_every_6h(self):
        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = next_run_from_cron("@every_6h", now)
        assert result == now + timedelta(hours=6)

    def test_next_run_unknown_defaults_daily(self):
        now = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = next_run_from_cron("unknown_expr", now)
        assert result == now + timedelta(days=1)

    def test_next_run_no_last_run(self):
        result = next_run_from_cron("@hourly")
        assert result > datetime.now(UTC)

    def test_cron_presets_exist(self):
        assert "@hourly" in CRON_PRESETS
        assert "@daily" in CRON_PRESETS
        assert "@weekly" in CRON_PRESETS
        assert "@every_6h" in CRON_PRESETS
        assert "@every_12h" in CRON_PRESETS
