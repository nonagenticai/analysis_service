"""'Not assessed' must be distinct from 'assessed badly', end to end.

Two mirror-image defects are pinned here:

A. An empty LLM reply became a *real* score of 0.0 (``model_cls().model_dump()``
   with ``overall_score: float = 0.0``), so a review that never happened was
   printed to the customer as "Score: 0.0/10 (F)" and compared as a delta.

B. ``compute_aggregate_score`` read the score through an ``or`` chain, so a
   *legitimate* 0.0 was dropped as if it were unmeasured -- inflating the mean.
   It also renormalised silently over the survivors, so a 1-of-4 job produced a
   complete-looking aggregate with no coverage denominator anywhere.
"""

from __future__ import annotations

import pytest

from src.domains.review.services.comparison import compare_reviews
from src.domains.review.services.llm_output_models import (
    DesignReviewOutput,
    insufficient_data_result,
)
from src.domains.review.services.report_generator import _review_section_lines
from src.domains.review.services.review_service import ReviewService
from src.domains.review.services.scoring import compute_aggregate_score
from src.domains.review.services.summary import generate_summary
from src.domains.review.services.text_utils import extract_overall_score
from tests.llm_test_doubles import FakeWMClient

_FOUR_EQUAL_WEIGHTS = {
    "design_review": 0.25,
    "usability_review": 0.25,
    "content_quality_review": 0.25,
    "feature_review": 0.25,
}


def _empty_reply_result(review_type: str = "design_review") -> dict:
    """What the service actually produces for an LLM reply of ``{}``."""
    service = ReviewService(wm_client=FakeWMClient())
    return service._parse_and_validate(review_type, "{}")


@pytest.mark.unit
class TestDefectAEmptyReplyIsNotAZero:
    def test_empty_reply_is_not_a_printable_zero_grade(self) -> None:
        result = _empty_reply_result()

        assert result["insufficient_data"] is True
        assert extract_overall_score(result) is None, (
            "an unmeasured review must yield NO score, not 0.0"
        )

        rendered = "\n".join(_review_section_lines("design_review", result))
        assert "**Score:**" not in rendered
        assert "(F)" not in rendered
        assert "0.0/10" not in rendered

    def test_empty_reply_is_distinguishable_from_a_genuine_zero(self) -> None:
        unmeasured = _empty_reply_result()
        genuine_zero = {"overall_score": 0.0, "strengths": [], "weaknesses": ["Everything"]}

        assert unmeasured.get("insufficient_data") is True
        assert genuine_zero.get("insufficient_data") is None

        # The genuine 0.0 is a real measurement and must still grade as F.
        assert extract_overall_score(genuine_zero) == 0.0
        rendered = "\n".join(_review_section_lines("design_review", genuine_zero))
        assert "**Score:** 0.0/10 (F)" in rendered

    def test_a_computed_zero_still_prints_as_f_in_the_report(self) -> None:
        """The ``or`` chain in the report bled the same mirror bug.

        ``computed_overall_score`` of 0.0 is falsy, so ``computed or overall``
        printed the LLM's own 9.0 instead of the deterministic 0.0.
        """
        result = {"computed_overall_score": 0.0, "overall_score": 9.0}
        rendered = "\n".join(_review_section_lines("design_review", result))
        assert "**Score:** 0.0/10 (F)" in rendered
        assert "9.0" not in rendered

    def test_a_missing_score_on_a_populated_reply_is_also_unmeasured(self) -> None:
        """The model layer, not just the empty-reply branch."""
        assert DesignReviewOutput.model_validate({"strengths": ["nice"]}).overall_score is None

    def test_empty_reply_produces_no_comparison_delta(self) -> None:
        baseline = {"design_review": {"overall_score": 8.0}}
        current = {"design_review": _empty_reply_result()}

        diff = compare_reviews(baseline, current)

        assert diff["score_deltas"]["design_review"] is None, (
            "a review that never ran must not report a -8.0 regression"
        )
        assert diff["overall_delta"] is None
        assert diff["grade_change"] is None


