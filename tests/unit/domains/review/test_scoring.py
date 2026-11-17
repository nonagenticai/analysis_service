"""Tests for src.services.scoring -- weighted scoring and grade computation."""

import pytest

from src.domains.review.services.scoring import (
    compute_aggregate_score,
    compute_weighted_score,
    normalize_to_100,
    score_to_grade,
)


class TestScoreToGrade:
    @pytest.mark.parametrize(
        "score,expected",
        [
            (9.5, "A+"),
            (9.0, "A+"),
            (8.7, "A"),
            (8.0, "A-"),
            (7.5, "B+"),
            (7.0, "B"),
            (6.5, "B-"),
            (6.0, "C+"),
            (5.5, "C"),
            (5.0, "C-"),
            (4.5, "D+"),
            (4.0, "D"),
            (3.5, "D-"),
            (2.0, "F"),
            (0.0, "F"),
        ],
    )
    def test_grade_thresholds(self, score, expected):
        assert score_to_grade(score) == expected


class TestComputeWeightedScore:
    def test_basic_weighted_score(self):
        categories = {
            "layout_consistency": {"score": 8},
            "whitespace_usage": {"score": 6},
        }
        weights = {"layout_consistency": 0.6, "whitespace_usage": 0.4}
        result = compute_weighted_score(categories, weights)
        expected = round((8 * 0.6 + 6 * 0.4) / 1.0, 2)
        assert result == expected

    def test_missing_category_skipped(self):
        categories = {"layout_consistency": {"score": 8}}
        weights = {"layout_consistency": 0.5, "missing_cat": 0.5}
        result = compute_weighted_score(categories, weights)
        assert result == 8.0

    def test_no_valid_scores_returns_none(self):
        result = compute_weighted_score({}, {"a": 0.5})
        assert result is None

    def test_non_dict_category_skipped(self):
        categories = {"layout": "not a dict"}
        weights = {"layout": 1.0}
        result = compute_weighted_score(categories, weights)
        assert result is None


class TestComputeAggregateScore:
    def test_basic_aggregate(self):
        results = {
            "design_review": {"overall_score": 8.0},
            "usability_review": {"overall_score": 6.0},
        }
        weights = {"design_review": 0.5, "usability_review": 0.5}
        agg = compute_aggregate_score(results, weights)
        assert agg["aggregate_score"] == 7.0
        assert agg["grade"] == "B"
        assert agg["per_review_scores"]["design_review"] == 8.0
        assert agg["per_review_scores"]["usability_review"] == 6.0

    def test_error_result_excluded(self):
        results = {
            "design_review": {"overall_score": 8.0},
            "usability_review": {"error": "timeout"},
        }
        weights = {"design_review": 0.5, "usability_review": 0.5}
        agg = compute_aggregate_score(results, weights)
        assert agg["aggregate_score"] == 8.0
        assert agg["per_review_scores"]["usability_review"] is None

    def test_no_results_returns_none(self):
        agg = compute_aggregate_score({}, {"design_review": 1.0})
        assert agg["aggregate_score"] is None
        assert agg["grade"] is None

    def test_computed_score_preferred(self):
        results = {"design_review": {"overall_score": 5.0, "computed_overall_score": 8.0}}
        weights = {"design_review": 1.0}
        agg = compute_aggregate_score(results, weights)
        assert agg["per_review_scores"]["design_review"] == 8.0


class TestNormalizeTo100:
    def test_zero(self):
        assert normalize_to_100(0.0) == 0.0

    def test_ten(self):
        assert normalize_to_100(10.0) == 100.0

    def test_mid_range(self):
        assert normalize_to_100(7.0) == 70.0
