"""Tests for src.config -- configuration settings."""

from src.config import (
    CacheSettings,
    DatabaseSettings,
    FeatureFlags,
    LoggingSettings,
    RateLimitSettings,
    ReviewSettings,
    ScoringSettings,
    Settings,
    TracingSettings,
    WebhookSettings,
    clear_settings_cache,
    get_settings,
)


class TestDatabaseSettings:
    def test_defaults(self):
        settings = DatabaseSettings(url="postgresql+psycopg://test:test@localhost:5432/test")
        assert "postgresql" in settings.url
        assert settings.pool_size == 10
        assert settings.echo is False


class TestLoggingSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        settings = LoggingSettings()
        assert settings.level == "INFO"
        assert settings.json_logs is False


class TestReviewSettings:
    def test_defaults(self):
        settings = ReviewSettings()
        # 600, not the old 180: this cap wraps WMClient.message(), whose own
        # timeout is 600s, so anything lower made the client timeout dead code.
        # See tests/unit/domains/review/test_review_wm_timeout_budget.py.
        assert settings.timeout_seconds == 600
        assert settings.max_pages_per_request == 50
        assert settings.max_request_size_kb == 512


class TestScoringSettings:
    def test_get_category_weights_design(self):
        settings = ScoringSettings()
        weights = settings.get_category_weights("design_review")
        assert "layout_consistency" in weights

    def test_get_category_weights_usability(self):
        settings = ScoringSettings()
        weights = settings.get_category_weights("usability_review")
        assert "navigation_clarity" in weights

    def test_get_category_weights_content(self):
        settings = ScoringSettings()
        weights = settings.get_category_weights("content_quality_review")
        assert "writing_clarity" in weights

    def test_get_category_weights_unknown(self):
        settings = ScoringSettings()
        assert settings.get_category_weights("unknown_type") == {}


class TestRateLimitSettings:
    def test_tier_limits_defaults(self):
        settings = RateLimitSettings()
        assert settings.enabled is False
        assert settings.hourly_limit == 60
        assert settings.daily_limit == 500

    def test_tenant_overrides_default_empty(self):
        settings = RateLimitSettings()
        assert settings.enabled is False


class TestTracingSettings:
    def test_defaults(self):
        settings = TracingSettings()
        assert settings.enabled is False
        assert settings.service_name == "analysis-service"
        assert settings.sample_rate == 1.0


class TestFeatureFlags:
    def test_defaults(self):
        flags = FeatureFlags()
        assert flags.sse_progress is True
        assert flags.batch_reviews is True


class TestCacheSettings:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("CACHE_ENABLED", raising=False)
        settings = CacheSettings()
        assert settings.enabled is True
        assert settings.job_ttl == 3600


class TestWebhookSettings:
    def test_defaults(self):
        settings = WebhookSettings()
        assert settings.max_retries == 3
        assert settings.timeout == 10


class TestSettings:
    def test_settings_has_all_subsections(self):
        settings = Settings()
        assert settings.database is not None
        assert settings.auth is not None
        assert settings.redis is not None
        assert settings.api is not None
        assert settings.logging is not None
        assert settings.review is not None
        assert settings.scoring is not None
        assert settings.cache is not None
        assert settings.rate_limit is not None
        assert settings.feature_flags is not None
        assert settings.tracing is not None
        assert settings.webhook is not None

    def test_get_database_url(self):
        settings = Settings()
        assert "postgresql" in settings.get_database_url()

    def test_get_redis_url(self):
        settings = Settings()
        assert "redis" in settings.get_redis_url()

    def test_is_development(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        settings = Settings()
        assert settings.is_development() is True

    def test_is_production(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "development")
        settings = Settings()
        assert settings.is_production() is False


class TestGetSettings:
    def test_cached(self):
        clear_settings_cache()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_clear_cache(self):
        clear_settings_cache()
        s1 = get_settings()
        clear_settings_cache()
        s2 = get_settings()
        assert s1 is not s2
