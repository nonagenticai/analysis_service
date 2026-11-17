"""Tests for src.services.summary -- executive summary generation."""

from src.domains.review.services.summary import _deduplicate, generate_summary


class TestGenerateSummary:
    def _sample_results(self) -> dict:
        return {
            "design_review": {
                "overall_score": 7.0,
                "strengths": ["Good layout", "Clean design"],
                "weaknesses": ["Poor contrast"],
                "recommendations": ["Improve contrast"],
            },
            "usability_review": {
                "overall_score": 8.0,
                "strengths": ["Easy navigation"],
                "critical_issues": ["Missing alt text"],
                "quick_wins": ["Add skip nav"],
                "long_term_improvements": ["Keyboard navigation"],
            },
        }

    def _default_weights(self) -> dict:
        return {
            "design_review": 0.5,
            "usability_review": 0.5,
        }

    def test_aggregate_score_computed(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert summary["aggregate_score"] == 7.5
        assert summary["grade"] == "B+"

    def test_per_review_scores(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert summary["per_review_scores"]["design_review"] == 7.0
        assert summary["per_review_scores"]["usability_review"] == 8.0

    def test_top_strengths_collected(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert "Good layout" in summary["top_strengths"]
        assert "Easy navigation" in summary["top_strengths"]

    def test_critical_issues_collected(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert "Poor contrast" in summary["critical_issues"]
        assert "Missing alt text" in summary["critical_issues"]

    def test_recommendations_collected(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert "Improve contrast" in summary["top_recommendations"]
        assert "Add skip nav" in summary["top_recommendations"]

    def test_score_100_included(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert summary["score_100"] == 75.0

    def test_penalty_score_included(self):
        summary = generate_summary("job-1", self._sample_results(), self._default_weights())
        assert summary["penalty_score"] is not None
        assert 50.0 <= summary["penalty_score"] <= 100.0

    def test_empty_results(self):
        summary = generate_summary("job-empty", {}, self._default_weights())
        assert summary["aggregate_score"] is None
        assert summary["grade"] is None
        assert summary["score_100"] is None
        assert summary["penalty_score"] is None

    def test_error_results_skipped(self):
        results = {
            "design_review": {"error": "timeout"},
            "usability_review": {"overall_score": 8.0, "strengths": ["Good"]},
        }
        summary = generate_summary("job-err", results, self._default_weights())
        assert summary["per_review_scores"]["design_review"] is None
        assert summary["per_review_scores"]["usability_review"] == 8.0

    def test_job_id_in_summary(self):
        summary = generate_summary("my-job-id", self._sample_results(), self._default_weights())
        assert summary["job_id"] == "my-job-id"


class TestDeduplicate:
    def test_basic_dedup(self):
        items = ["a", "b", "a", "c"]
        result = _deduplicate(items)
        assert result == ["a", "b", "c"]

    def test_case_insensitive(self):
        items = ["Hello", "hello", "HELLO"]
        result = _deduplicate(items)
        assert len(result) == 1

    def test_max_items(self):
        items = [str(i) for i in range(20)]
        result = _deduplicate(items, max_items=5)
        assert len(result) == 5

    def test_empty_list(self):
        assert _deduplicate([]) == []

    def test_preserves_order(self):
        items = ["c", "a", "b"]
        result = _deduplicate(items)
        assert result == ["c", "a", "b"]
