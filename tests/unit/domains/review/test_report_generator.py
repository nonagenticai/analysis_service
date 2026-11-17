"""Tests for src.services.report_generator -- Markdown report generation."""

from src.domains.review.services.report_generator import generate_markdown_report


class TestGenerateMarkdownReport:
    def _sample_results(self) -> dict:
        return {
            "design_review": {
                "overall_score": 7.5,
                "strengths": ["Good layout", "Clean typography"],
                "weaknesses": ["Poor contrast"],
                "recommendations": ["Improve contrast ratios"],
            },
            "usability_review": {
                "overall_score": 8.0,
                "critical_issues": ["Missing alt text"],
                "quick_wins": ["Add skip nav"],
                "long_term_improvements": ["Keyboard nav"],
            },
        }

    def _sample_summary(self) -> dict:
        return {
            "job_id": "test-report-job",
            "aggregate_score": 7.75,
            "grade": "B+",
            "per_review_scores": {"design_review": 7.5, "usability_review": 8.0},
            "top_strengths": ["Good layout"],
            "critical_issues": ["Missing alt text"],
            "top_recommendations": ["Improve contrast ratios"],
        }

    def test_produces_markdown_string(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert isinstance(report, str)
        assert len(report) > 0

    def test_header_contains_job_id(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "job-1" in report

    def test_contains_overall_score(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "7.75/10" in report
        assert "B+" in report

    def test_per_review_sections(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "Design Review" in report
        assert "Usability Review" in report

    def test_strengths_section(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "Good layout" in report
        assert "Strengths" in report

    def test_weaknesses_section(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "Poor contrast" in report

    def test_recommendations_section(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "Improve contrast ratios" in report

    def test_executive_summary_section(self):
        report = generate_markdown_report("job-1", self._sample_results(), self._sample_summary())
        assert "Executive Summary" in report

    def test_error_result_handled(self):
        results = {"design_review": {"error": "timeout"}}
        summary = {
            "aggregate_score": None,
            "grade": None,
            "top_strengths": [],
            "critical_issues": [],
            "top_recommendations": [],
        }
        report = generate_markdown_report("job-err", results, summary)
        assert "Review failed" in report
        assert "timeout" in report

    def test_empty_results(self):
        summary = {
            "aggregate_score": None,
            "grade": None,
            "top_strengths": [],
            "critical_issues": [],
            "top_recommendations": [],
        }
        report = generate_markdown_report("job-empty", {}, summary)
        assert "N/A" in report
        assert "Executive Summary" in report
