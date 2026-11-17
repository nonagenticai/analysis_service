"""Tests for AnalysisConfig enhanced fields and IntelligenceContext models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.domains.analyze.schemas import (
    AnalysisConfig,
    AnalyzeRequest,
    CrawledPageInput,
    IntelligenceContext,
)


class TestAnalysisConfig:
    def test_defaults(self) -> None:
        config = AnalysisConfig()
        assert config.enable_language_dedup is True
        assert config.enable_spa_detection is True
        assert config.enable_context_enrichment is True
        assert config.llm_model is None
        assert config.max_pages == 100
        assert config.temperature is None
        assert config.timeout_seconds == 300
        assert config.parallel_workers == 4
        assert config.include_narrative is True
        assert config.custom_prompts is None

    def test_override_flags(self) -> None:
        config = AnalysisConfig(
            enable_language_dedup=False,
            enable_spa_detection=False,
            llm_model="claude-haiku-4-5-20251001",
            max_pages=50,
        )
        assert config.enable_language_dedup is False
        assert config.enable_spa_detection is False
        assert config.llm_model == "claude-haiku-4-5-20251001"
        assert config.max_pages == 50

    def test_temperature_none_is_valid(self) -> None:
        config = AnalysisConfig(temperature=None)
        assert config.temperature is None

    def test_temperature_zero_is_valid(self) -> None:
        config = AnalysisConfig(temperature=0.0)
        assert config.temperature == 0.0

    def test_temperature_max_boundary(self) -> None:
        config = AnalysisConfig(temperature=2.0)
        assert config.temperature == 2.0

    def test_temperature_mid_value(self) -> None:
        config = AnalysisConfig(temperature=0.7)
        assert config.temperature == 0.7

    def test_temperature_negative_rejected(self) -> None:
        with pytest.raises(ValidationError, match="temperature"):
            AnalysisConfig(temperature=-0.1)

    def test_temperature_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="temperature"):
            AnalysisConfig(temperature=2.1)

    def test_timeout_seconds_default(self) -> None:
        config = AnalysisConfig()
        assert config.timeout_seconds == 300

    def test_timeout_seconds_min_boundary(self) -> None:
        config = AnalysisConfig(timeout_seconds=10)
        assert config.timeout_seconds == 10

    def test_timeout_seconds_max_boundary(self) -> None:
        config = AnalysisConfig(timeout_seconds=600)
        assert config.timeout_seconds == 600

    def test_timeout_seconds_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timeout_seconds"):
            AnalysisConfig(timeout_seconds=9)

    def test_timeout_seconds_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="timeout_seconds"):
            AnalysisConfig(timeout_seconds=601)

    def test_parallel_workers_default(self) -> None:
        config = AnalysisConfig()
        assert config.parallel_workers == 4

    def test_parallel_workers_min_boundary(self) -> None:
        config = AnalysisConfig(parallel_workers=1)
        assert config.parallel_workers == 1

    def test_parallel_workers_max_boundary(self) -> None:
        config = AnalysisConfig(parallel_workers=16)
        assert config.parallel_workers == 16

    def test_parallel_workers_below_min_rejected(self) -> None:
        with pytest.raises(ValidationError, match="parallel_workers"):
            AnalysisConfig(parallel_workers=0)

    def test_parallel_workers_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError, match="parallel_workers"):
            AnalysisConfig(parallel_workers=17)

    def test_include_narrative_default_true(self) -> None:
        config = AnalysisConfig()
        assert config.include_narrative is True

    def test_include_narrative_false(self) -> None:
        config = AnalysisConfig(include_narrative=False)
        assert config.include_narrative is False

    def test_custom_prompts_none_default(self) -> None:
        config = AnalysisConfig()
        assert config.custom_prompts is None

    def test_custom_prompts_valid_dict(self) -> None:
        prompts = {"site_analysis": "Analyze this site.", "summary": "Summarize content."}
        config = AnalysisConfig(custom_prompts=prompts)
        assert config.custom_prompts == prompts
        assert config.custom_prompts is not None
        assert config.custom_prompts["site_analysis"] == "Analyze this site."

    def test_config_serialization_round_trip(self) -> None:
        original = AnalysisConfig(
            temperature=0.5,
            timeout_seconds=120,
            parallel_workers=8,
            include_narrative=False,
            custom_prompts={"site_analysis": "Custom prompt"},
        )
        data = original.model_dump()
        restored = AnalysisConfig(**data)
        assert restored.temperature == original.temperature
        assert restored.timeout_seconds == original.timeout_seconds
        assert restored.parallel_workers == original.parallel_workers
        assert restored.include_narrative == original.include_narrative
        assert restored.custom_prompts == original.custom_prompts

    def test_config_json_round_trip(self) -> None:
        original = AnalysisConfig(
            temperature=1.0,
            timeout_seconds=60,
            parallel_workers=2,
            custom_prompts={"key": "value"},
        )
        json_str = original.model_dump_json()
        restored = AnalysisConfig.model_validate_json(json_str)
        assert restored == original


class TestIntelligenceContext:
    def test_defaults(self) -> None:
        ctx = IntelligenceContext()
        assert ctx.description is None
        assert ctx.goals == []
        assert ctx.focus_pages == []

    def test_full_context(self) -> None:
        ctx = IntelligenceContext(
            description="A SaaS platform",
            site_type="saas",
            target_audience="developers",
            goals=["generate leads"],
            focus_pages=["/pricing"],
            exclude_pages=["/login"],
        )
        assert ctx.description == "A SaaS platform"
        assert ctx.goals == ["generate leads"]


class TestAnalyzeRequestConfig:
    def test_default_config(self) -> None:
        req = AnalyzeRequest(
            pages=[CrawledPageInput(url="https://example.com/", html="<html></html>")]
        )
        assert req.config.enable_language_dedup is True
        assert req.config.max_pages == 100
        assert req.config.temperature is None
        assert req.config.timeout_seconds == 300
        assert req.config.parallel_workers == 4
        assert req.config.include_narrative is True
        assert req.config.custom_prompts is None

    def test_backward_compatibility_dict_context(self) -> None:
        req = AnalyzeRequest(
            pages=[CrawledPageInput(url="https://example.com/", html="<html></html>")],
            user_context={"description": "legacy dict"},
        )
        # Pydantic coerces matching dicts to IntelligenceContext;
        # verify the data is preserved and accessible either way.
        if isinstance(req.user_context, dict):
            assert req.user_context == {"description": "legacy dict"}
        else:
            assert req.user_context is not None
            assert req.user_context.description == "legacy dict"

    def test_request_with_enhanced_config(self) -> None:
        req = AnalyzeRequest(
            pages=[CrawledPageInput(url="https://example.com/", html="<html></html>")],
            config=AnalysisConfig(
                temperature=0.8,
                timeout_seconds=60,
                parallel_workers=2,
                include_narrative=False,
                custom_prompts={"site_analysis": "Custom"},
            ),
        )
        assert req.config.temperature == 0.8
        assert req.config.timeout_seconds == 60
        assert req.config.parallel_workers == 2
        assert req.config.include_narrative is False
        assert req.config.custom_prompts == {"site_analysis": "Custom"}
