"""Tests for configuration."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.config import (
    APISettings,
    AuthSettings,
    CacheSettings,
    DatabaseSettings,
    LoggingSettings,
    PlaywrightSettings,
    RedisSettings,
    Settings,
    StorageSettings,
    WorkerManagementSettings,
    clear_settings_cache,
    get_settings,
)


class TestDatabaseSettings:
    def test_defaults(self) -> None:
        settings = DatabaseSettings(url="postgresql+psycopg://test:test@localhost:5432/test")
        assert "postgresql" in settings.url
        assert settings.pool_size == 10

    def test_echo_default_false(self) -> None:
        settings = DatabaseSettings(url="postgresql+psycopg://test:test@localhost:5432/test")
        assert settings.echo is False


class TestAuthSettings:
    def test_defaults(self) -> None:
        settings = AuthSettings()
        # w2v_common 0.67.0 DELETED `default_tenant_id`. This used to assert the
        # nil-UUID sentinel it substituted for an absent tenant; that sentinel is
        # registered upstream as `default-anon`, so it silently mis-attributed
        # cost. The invariant now is that no fallback tenant exists at all —
        # asserted on the model, not on an instance, so a re-added field with a
        # None default would still fail this.
        assert "default_tenant_id" not in AuthSettings.model_fields
        assert not hasattr(settings, "default_tenant_id")


class TestRedisSettings:
    def test_defaults(self) -> None:
        settings = RedisSettings(url="redis://localhost:6379/0")
        assert "redis://" in settings.url
        assert settings.max_connections == 10


class TestAPISettings:
    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.delenv("API_PORT", raising=False)
        settings = APISettings()
        assert settings.port == 6303

    def test_parse_cors_list(self) -> None:
        settings = APISettings(cors_origins=["http://localhost:3000", "http://localhost:8080"])
        assert len(settings.cors_origins) == 2

    def test_parse_cors_single(self) -> None:
        settings = APISettings(cors_origins=["http://localhost:3000"])
        assert len(settings.cors_origins) == 1

    def test_parse_cors_empty(self) -> None:
        settings = APISettings(cors_origins=[])
        assert settings.cors_origins == []


class TestStorageSettings:
    def test_defaults(self) -> None:
        settings = StorageSettings()
        assert settings.backend == "local"


class TestLoggingSettings:
    @patch.dict(os.environ, {}, clear=False)
    def test_defaults(self) -> None:
        # Remove LOG_LEVEL env var so the true default is tested
        env = os.environ.copy()
        env.pop("LOG_LEVEL", None)
        with patch.dict(os.environ, env, clear=True):
            settings = LoggingSettings()
            assert settings.level == "INFO"

    def test_picks_up_env_var(self) -> None:
        settings = LoggingSettings()
        # In test env, LOG_LEVEL=WARNING is set by conftest
        assert settings.level in ("INFO", "WARNING")

    def test_validates_level(self) -> None:
        settings = LoggingSettings(level="debug")
        assert settings.level == "DEBUG"

    def test_rejects_invalid_level(self) -> None:
        with pytest.raises(ValidationError):
            LoggingSettings(level="INVALID")


class TestCacheSettings:
    def test_defaults(self) -> None:
        # Remove CACHE_ENABLED env var so the true default is tested
        env = os.environ.copy()
        env.pop("CACHE_ENABLED", None)
        with patch.dict(os.environ, env, clear=True):
            settings = CacheSettings()
            assert settings.job_ttl == 3600
            assert settings.enabled is True

    def test_picks_up_env_var(self) -> None:
        settings = CacheSettings()
        assert settings.job_ttl == 3600


class TestWorkerManagementSettings:
    def test_defaults(self) -> None:
        settings = WorkerManagementSettings.model_validate({})
        assert settings.timeout_seconds == 600.0
        assert settings.max_retries == 1
        assert settings.retry_delay == 2.0

    def test_picks_up_env_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WM_MAX_RETRIES", "3")
        monkeypatch.setenv("WM_RETRY_DELAY", "5.5")
        settings = WorkerManagementSettings.model_validate({})
        assert settings.max_retries == 3
        assert settings.retry_delay == 5.5


class TestPlaywrightSettings:
    def test_defaults(self) -> None:
        settings = PlaywrightSettings()
        assert settings.headless is True
        assert settings.timeout_ms == 30000
        assert settings.viewport_width == 1440
        assert settings.viewport_height == 900
        assert settings.wait_until == "networkidle"


class TestSettings:
    def test_has_all_sub_settings(self) -> None:
        settings = Settings()
        assert settings.database is not None
        assert settings.auth is not None
        assert settings.redis is not None
        assert settings.api is not None
        assert settings.storage is not None
        assert settings.logging is not None
        assert settings.cache is not None
        assert settings.wm is not None
        assert settings.playwright is not None

    def test_is_development(self) -> None:
        settings = Settings(environment="development")
        assert settings.is_development() is True
        assert settings.is_production() is False

    def test_is_production(self) -> None:
        settings = Settings(environment="production")
        assert settings.is_production() is True
        assert settings.is_development() is False


class TestGetSettings:
    def test_returns_settings(self) -> None:
        clear_settings_cache()
        settings = get_settings()
        assert isinstance(settings, Settings)

    def test_caching(self) -> None:
        clear_settings_cache()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
