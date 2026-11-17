"""Targeted coverage tests for review_service.py."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.domains.review.schemas import (
    AnalysisInput,
    NarrativeInput,
    PageInput,
    SiteAnalysisInput,
    UserContext,
)
from src.domains.review.services.review_service import ReviewService, _get_tracer, _NullContext
from tests.llm_test_doubles import FakeWMClient


def _make_analysis(
    pages: list[PageInput] | None = None,
    site: SiteAnalysisInput | None = None,
    narrative: NarrativeInput | None = None,
) -> AnalysisInput:
    """Create a minimal AnalysisInput for testing."""
    return AnalysisInput(
        pages=pages
        if pages is not None
        else [PageInput(url="https://example.com", title="Example", page_type="homepage")],
        site=site if site is not None else SiteAnalysisInput(description="A test website"),
        narrative=narrative,
    )


def _service(wm: FakeWMClient | None = None, **kwargs) -> ReviewService:
    """Construct a ReviewService wired to a FakeWMClient."""
    return ReviewService(
        timeout_seconds=kwargs.pop("timeout_seconds", 60),
        wm_client=wm if wm is not None else FakeWMClient(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _NullContext (lines 43, 46)
# ---------------------------------------------------------------------------


class TestNullContext:
    def test_enter_returns_self(self) -> None:
        ctx = _NullContext()
        result = ctx.__enter__()
        assert result is ctx

    def test_exit_returns_none(self) -> None:
        ctx = _NullContext()
        result = ctx.__exit__(None, None, None)
        assert result is None

    def test_used_as_context_manager(self) -> None:
        with _NullContext() as ctx:
            assert isinstance(ctx, _NullContext)


# ---------------------------------------------------------------------------
# _get_tracer ImportError (lines 35-36)
# ---------------------------------------------------------------------------


class TestGetTracer:
    def test_returns_none_when_opentelemetry_missing(self) -> None:
        with patch.dict("sys.modules", {"opentelemetry": None, "opentelemetry.trace": None}):
            # Force re-evaluation by calling the function
            result = _get_tracer()
            assert result is None

    def test_returns_tracer_when_available(self) -> None:
        # opentelemetry is installed, so _get_tracer returns a real tracer
        result = _get_tracer()
        assert result is not None


# ---------------------------------------------------------------------------
# Progress callback failure (lines 179-180, 208-211)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestProgressCallbackFailure:
    async def test_progress_callback_exception_in_independent_reviews(self) -> None:
        """Test that a failing progress callback does not prevent review completion."""
        design_result = {
            "overall_score": 8.0,
            "categories": {"layout": 9},
            "strengths": ["Good"],
            "weaknesses": ["Minor"],
            "recommendations": ["Improve"],
        }

        async def mock_generate_review(review_type, analysis, **kwargs):
            return design_result

        async def failing_progress(results):
            raise RuntimeError("Callback exploded")

        service = _service()
        analysis = _make_analysis()

        with patch.object(service, "_generate_review", side_effect=mock_generate_review):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review"],
                on_progress=failing_progress,
            )

        assert results["design_review"]["overall_score"] == 8.0

    async def test_progress_callback_exception_in_dependent_reviews(self) -> None:
        """Test that a failing progress callback in dependent section is logged but not fatal."""
        design_result = {
            "overall_score": 7.5,
            "categories": {"layout": 8},
            "strengths": ["Good"],
            "weaknesses": ["OK"],
            "recommendations": ["Better"],
        }
        feature_result = {
            "total_proposals": 2,
            "proposals": [{"title": "Idea 1"}, {"title": "Idea 2"}],
        }

        call_count = 0

        async def mock_generate_review(review_type, analysis, **kwargs):
            nonlocal call_count
            call_count += 1
            if review_type == "design_review":
                return design_result
            return feature_result

        async def failing_progress(results):
            raise RuntimeError("Dependent callback exploded")

        service = _service()
        analysis = _make_analysis()

        with patch.object(service, "_generate_review", side_effect=mock_generate_review):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "feature_suggestions"],
                on_progress=failing_progress,
            )

        assert results["design_review"]["overall_score"] == 7.5
        assert results["feature_suggestions"]["total_proposals"] == 2


# ---------------------------------------------------------------------------
# Dependent review first-attempt success (line 192)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDependentReviewSuccess:
    async def test_dependent_review_succeeds_first_try(self) -> None:
        """Cover line 192: dependent review succeeds on first attempt."""
        design_result = {
            "overall_score": 7.0,
            "categories": {},
            "strengths": ["Nice"],
            "weaknesses": [],
            "recommendations": [],
        }
        feature_result = {
            "total_proposals": 3,
            "proposals": [{"title": "A"}, {"title": "B"}, {"title": "C"}],
        }

        async def mock_generate_review(review_type, analysis, **kwargs):
            if review_type == "design_review":
                return design_result
            return feature_result

        service = _service()
        analysis = _make_analysis()

        with patch.object(service, "_generate_review", side_effect=mock_generate_review):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "feature_suggestions"],
            )

        assert results["feature_suggestions"]["total_proposals"] == 3
        assert "error" not in results["feature_suggestions"]


# ---------------------------------------------------------------------------
# Empty LLM response (line 271)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmptyWMResponse:
    async def test_empty_string_response_raises_value_error(self) -> None:
        """Empty WM string response raises ValueError."""
        wm = FakeWMClient(outputs=[""])
        service = _service(wm=wm)
        analysis = _make_analysis()

        with (
            patch("src.domains.review.services.review_service.review_errors_total"),
            patch("src.domains.review.services.review_service.review_llm_latency_seconds"),
            patch("src.domains.review.services.review_service.review_llm_tokens_total"),
            pytest.raises(ValueError, match="Empty response from WM"),
        ):
            await service._generate_review("design_review", analysis)


# ---------------------------------------------------------------------------
# _retry_feature_suggestions (lines 302-356)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRetryFeatureSuggestions:
    async def test_retry_with_prior_reviews(self) -> None:
        """Cover _retry_feature_suggestions with prior reviews context."""
        payload = {
            "total_proposals": 2,
            "proposals": [
                {"id": 1, "title": "Search", "category": "quick_win"},
                {"id": 2, "title": "Dark mode", "category": "nice_to_have"},
            ],
        }
        wm = FakeWMClient(outputs=[payload])
        service = _service(wm=wm)
        analysis = _make_analysis()
        prior_reviews = {
            "design_review": {
                "overall_score": 7.5,
                "strengths": ["Clean layout", "Good colors", "Modern typography"],
            }
        }

        result = await service._retry_feature_suggestions(
            analysis, prior_reviews, message="Focus on UX"
        )

        assert result["total_proposals"] == 2
        assert len(result["proposals"]) == 2

    async def test_retry_without_prior_reviews_uses_analysis_context(self) -> None:
        """Cover _retry_feature_suggestions when prior_reviews is empty/None."""
        payload = {
            "total_proposals": 1,
            "proposals": [{"id": 1, "title": "Add FAQ"}],
        }
        wm = FakeWMClient(outputs=[payload])
        service = _service(wm=wm)
        analysis = _make_analysis(
            pages=[
                PageInput(
                    url="https://example.com",
                    title="Example",
                    page_type="homepage",
                    has_forms=True,
                    has_media=True,
                ),
            ],
        )

        result = await service._retry_feature_suggestions(analysis, None, message="")

        assert result["total_proposals"] == 1
        assert len(wm.calls) == 1

    async def test_retry_empty_response_raises(self) -> None:
        """Empty WM response from retry raises ValueError."""
        wm = FakeWMClient(outputs=[""])
        service = _service(wm=wm)
        analysis = _make_analysis()

        with pytest.raises(ValueError, match="Empty response from WM"):
            await service._retry_feature_suggestions(analysis, None, message="")


# ---------------------------------------------------------------------------
# _analysis_to_review_context (lines 360-373)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAnalysisToReviewContext:
    def test_with_pages_forms_and_media(self) -> None:
        """Cover _analysis_to_review_context with pages having forms and media."""
        service = _service()
        analysis = _make_analysis(
            pages=[
                PageInput(
                    url="https://example.com/contact",
                    title="Contact",
                    page_type="form",
                    has_forms=True,
                    has_media=False,
                ),
                PageInput(
                    url="https://example.com/gallery",
                    title="Gallery",
                    page_type="gallery",
                    has_forms=False,
                    has_media=True,
                ),
            ]
        )

        context = service._analysis_to_review_context(analysis)

        assert "analysis_summary" in context
        assert context["analysis_summary"]["overall_score"] == 5
        strengths = context["analysis_summary"]["strengths"]
        assert any("2 pages" in s for s in strengths)
        assert any("Includes forms" in s for s in strengths)
        assert any("Includes media" in s for s in strengths)

    def test_without_forms_or_media(self) -> None:
        """Cover _analysis_to_review_context with no forms/media."""
        service = _service()
        analysis = _make_analysis(
            pages=[
                PageInput(url="https://example.com", has_forms=False, has_media=False),
            ]
        )

        context = service._analysis_to_review_context(analysis)

        strengths = context["analysis_summary"]["strengths"]
        assert any("No forms" in s for s in strengths)
        assert any("No media" in s for s in strengths)

    def test_empty_pages_returns_empty_context(self) -> None:
        """Cover _analysis_to_review_context with no pages."""
        service = _service()
        analysis = _make_analysis(pages=[])

        context = service._analysis_to_review_context(analysis)

        assert context == {}


# ---------------------------------------------------------------------------
# User context injection (lines 440-447)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUserContextInjection:
    def test_user_context_with_all_fields(self) -> None:
        """Cover lines 440-447: user_context fields injected into template context."""
        service = _service()
        analysis = _make_analysis()
        user_context = UserContext(
            focus_areas=["mobile", "accessibility"],
            goals=["increase conversions", "reduce bounce rate"],
            tone="critical",
            target_audience="developers aged 25-40",
        )

        context = service._build_template_context(
            "design_review",
            analysis,
            user_context=user_context,
        )

        assert "focus_areas" in context
        assert "mobile" in context["focus_areas"]
        assert "business_goals" in context
        assert "increase conversions" in context["business_goals"]
        assert context["review_tone"] == "critical"
        assert context["user_target_audience"] == "developers aged 25-40"

    def test_user_context_with_empty_fields(self) -> None:
        """User context with empty fields should not inject keys."""
        service = _service()
        analysis = _make_analysis()
        user_context = UserContext(
            focus_areas=[],
            goals=[],
            tone="",
            target_audience="",
        )

        context = service._build_template_context(
            "design_review",
            analysis,
            user_context=user_context,
        )

        assert "focus_areas" not in context
        assert "business_goals" not in context
        assert "review_tone" not in context
        assert "user_target_audience" not in context


# ---------------------------------------------------------------------------
# Validation warnings (line 514/544)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseNonDictJson:
    def test_json_loads_returns_non_dict_raises_value_error(self) -> None:
        """Cover line 514: json.loads returns a non-dict value.

        In standard JSON, a {..} substring always decodes to a dict, so this
        line is defensive. We mock json.loads to simulate the edge case.
        """
        service = _service()
        with patch("src.domains.review.services.review_service.json.loads", return_value=[1, 2]):
            raw = '{"will_be_mocked": true}'
            with pytest.raises(ValueError, match="not a JSON object"):
                service._parse_and_validate("design_review", raw)


@pytest.mark.unit
class TestPydanticCoercion:
    def test_missing_fields_get_defaults(self) -> None:
        """Pydantic coercion fills in missing fields with defaults."""
        service = _service()
        # Missing 'categories' key, but has a valid score and list data
        raw = json.dumps(
            {
                "overall_score": 7,
                "strengths": ["Good"],
                "weaknesses": ["Bad"],
                "recommendations": ["Do better"],
            }
        )
        result = service._parse_and_validate("design_review", raw)
        # Pydantic provides defaults for missing fields
        assert result["categories"] == {}
        assert result["overall_score"] == 7.0

    def test_string_score_coerced_to_float(self) -> None:
        """Pydantic coerces string scores to float."""
        service = _service()
        raw = json.dumps(
            {
                "overall_score": "8",
                "categories": {},
                "strengths": [],
                "weaknesses": [],
                "recommendations": [],
            }
        )
        result = service._parse_and_validate("design_review", raw)
        assert result["overall_score"] == 8.0
        assert isinstance(result["overall_score"], float)


# ---------------------------------------------------------------------------
# _apply_computed_scores (lines 563-565)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestApplyComputedScores:
    def test_computed_score_applied(self) -> None:
        """Cover lines 563-565: actual computed score is applied."""
        from src.config import ScoringSettings

        scoring = ScoringSettings()
        service = ReviewService(timeout_seconds=60, scoring_settings=scoring)

        results = {
            "design_review": {
                "overall_score": 7.0,
                "categories": {
                    "layout_consistency": {"score": 8.0},
                    "whitespace_usage": {"score": 7.0},
                    "typography_hierarchy": {"score": 9.0},
                    "color_palette_harmony": {"score": 6.0},
                    "contrast_ratios": {"score": 8.0},
                    "responsive_design": {"score": 7.5},
                    "visual_hierarchy": {"score": 8.5},
                    "brand_consistency": {"score": 7.0},
                },
                "strengths": ["Good"],
                "weaknesses": [],
                "recommendations": [],
            }
        }

        service._apply_computed_scores(results)

        assert "computed_overall_score" in results["design_review"]
        assert isinstance(results["design_review"]["computed_overall_score"], float)

    def test_computed_score_skipped_for_error_results(self) -> None:
        """Verify error results are skipped."""
        from src.config import ScoringSettings

        scoring = ScoringSettings()
        service = ReviewService(timeout_seconds=60, scoring_settings=scoring)

        results = {
            "design_review": {"error": "LLM failure"},
        }

        service._apply_computed_scores(results)

        assert "computed_overall_score" not in results["design_review"]

    def test_no_scoring_settings_is_noop(self) -> None:
        """Verify no scoring_settings means no-op."""
        service = ReviewService(timeout_seconds=60, scoring_settings=None)

        results = {
            "design_review": {
                "overall_score": 7.0,
                "categories": {"layout_consistency": {"score": 8.0}},
            }
        }

        service._apply_computed_scores(results)

        assert "computed_overall_score" not in results["design_review"]
