"""Tests for configuration module."""

import pytest

from src.config import (
    APISettings,
    AuthSettings,
    CacheSettings,
    CGHSettings,
    DatabaseSettings,
    ExternalToolSettings,
    FeatureFlags,
    LoggingSettings,
    RateLimitSettings,
    RedisSettings,
    RetentionSettings,
    ScannerSettings,
    ScoringSettings,
    Settings,
    StorageSettings,
    WebhookSettings,
    clear_settings_cache,
    get_settings,
)


class TestDatabaseSettings:
    def test_defaults(self):
        s = DatabaseSettings(url="postgresql+psycopg://test:test@localhost:5432/test")
        assert "postgresql" in s.url
        assert s.pool_size == 10
        assert s.max_overflow == 20
        assert s.pool_timeout == 30
        assert s.pool_recycle == 3600
        assert s.echo is False


class TestAuthSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        s = AuthSettings()
        assert s.enabled is True
        assert s.ssl_verify is True
        # w2v_common 0.67.0 DELETED `default_tenant_id`; see the twin assertion
        # in tests/unit/domains/brand/test_config.py. An absent tenant must now
        # FAIL LOUD rather than be substituted with the nil-UUID sentinel.
        assert "default_tenant_id" not in AuthSettings.model_fields
        assert not hasattr(s, "default_tenant_id")


class TestRedisSettings:
    def test_defaults(self):
        s = RedisSettings(url="redis://localhost:6379/0")
        assert "redis" in s.url
        assert s.max_connections == 10
        assert s.decode_responses is True


class TestAPISettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("API_PORT", raising=False)
        s = APISettings()
        assert s.port == 6303
        assert s.debug is False
        assert s.reload is False
        assert s.cors_allow_credentials is True

    def test_parse_list_from_list(self):
        s = APISettings(cors_origins=["http://a.com", "http://b.com"])
        assert len(s.cors_origins) == 2
        assert "http://a.com" in s.cors_origins
        assert "http://b.com" in s.cors_origins

    def test_parse_list_empty(self):
        s = APISettings(cors_origins=[])
        assert s.cors_origins == []

    def test_parse_list_single(self):
        s = APISettings(cors_origins=["http://a.com"])
        assert s.cors_origins == ["http://a.com"]

    def test_parse_methods_from_list(self):
        s = APISettings(cors_allow_methods=["GET", "POST"])
        assert "GET" in s.cors_allow_methods
        assert "POST" in s.cors_allow_methods


class TestStorageSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("STORAGE_LOCAL_PATH", raising=False)
        s = StorageSettings()
        assert s.backend == "local"
        assert s.local_path == "/app/data"
        assert s.minio_secure is False


class TestLoggingSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        s = LoggingSettings()
        assert s.level == "INFO"
        assert s.json_logs is False

    def test_validate_level_valid(self):
        s = LoggingSettings(level="debug")
        assert s.level == "DEBUG"

    def test_validate_level_invalid(self):
        with pytest.raises(ValueError, match="Invalid log level"):
            LoggingSettings(level="TRACE")


class TestScannerSettings:
    def test_defaults(self):
        s = ScannerSettings()
        assert s.seo_timeout == 30
        assert s.security_timeout == 30
        assert s.overall_timeout == 300
        assert s.max_retries == 2


class TestRateLimitSettings:
    def test_defaults(self):
        s = RateLimitSettings()
        assert s.enabled is False
        assert s.hourly_limit == 60
        assert s.daily_limit == 500


class TestCacheSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("CACHE_ENABLED", raising=False)
        s = CacheSettings()
        assert s.enabled is True
        assert s.job_ttl == 3600


class TestWebhookSettings:
    def test_defaults(self):
        s = WebhookSettings()
        assert s.enabled is True
        assert s.max_retries == 3
        assert s.timeout == 10


class TestFeatureFlags:
    def test_defaults(self):
        f = FeatureFlags()
        assert f.batch_scan is True
        assert f.sse_progress is True
        assert f.pdf_reports is True
        assert f.html_reports is True
        assert f.analytics is True
        assert f.export_csv is True
        assert f.export_sarif is True
        assert f.scheduled_scans is True
        assert f.batch_reviews is True


class TestScoringSettings:
    def test_defaults(self):
        s = ScoringSettings()
        assert s.critical_weight == 30
        assert s.high_weight == 15
        assert s.medium_weight == 7
        assert s.low_weight == 3
        assert s.info_weight == 0
        assert s.a11y_weight == 0.25
        assert s.security_weight == 0.25
        assert s.performance_weight == 0.20
        assert s.seo_weight == 0.15
        assert s.ux_weight == 0.15


class TestExternalToolSettings:
    def test_defaults(self):
        s = ExternalToolSettings()
        assert s.enabled is True
        assert s.axe_enabled is True
        assert s.lighthouse_enabled is False
        assert s.zap_enabled is False
        assert s.nuclei_enabled is False


class TestRetentionSettings:
    def test_defaults(self):
        s = RetentionSettings()
        assert s.enabled is False
        assert s.job_retention_days == 90
        assert s.audit_retention_days == 365
        assert s.cleanup_interval_hours == 24


class TestCGHSettings:
    def test_defaults(self):
        s = CGHSettings()
        assert s.api_url == ""
        assert s.api_key == ""
        assert s.timeout == 120
        assert s.enabled is False


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.app_name == "Analysis Service"
        assert isinstance(s.database, DatabaseSettings)
        assert isinstance(s.auth, AuthSettings)
        assert isinstance(s.redis, RedisSettings)
        assert isinstance(s.api, APISettings)
        assert isinstance(s.features, FeatureFlags)
        assert isinstance(s.scoring, ScoringSettings)

    def test_get_database_url(self):
        s = Settings()
        assert "postgresql" in s.get_database_url()

    def test_get_redis_url(self):
        s = Settings()
        assert "redis" in s.get_redis_url()

    def test_is_production(self):
        s = Settings(environment="production")
        assert s.is_production() is True
        assert s.is_development() is False

    def test_is_development(self):
        s = Settings(environment="development")
        assert s.is_development() is True
        assert s.is_production() is False


class TestGetSettings:
    def test_returns_settings_instance(self):
        clear_settings_cache()
        s = get_settings()
        assert isinstance(s, Settings)

    def test_is_cached(self):
        clear_settings_cache()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_clear_settings_cache(self):
        clear_settings_cache()
        get_settings()
        clear_settings_cache()
        s2 = get_settings()
        # After clearing, a new instance should be created
        # (they might be equal but not necessarily the same object)
        assert isinstance(s2, Settings)
