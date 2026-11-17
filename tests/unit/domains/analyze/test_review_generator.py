"""Unit tests for ReviewGenerator service."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.domains.analyze.services.review_generator import ReviewGenerator
from tests.llm_test_doubles import FakeWMClient


@pytest.fixture
def generator() -> ReviewGenerator:
    """Create a ReviewGenerator instance with a fake WM client."""
    return ReviewGenerator(wm_client=FakeWMClient())


@pytest.fixture
def sample_analysis_result() -> dict:
    """Sample analysis result for testing."""
    return {
        "pages": [
            {
                "url": "https://example.com/",
                "title": "Example Site",
                "page_type": "homepage",
                "word_count": 500,
                "headings": ["Welcome", "Features", "Pricing"],
            },
            {
                "url": "https://example.com/about",
                "title": "About Us",
                "page_type": "about",
                "word_count": 300,
                "headings": ["Our Story", "Team"],
            },
        ],
        "site_analysis": {
            "description": "An example SaaS platform",
            "target_audience": ["developers", "startups"],
            "tone": "professional",
            "visual_identity": {"primary_color": "#3366CC"},
            "selling_points": ["Easy to use", "Fast deployment"],
            "use_cases": ["Web development", "API management"],
        },
        "narrative_extraction": {
            "hooks": ["Build faster", "Ship confidently"],
        },
    }


def test_review_generator_instantiation(generator: ReviewGenerator) -> None:
    """ReviewGenerator should be instantiable with default config."""
    assert generator.prompt_builder is not None
    assert generator.audit is not None


@pytest.mark.asyncio
async def test_generate_review_design(sample_analysis_result: dict) -> None:
    """generate_review should return parsed WM result for design review."""
    payload = {
        "overall_score": 7.5,
        "categories": {"layout_consistency": {"score": 8, "observations": ["Good grid"]}},
        "strengths": ["Strong layout"],
        "weaknesses": ["Low contrast"],
        "recommendations": ["Fix contrast"],
    }
    wm = FakeWMClient(outputs=[payload])
    generator = ReviewGenerator(wm_client=wm)

    result = await generator.generate_review(
        review_type="design",
        analysis_result=sample_analysis_result,
        job_id="test-job",
    )

    assert result["review_type"] == "design"
    assert result["result"]["overall_score"] == 7.5
    assert result["llm_calls"] == 1
    assert len(result.get("validation_errors", [])) == 0
    assert wm.calls[0].worker_name == "analysis-review-design"


@pytest.mark.asyncio
async def test_generate_review_usability(sample_analysis_result: dict) -> None:
    """generate_review should return parsed WM result for usability review."""
    payload = {
        "overall_score": 6.8,
        "categories": {
            "navigation_clarity": {
                "score": 7,
                "issues": ["Deep menu"],
                "recommendations": ["Flatten nav"],
            }
        },
        "critical_issues": [],
        "quick_wins": ["Add breadcrumbs"],
        "long_term_improvements": ["Redesign nav"],
    }
    wm = FakeWMClient(outputs=[payload])
    generator = ReviewGenerator(wm_client=wm)

    result = await generator.generate_review(
        review_type="usability",
        analysis_result=sample_analysis_result,
        job_id="test-job",
    )

    assert result["review_type"] == "usability"
    assert result["result"]["overall_score"] == 6.8
    assert len(result.get("validation_errors", [])) == 0


@pytest.mark.asyncio
async def test_generate_reviews_parallel(
    generator: ReviewGenerator, sample_analysis_result: dict
) -> None:
    """generate_reviews should call all review types in parallel."""
    call_count = 0

    async def mock_generate(
        review_type: str,
        analysis_result: dict,
        user_context: dict | None = None,
        job_id: str = "",
        **kwargs: object,
    ) -> dict:
        nonlocal call_count
        call_count += 1
        return {
            "review_type": review_type,
            "result": {"overall_score": 7.0},
            "llm_calls": 1,
            "artifacts": [],
        }

    with patch.object(generator, "generate_review", side_effect=mock_generate):
        results = await generator.generate_reviews(
            review_types=["design", "usability", "feature", "content_quality"],
            analysis_result=sample_analysis_result,
            job_id="test-parallel",
        )

    assert call_count == 4
    assert "design" in results
    assert "usability" in results
    assert "feature" in results
    assert "content_quality" in results


def test_validate_schema_design_valid(generator: ReviewGenerator) -> None:
    """Valid design review data should pass validation."""
    data: dict[str, object] = {
        "overall_score": 7.5,
        "categories": {},
        "strengths": [],
        "weaknesses": [],
        "recommendations": [],
    }
    errors = generator._validate_schema("design", data)
    assert errors == []


def test_validate_schema_design_invalid(generator: ReviewGenerator) -> None:
    """Design review missing required keys should return errors."""
    data: dict[str, object] = {"overall_score": 7.5}
    errors = generator._validate_schema("design", data)
    assert len(errors) > 0
    assert any("categories" in e for e in errors)


def test_validate_schema_feature_valid(generator: ReviewGenerator) -> None:
    """Valid feature review data should pass validation."""
    data: dict[str, object] = {
        "vertical": "SaaS",
        "feature_completeness_score": 7,
        "present_features": {},
        "missing_features": [],
        "differentiators": [],
        "suggestions": [],
    }
    errors = generator._validate_schema("feature", data)
    assert errors == []


def test_validate_schema_content_quality_invalid(generator: ReviewGenerator) -> None:
    """Content quality review missing keys should return errors."""
    data: dict[str, object] = {"overall_score": 5.0}
    errors = generator._validate_schema("content_quality", data)
    assert len(errors) > 0
    assert any("categories" in e for e in errors)
    assert any("strengths" in e for e in errors)


@pytest.mark.asyncio
async def test_generate_review_wm_failure(
    sample_analysis_result: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When WM fails, generate_review should return graceful fallback."""
    wm = FakeWMClient(raise_after="always", error=RuntimeError("WM unavailable"))
    generator = ReviewGenerator(wm_client=wm)

    # Disable retries to keep the test deterministic.
    monkeypatch.setenv("WM_MAX_RETRIES", "0")
    monkeypatch.setenv("WM_RETRY_DELAY", "0")
    from src.config import clear_settings_cache

    clear_settings_cache()
    try:
        result = await generator.generate_review(
            review_type="design",
            analysis_result=sample_analysis_result,
            job_id="test-fail",
        )
    finally:
        clear_settings_cache()

    assert result["review_type"] == "design"
    assert result["result"] == {}
    assert "error" in result
    assert "WM unavailable" in result["error"]


@pytest.mark.asyncio
async def test_generate_review_custom_prompt(sample_analysis_result: dict) -> None:
    """Custom prompt should override template-generated prompt."""
    payload = {
        "overall_score": 9.0,
        "categories": {},
        "strengths": [],
        "weaknesses": [],
        "recommendations": [],
    }
    wm = FakeWMClient(outputs=[payload])
    generator = ReviewGenerator(wm_client=wm)

    custom_prompt = "Review this site and return JSON."
    result = await generator.generate_review(
        review_type="design",
        analysis_result=sample_analysis_result,
        job_id="test-custom",
        custom_prompt=custom_prompt,
    )

    assert result["review_type"] == "design"
    assert result["result"]["overall_score"] == 9.0
    # Verify the custom prompt was actually sent to WM.
    assert wm.calls, "no WM request captured"
    assert wm.calls[0].message == custom_prompt
