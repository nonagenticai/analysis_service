"""Unit tests for configuration management."""

from __future__ import annotations

import pytest

from src.config import (
    APISettings,
    DatabaseSettings,
    LoggingSettings,
    RedisSettings,
    Settings,
)


class TestDatabaseSettings:
    def test_url_required(self, monkeypatch) -> None:
        """DATABASE_URL has no localhost fallback (w2v_common 0.15.0+)."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(ValueError):
            DatabaseSettings()  # ty: ignore[missing-argument]  # why: deliberately omits required url to verify ValidationError

    def test_pool_settings(self) -> None:
        settings = DatabaseSettings(url="postgresql+psycopg://test:test@localhost:5432/test")
        assert settings.pool_timeout == 30
        assert settings.pool_recycle == 3600


class TestRedisSettings:
    def test_url_required(self, monkeypatch) -> None:
        """REDIS_URL has no localhost fallback (w2v_common 0.15.0+)."""
        monkeypatch.delenv("REDIS_URL", raising=False)
        with pytest.raises(ValueError):
            RedisSettings()  # ty: ignore[missing-argument]  # why: deliberately omits required url to verify ValidationError


class TestLoggingSettings:
    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        settings = LoggingSettings()
        assert settings.level == "INFO"
        assert settings.json_logs is False

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValueError):
            LoggingSettings(level="INVALID")

    def test_level_case_insensitive(self) -> None:
        settings = LoggingSettings(level="debug")
        assert settings.level == "DEBUG"


class TestAPISettings:
    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.delenv("API_PORT", raising=False)
        settings = APISettings()
        assert settings.port == 6303
        assert settings.debug is False

    def test_cors_parsing_list(self) -> None:
        settings = APISettings(cors_origins=["http://localhost", "http://127.0.0.1"])
        assert isinstance(settings.cors_origins, list)
        assert len(settings.cors_origins) == 2


class TestSettings:
    def test_aggregate_settings(self) -> None:
        settings = Settings()
        assert settings.app_name == "Analysis Service"
        assert hasattr(settings, "database")
        assert hasattr(settings, "redis")
        assert hasattr(settings, "api")

    def test_helper_methods(self, monkeypatch) -> None:
        monkeypatch.setenv("ENVIRONMENT", "development")
        settings = Settings()
        assert settings.is_development() is True
        assert settings.is_production() is False
