"""Unit tests for review service error handling and edge cases.

Tests WM timeout, invalid JSON, all-NA detection, empty proposals,
partial failure in generate_all, and feature_suggestions retry logic.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from src.domains.review.schemas import AnalysisInput, PageInput, SiteAnalysisInput
from src.domains.review.services.review_service import ReviewService, _is_all_na
from tests.llm_test_doubles import FakeWMClient


def _make_analysis() -> AnalysisInput:
    """Create a minimal AnalysisInput for testing."""
    return AnalysisInput(
        pages=[
            PageInput(
                url="https://example.com",
                title="Example",
                page_type="homepage",
            )
        ],
        site=SiteAnalysisInput(description="A test website"),
    )


@pytest.mark.unit
class TestWMTimeout:
    """Tests for WM call timeout behavior."""

    async def test_wm_timeout_raises_timeout_error(self) -> None:
        """Test that WM timeout raises TimeoutError and increments metric."""
        wm = FakeWMClient(raise_after="always", error=TimeoutError())
        service = ReviewService(timeout_seconds=1, wm_client=wm)
        analysis = _make_analysis()

        with (
            patch("src.domains.review.services.review_service.review_errors_total") as mock_metric,
            patch("src.domains.review.services.review_service.review_llm_latency_seconds"),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            await service._generate_review("design_review", analysis)

            mock_metric.labels.assert_called_with(review_type="design_review", error_type="timeout")


@pytest.mark.unit
class TestInvalidJSONResponse:
    """Tests for handling invalid JSON from WM."""

    async def test_invalid_json_response_raises_value_error(self) -> None:
        """Test that non-JSON WM string response raises ValueError during parse."""
        wm = FakeWMClient(outputs=["This is not JSON at all, just plain text."])
        service = ReviewService(timeout_seconds=60, wm_client=wm)
        analysis = _make_analysis()

        with (
            patch("src.domains.review.services.review_service.review_errors_total"),
            patch("src.domains.review.services.review_service.review_llm_latency_seconds"),
            patch("src.domains.review.services.review_service.review_llm_tokens_total"),
            pytest.raises(ValueError, match="No JSON object"),
        ):
            await service._generate_review("design_review", analysis)

    def test_parse_and_validate_with_invalid_json_string(self) -> None:
        """Test _parse_and_validate raises ValueError for non-JSON."""
        service = ReviewService(wm_client=FakeWMClient())
        with pytest.raises(ValueError, match="No JSON object"):
            service._parse_and_validate("design_review", "Just some text without JSON")


@pytest.mark.unit
class TestAllNADetection:
    """Tests for all-NA response detection."""

    def test_all_na_response_detected(self) -> None:
        """Test that _is_all_na correctly identifies all-NA data."""
        data = {
            "overall_score": "N/A",
            "categories": {},
            "strengths": [],
            "weaknesses": [],
            "recommendations": [],
        }
        assert _is_all_na(data) is True

    def test_mixed_response_not_all_na(self) -> None:
        """Test that _is_all_na returns False when some values are meaningful."""
        data = {
            "overall_score": 7.5,
            "categories": {},
            "strengths": ["Good design"],
            "weaknesses": [],
            "recommendations": [],
        }
        assert _is_all_na(data) is False

    def test_empty_dict_is_all_na(self) -> None:
        """Test that empty dict is treated as all-NA."""
        assert _is_all_na({}) is True

    def test_meta_keys_ignored(self) -> None:
        """Test that keys starting with _ are ignored in NA check."""
        data = {
            "_validation_warnings": ["some warning"],
            "overall_score": "na",
            "categories": {},
        }
        assert _is_all_na(data) is True

    def test_all_na_response_raises_in_parse(self) -> None:
        """Test that all-NA response raises ValueError during parsing."""
        service = ReviewService(wm_client=FakeWMClient())
        raw = json.dumps(
            {
                "overall_score": "N/A",
                "categories": {},
                "strengths": [],
                "weaknesses": [],
                "recommendations": [],
            }
        )
        with pytest.raises(ValueError, match="all N/A"):
            service._parse_and_validate("design_review", raw)


@pytest.mark.unit
class TestEmptyProposalsRejection:
    """Tests for empty proposals in feature_suggestions."""

    def test_empty_proposals_raises_value_error(self) -> None:
        """Test that empty proposals list raises ValueError."""
        service = ReviewService(wm_client=FakeWMClient())
        raw = json.dumps(
            {
                "total_proposals": 0,
                "proposals": [],
            }
        )
        with pytest.raises(ValueError, match="empty proposals"):
            service._parse_and_validate("feature_suggestions", raw)

    def test_empty_json_object_returns_fallback(self) -> None:
        """A completely empty JSON object returns the insufficient-data marker.

        It must NOT return a defaults-filled model dump: this assertion used to
        read ``total_proposals == 0`` / ``proposals == []``, i.e. it pinned a
        review that never happened as a real result reporting zero proposals.
        """
        service = ReviewService(wm_client=FakeWMClient())
        result = service._parse_and_validate("feature_suggestions", "{}")
        assert result["insufficient_data"] is True
        assert "error" in result
        assert "total_proposals" not in result


@pytest.mark.unit
class TestPartialFailureInGenerateAll:
    """Tests for partial failure handling in generate_all."""

    async def test_one_type_fails_others_succeed(self) -> None:
        """Test that one review type failing does not block others."""
        service = ReviewService(timeout_seconds=60, wm_client=FakeWMClient())
        analysis = _make_analysis()

        design_result = {
            "overall_score": 8.0,
            "categories": {"layout": 9},
            "strengths": ["Good"],
            "weaknesses": ["Minor"],
            "recommendations": ["Improve"],
        }

        call_count = 0

        async def mock_generate_review(review_type, analysis, **kwargs):
            nonlocal call_count
            call_count += 1
            if review_type == "usability_review":
                raise OSError("LLM error for usability")
            return design_result

        with patch.object(service, "_generate_review", side_effect=mock_generate_review):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "usability_review"],
            )

        assert "design_review" in results
        assert results["design_review"]["overall_score"] == 8.0
        assert "usability_review" in results
        assert "error" in results["usability_review"]
        assert "LLM error" in results["usability_review"]["error"]

    async def test_all_independent_types_fail(self) -> None:
        """Test that all independent types failing records errors for each."""
        service = ReviewService(timeout_seconds=60, wm_client=FakeWMClient())
        analysis = _make_analysis()

        async def mock_generate_review(review_type, analysis, **kwargs):
            raise OSError(f"Failed for {review_type}")

        with patch.object(service, "_generate_review", side_effect=mock_generate_review):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "usability_review"],
            )

        assert "error" in results["design_review"]
        assert "error" in results["usability_review"]


@pytest.mark.unit
class TestFeatureSuggestionsRetry:
    """Tests for feature_suggestions retry logic."""

    async def test_feature_suggestions_first_fails_retry_succeeds(self) -> None:
        """Test that failed feature_suggestions is retried with simplified prompt."""
        service = ReviewService(timeout_seconds=60, wm_client=FakeWMClient())
        analysis = _make_analysis()

        good_result = {
            "total_proposals": 3,
            "proposals": [
                {"id": 1, "title": "Add search"},
                {"id": 2, "title": "Add dark mode"},
                {"id": 3, "title": "Add analytics"},
            ],
        }

        # Independent review passes
        design_result = {
            "overall_score": 7.5,
            "categories": {"layout": 8},
            "strengths": ["Good"],
            "weaknesses": ["OK"],
            "recommendations": ["Better"],
        }

        call_count = 0

        async def mock_generate_review(review_type, analysis, **kwargs):
            nonlocal call_count
            call_count += 1
            if review_type == "design_review":
                return design_result
            # feature_suggestions fails on first attempt
            raise OSError("LLM parse error")

        async def mock_retry_feature_suggestions(analysis, prior_reviews, message, **kwargs):
            return good_result

        with (
            patch.object(service, "_generate_review", side_effect=mock_generate_review),
            patch.object(
                service,
                "_retry_feature_suggestions",
                side_effect=mock_retry_feature_suggestions,
            ),
        ):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "feature_suggestions"],
            )

        assert "design_review" in results
        assert results["design_review"]["overall_score"] == 7.5
        assert "feature_suggestions" in results
        assert results["feature_suggestions"]["total_proposals"] == 3
        assert "error" not in results["feature_suggestions"]

    async def test_feature_suggestions_both_attempts_fail(self) -> None:
        """Test that feature_suggestions records error when retry also fails."""
        service = ReviewService(timeout_seconds=60, wm_client=FakeWMClient())
        analysis = _make_analysis()

        design_result = {
            "overall_score": 7.5,
            "categories": {"layout": 8},
            "strengths": ["Good"],
            "weaknesses": ["OK"],
            "recommendations": ["Better"],
        }

        async def mock_generate_review(review_type, analysis, **kwargs):
            if review_type == "design_review":
                return design_result
            raise OSError("First attempt failed")

        async def mock_retry_feature_suggestions(analysis, prior_reviews, message, **kwargs):
            raise OSError("Retry also failed")

        with (
            patch.object(service, "_generate_review", side_effect=mock_generate_review),
            patch.object(
                service,
                "_retry_feature_suggestions",
                side_effect=mock_retry_feature_suggestions,
            ),
        ):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "feature_suggestions"],
            )

        assert results["design_review"]["overall_score"] == 7.5
        assert "error" in results["feature_suggestions"]
        assert "Retry also failed" in results["feature_suggestions"]["error"]

    async def test_dependent_review_httpx_error_degrades_not_aborts(self) -> None:
        """A transient httpx.HTTPError (WM/KAG blip) during a dependent review must
        degrade THAT review to an error entry, not abort the whole job and discard
        already-completed parallel reviews.

        Regression: _run_dependent_reviews' except tuples omitted httpx.HTTPError
        while _generate_review re-raises it, so httpx.ConnectError escaped uncaught
        and aborted generate_all.
        """
        service = ReviewService(timeout_seconds=60, wm_client=FakeWMClient())
        analysis = _make_analysis()

        design_result = {
            "overall_score": 7.5,
            "categories": {"layout": 8},
            "strengths": ["Good"],
            "weaknesses": ["OK"],
            "recommendations": ["Better"],
        }

        async def mock_generate_review(review_type, analysis, **kwargs):
            if review_type == "design_review":
                return design_result
            # Dependent review's WM (HTTP to worker_management) call blips.
            raise httpx.ConnectError("connection refused")

        async def mock_retry_feature_suggestions(analysis, prior_reviews, message, **kwargs):
            raise httpx.ReadError("read timed out")

        with (
            patch.object(service, "_generate_review", side_effect=mock_generate_review),
            patch.object(
                service,
                "_retry_feature_suggestions",
                side_effect=mock_retry_feature_suggestions,
            ),
        ):
            results = await service.generate_all(
                analysis=analysis,
                review_types=["design_review", "feature_suggestions"],
            )

        # Whole job survived: the completed parallel review is preserved.
        assert results["design_review"]["overall_score"] == 7.5
        # The blipping dependent review degraded to an error entry.
        assert "error" in results["feature_suggestions"]
        assert "read timed out" in results["feature_suggestions"]["error"]
