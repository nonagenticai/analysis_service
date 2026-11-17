"""Tests for runtime_config service."""

from unittest.mock import AsyncMock, patch

from src.domains.critique.services.runtime_config import (
    delete_runtime_value,
    get_all_overrides,
    get_runtime_value,
    get_scoring_weights,
    set_runtime_value,
)

REDIS_PATCH = "src.shared.cache.redis_client.get_redis_client"


class TestRuntimeConfig:
    async def test_get_runtime_value(self, mock_redis):
        mock_redis.hget = AsyncMock(return_value="42")
        with patch(REDIS_PATCH, return_value=mock_redis):
            result = await get_runtime_value("scoring", "critical_weight")
        assert result == "42"
        mock_redis.hget.assert_called_once()

    async def test_get_runtime_value_not_found(self, mock_redis):
        mock_redis.hget = AsyncMock(return_value=None)
        with patch(REDIS_PATCH, return_value=mock_redis):
            result = await get_runtime_value("scoring", "unknown")
        assert result is None

    async def test_set_runtime_value(self, mock_redis):
        with patch(REDIS_PATCH, return_value=mock_redis):
            await set_runtime_value("scoring", "critical_weight", "25")
        mock_redis.hset.assert_called_once()

    async def test_delete_runtime_value(self, mock_redis):
        mock_redis.hdel = AsyncMock(return_value=1)
        with patch(REDIS_PATCH, return_value=mock_redis):
            result = await delete_runtime_value("scoring", "critical_weight")
        assert result is True

    async def test_get_all_overrides(self, mock_redis):
        mock_redis.hgetall = AsyncMock(
            return_value={
                "scoring.critical_weight": "25",
                "scoring.high_weight": "10",
            }
        )
        with patch(REDIS_PATCH, return_value=mock_redis):
            result = await get_all_overrides()
        assert "scoring" in result
        assert result["scoring"]["critical_weight"] == "25"

    async def test_get_scoring_weights(self, mock_redis):
        mock_redis.hgetall = AsyncMock(return_value={})
        with (
            patch(REDIS_PATCH, return_value=mock_redis),
            patch("src.config.get_settings") as mock_settings,
        ):
            # Configure the scoring sub-settings with expected weight attributes
            scoring = mock_settings.return_value.scoring
            scoring.a11y_weight = 0.25
            scoring.security_weight = 0.25
            scoring.performance_weight = 0.20
            scoring.seo_weight = 0.15
            scoring.ux_weight = 0.15
            scoring.critical_weight = 30
            scoring.high_weight = 15
            scoring.medium_weight = 7
            scoring.low_weight = 3
            scoring.info_weight = 0

            weights = await get_scoring_weights()
        assert "critical_weight" in weights
        assert "a11y_weight" in weights