@pytest.mark.unit
class TestDefectBTheMirrorAndCoverage:
    def test_a_legitimate_zero_is_not_dropped_as_unmeasured(self) -> None:
        """The mirror bug: the old ``or`` chain skipped a real 0.0 entirely."""
        results = {
            "design_review": {"overall_score": 0.0},
            "usability_review": {"overall_score": 10.0},
        }
        weights = {"design_review": 0.5, "usability_review": 0.5}

        agg = compute_aggregate_score(results, weights)

        assert agg["per_review_scores"]["design_review"] == 0.0
        assert agg["aggregate_score"] == 5.0, (
            "a real 0.0 was dropped as unmeasured, renormalising to the 10.0 alone"
        )
        assert agg["coverage"] == 1.0
        assert agg["partial"] is False
        assert agg["unscored_review_types"] == []

    def test_a_legitimate_zero_computed_score_is_not_dropped(self) -> None:
        """``computed_overall_score`` of 0.0 must win over the LLM's own score."""
        results = {"design_review": {"computed_overall_score": 0.0, "overall_score": 9.0}}

        agg = compute_aggregate_score(results, {"design_review": 1.0})

        assert agg["per_review_scores"]["design_review"] == 0.0
        assert agg["aggregate_score"] == 0.0
        assert agg["grade"] == "F"

    def test_a_partial_aggregate_carries_its_coverage(self) -> None:
        results = {
            "design_review": {"overall_score": 8.0},
            "usability_review": {"overall_score": 6.0},
            "content_quality_review": {"overall_score": 7.0},
            "feature_review": {"error": "timeout"},
        }

        agg = compute_aggregate_score(results, _FOUR_EQUAL_WEIGHTS)

        assert agg["aggregate_score"] == 7.0
        assert agg["coverage"] == 0.75
        assert agg["partial"] is True
        assert agg["unscored_review_types"] == ["feature_review"]
        assert agg["scored_review_types"] == [
            "content_quality_review",
            "design_review",
            "usability_review",
        ]

    def test_a_one_of_four_aggregate_is_refused(self) -> None:
        results = {
            "design_review": {"overall_score": 8.0},
            "usability_review": {"error": "timeout"},
            "content_quality_review": insufficient_data_result("content_quality_review", "empty"),
            "feature_review": {"error": "timeout"},
        }

        agg = compute_aggregate_score(results, _FOUR_EQUAL_WEIGHTS)

        assert agg["aggregate_score"] is None, (
            "25% coverage must not be published as the site's score"
        )
        assert agg["grade"] is None
        assert agg["coverage"] == 0.25
        assert agg["partial"] is True
        assert agg["per_review_scores"]["design_review"] == 8.0, (
            "the one score that WAS measured stays visible; only the aggregate is refused"
        )

    def test_coverage_reaches_the_summary_a_consumer_reads(self) -> None:
        results = {
            "design_review": {"overall_score": 8.0},
            "usability_review": {"overall_score": 6.0},
            "content_quality_review": {"overall_score": 7.0},
            "feature_review": {"error": "timeout"},
        }

        summary = generate_summary("job-1", results, _FOUR_EQUAL_WEIGHTS)

        assert summary["coverage"] == 0.75
        assert summary["partial"] is True
        assert summary["unscored_review_types"] == ["feature_review"]

    def test_a_fully_successful_job_scores_exactly_as_before(self) -> None:
        """Regression guard: complete coverage is untouched by all of the above."""
        results = {
            "design_review": {"overall_score": 8.0},
            "usability_review": {"overall_score": 6.0},
            "content_quality_review": {"overall_score": 7.0},
            "feature_review": {"feature_completeness_score": 7.0},
        }

        agg = compute_aggregate_score(results, _FOUR_EQUAL_WEIGHTS)

        assert agg["aggregate_score"] == 7.0
        assert agg["grade"] == "B"
        assert agg["per_review_scores"] == {
            "design_review": 8.0,
            "usability_review": 6.0,
            "content_quality_review": 7.0,
            "feature_review": 7.0,
        }
        assert agg["coverage"] == 1.0
        assert agg["partial"] is False

        summary = generate_summary("job-1", results, _FOUR_EQUAL_WEIGHTS)
        assert summary["aggregate_score"] == 7.0
        assert summary["grade"] == "B"
        assert summary["score_100"] == 70.0


class TestCoverageIsMeasuredAgainstWhatTheJobRan:
    """The denominator is what the job ATTEMPTED, not the service's catalogue.

    Caught by a sibling test in CI, not by this file: `aggregate_weights` is
    `settings.scoring.aggregate_weights` at every call site, so measuring
    coverage against it refuses a job that legitimately requested ONE review
    type -- coverage ~0.25 against the full catalogue, under the floor, no score
    for a completely successful run. That is the over-correction this floor was
    supposed to avoid, in the opposite direction.
    """

    def test_a_single_requested_review_that_succeeded_still_scores(self) -> None:
        weights = {"design_review": 0.25, "seo": 0.25, "content": 0.25, "ux": 0.25}
        out = compute_aggregate_score({"design_review": {"overall_score": 5.5}}, weights)
        assert out["coverage"] == 1.0
        assert out["aggregate_score"] == 5.5
        assert out["grade"] is not None

    def test_a_job_that_ran_four_and_scored_one_is_still_refused(self) -> None:
        # The floor must keep working for the case it exists for.
        weights = {"design_review": 0.25, "seo": 0.25, "content": 0.25, "ux": 0.25}
        results = {
            "design_review": {"overall_score": 5.5},
            "seo": {"error": "boom"},
            "content": {"error": "boom"},
            "ux": {"error": "boom"},
        }
        out = compute_aggregate_score(results, weights)
        assert out["coverage"] == 0.25
        assert out["aggregate_score"] is None
