"""Tests for src.services.html_report_generator -- HTML report generation."""

from src.domains.review.services.html_report_generator import (
    _grade_colour,
    generate_html_report,
)


class TestGradeColour:
    def test_a_grade_green(self):
        assert _grade_colour("A+") == "#16a34a"
        assert _grade_colour("A") == "#16a34a"
        assert _grade_colour("A-") == "#16a34a"

    def test_b_grade_blue(self):
        assert _grade_colour("B+") == "#2563eb"

    def test_c_grade_yellow(self):
        assert _grade_colour("C") == "#ca8a04"

    def test_d_grade_orange(self):
        assert _grade_colour("D-") == "#ea580c"

    def test_f_grade_red(self):
        assert _grade_colour("F") == "#dc2626"

    def test_na_grade_grey(self):
        assert _grade_colour("N/A") == "#6b7280"

    def test_empty_grade_grey(self):
        assert _grade_colour("") == "#6b7280"


class TestGenerateHtmlReport:
    def _sample_results(self) -> dict:
        return {
            "design_review": {
                "overall_score": 8.5,
                "strengths": ["Clean layout"],
                "weaknesses": ["Minor issues"],
                "recommendations": ["Polish details"],
            }
        }

    def _sample_summary(self) -> dict:
        return {
            "job_id": "html-test",
            "aggregate_score": 8.5,
            "grade": "A",
            "per_review_scores": {"design_review": 8.5},
            "top_strengths": ["Clean layout"],
            "critical_issues": [],
            "top_recommendations": ["Polish details"],
        }

    def test_produces_html_string(self):
        html = generate_html_report("job-1", self._sample_results(), self._sample_summary())
        assert isinstance(html, str)
        assert "<html" in html

    def test_contains_job_id(self):
        html = generate_html_report("job-1", self._sample_results(), self._sample_summary())
        assert "job-1" in html

    def test_grade_colour_in_output(self):
        html = generate_html_report("job-1", self._sample_results(), self._sample_summary())
        # A grade should use green
        assert "#16a34a" in html

    def test_executive_summary_section(self):
        html = generate_html_report("job-1", self._sample_results(), self._sample_summary())
        assert "Executive Summary" in html

    def test_html_escaping(self):
        """XSS-unsafe characters should be escaped."""
        results = {
            "design_review": {
                "overall_score": 7.0,
                "strengths": ["<script>alert('xss')</script>"],
            }
        }
        summary = {
            "aggregate_score": 7.0,
            "grade": "B",
            "top_strengths": ["<script>alert('xss')</script>"],
            "critical_issues": [],
            "top_recommendations": [],
        }
        html = generate_html_report("job-xss", results, summary)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_error_result_displayed(self):
        results = {"design_review": {"error": "API timeout"}}
        summary = {
            "aggregate_score": None,
            "grade": None,
            "top_strengths": [],
            "critical_issues": [],
            "top_recommendations": [],
        }
        html = generate_html_report("job-err", results, summary)
        assert "Review failed" in html
        assert "API timeout" in html

    def test_empty_results(self):
        summary = {
            "aggregate_score": None,
            "grade": None,
            "top_strengths": [],
            "critical_issues": [],
            "top_recommendations": [],
        }
        html = generate_html_report("empty", {}, summary)
        assert "<html" in html
        assert "Executive Summary" in html
