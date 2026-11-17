"""Tests for extraction configuration."""

import pytest
from pydantic import ValidationError

from src.domains.brand.services.extraction_config import (
    ALL_METHODS,
    BrandExtractConfig,
    default_config,
    minimal_config,
)


class TestDefaultConfig:
    """Tests for default configuration behavior."""

    def test_default_config_all_methods(self):
        """Default config should include all extraction methods."""
        config = default_config()
        assert config.methods == ALL_METHODS
        assert len(config.methods) == len(ALL_METHODS)

    def test_default_config_features_enabled(self):
        """All feature toggles should be True by default (except download_logo)."""
        config = default_config()
        assert config.extract_social is True
        assert config.extract_tagline is True
        assert config.extract_fonts is True
        assert config.extract_colors is True
        assert config.download_logo is False

    def test_default_timeout(self):
        """Default timeout should be 15.0 seconds."""
        config = default_config()
        assert config.timeout == 15.0

    def test_default_max_css(self):
        """Default max CSS stylesheets should be 10."""
        config = default_config()
        assert config.max_css_stylesheets == 10

    def test_default_confidence_threshold(self):
        """Default confidence threshold should be 0.0."""
        config = default_config()
        assert config.confidence_threshold == 0.0


class TestMinimalConfig:
    """Tests for minimal/fast configuration."""

    def test_minimal_config_single_method(self):
        """Minimal config should only include html_meta method."""
        config = minimal_config()
        assert config.methods == ["html_meta"]
        assert len(config.methods) == 1

    def test_minimal_config_features_disabled(self):
        """Minimal config should disable social, tagline, and fonts."""
        config = minimal_config()
        assert config.extract_social is False
        assert config.extract_tagline is False
        assert config.extract_fonts is False
        assert config.extract_colors is True
        assert config.download_logo is False

    def test_minimal_config_fast_timeout(self):
        """Minimal config should use a shorter timeout of 5.0 seconds."""
        config = minimal_config()
        assert config.timeout == 5.0


class TestShouldRun:
    """Tests for the should_run method."""

    def test_should_run_true(self):
        """should_run returns True when the method is in the methods list."""
        config = default_config()
        assert config.should_run("html_meta") is True
        assert config.should_run("css_parser") is True
        assert config.should_run("schema_org") is True

    def test_should_run_false(self):
        """should_run returns False when the method is not in the methods list."""
        config = minimal_config()
        assert config.should_run("css_parser") is False
        assert config.should_run("schema_org") is False
        assert config.should_run("nonexistent_method") is False


class TestIsFeatureEnabled:
    """Tests for the is_feature_enabled method."""

    def test_is_feature_enabled(self):
        """is_feature_enabled should correctly map each feature name to its toggle."""
        config = default_config()
        assert config.is_feature_enabled("social") is True
        assert config.is_feature_enabled("tagline") is True
        assert config.is_feature_enabled("fonts") is True
        assert config.is_feature_enabled("colors") is True
        assert config.is_feature_enabled("logo") is False

        minimal = minimal_config()
        assert minimal.is_feature_enabled("social") is False
        assert minimal.is_feature_enabled("tagline") is False
        assert minimal.is_feature_enabled("fonts") is False
        assert minimal.is_feature_enabled("colors") is True
        assert minimal.is_feature_enabled("logo") is False

        # Unknown features default to True
        assert config.is_feature_enabled("unknown_feature") is True


class TestValidation:
    """Tests for pydantic field validation."""

    def test_confidence_threshold_validation(self):
        """Confidence threshold values > 1.0 should be rejected."""
        with pytest.raises(ValidationError):
            BrandExtractConfig(confidence_threshold=1.5)

        with pytest.raises(ValidationError):
            BrandExtractConfig(confidence_threshold=-0.1)

        # Boundary values should be accepted
        config_zero = BrandExtractConfig(confidence_threshold=0.0)
        assert config_zero.confidence_threshold == 0.0

        config_one = BrandExtractConfig(confidence_threshold=1.0)
        assert config_one.confidence_threshold == 1.0

    def test_timeout_validation(self):
        """Timeout values <= 0 should be rejected."""
        with pytest.raises(ValidationError):
            BrandExtractConfig(timeout=0.0)

        with pytest.raises(ValidationError):
            BrandExtractConfig(timeout=-5.0)

        with pytest.raises(ValidationError):
            BrandExtractConfig(timeout=121.0)

        # Boundary values should be accepted
        config_small = BrandExtractConfig(timeout=0.1)
        assert config_small.timeout == 0.1

        config_max = BrandExtractConfig(timeout=120.0)
        assert config_max.timeout == 120.0


class TestCustomConfig:
    """Tests for custom configuration overrides."""

    def test_custom_config(self):
        """Override specific fields while others keep defaults."""
        config = BrandExtractConfig(
            methods=["html_meta", "css_parser"],
            confidence_threshold=0.5,
            timeout=30.0,
            max_css_stylesheets=5,
            extract_social=False,
            download_logo=True,
        )
        assert config.methods == ["html_meta", "css_parser"]
        assert config.confidence_threshold == 0.5
        assert config.timeout == 30.0
        assert config.max_css_stylesheets == 5
        assert config.extract_social is False
        assert config.download_logo is True

        # Non-overridden fields keep defaults
        assert config.extract_tagline is True
        assert config.extract_fonts is True
        assert config.extract_colors is True
