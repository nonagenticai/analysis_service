"""Tests for src.services.comparison -- compare two review job results."""

from src.domains.review.services.comparison import compare_reviews


class TestCompareReviews:
    def test_empty_results(self):
        result = compare_reviews({}, {})
        assert result["score_deltas"] == {}
        assert result["overall_delta"] is None
        assert result["grade_change"] is None
        assert result["new_strengths"] == []
        assert result["resolved_weaknesses"] == []
        assert result["persistent_issues"] == 0

    def test_score_deltas(self):
        job1 = {"design_review": {"overall_score": 6.0}}
        job2 = {"design_review": {"overall_score": 8.0}}
        result = compare_reviews(job1, job2)
        assert result["score_deltas"]["design_review"] == 2.0

    def test_overall_delta(self):
        job1 = {
            "design_review": {"overall_score": 5.0},
            "usability_review": {"overall_score": 7.0},
        }
        job2 = {
            "design_review": {"overall_score": 7.0},
            "usability_review": {"overall_score": 9.0},
        }
        result = compare_reviews(job1, job2)
        assert result["overall_delta"] == 2.0

    def test_grade_change(self):
        job1 = {"design_review": {"overall_score": 5.5}}
        job2 = {"design_review": {"overall_score": 9.0}}
        result = compare_reviews(job1, job2)
        assert result["grade_change"] is not None
        assert "->" in result["grade_change"]

    def test_no_grade_change_when_same(self):
        job1 = {"design_review": {"overall_score": 7.0}}
        job2 = {"design_review": {"overall_score": 7.0}}
        result = compare_reviews(job1, job2)
        assert result["grade_change"] is None

    def test_new_strengths(self):
        job1 = {"design_review": {"strengths": ["Good layout"]}}
        job2 = {"design_review": {"strengths": ["Good layout", "Great colors"]}}
        result = compare_reviews(job1, job2)
        assert "Great colors" in result["new_strengths"]

    def test_resolved_weaknesses(self):
        job1 = {"design_review": {"weaknesses": ["Poor contrast", "Bad spacing"]}}
        job2 = {"design_review": {"weaknesses": ["Bad spacing"]}}
        result = compare_reviews(job1, job2)
        assert "Poor contrast" in result["resolved_weaknesses"]

    def test_persistent_issues(self):
        job1 = {"design_review": {"weaknesses": ["Bad spacing", "Poor contrast"]}}
        job2 = {"design_review": {"weaknesses": ["Bad spacing"]}}
        result = compare_reviews(job1, job2)
        assert result["persistent_issues"] == 1

    def test_error_results_skipped(self):
        job1 = {"design_review": {"error": "timeout"}}
        job2 = {"design_review": {"overall_score": 8.0}}
        result = compare_reviews(job1, job2)
        assert result["score_deltas"]["design_review"] is None

    def test_computed_overall_score_preferred(self):
        job1 = {"design_review": {"overall_score": 5.0, "computed_overall_score": 6.0}}
        job2 = {"design_review": {"overall_score": 7.0, "computed_overall_score": 8.0}}
        result = compare_reviews(job1, job2)
        assert result["score_deltas"]["design_review"] == 2.0
