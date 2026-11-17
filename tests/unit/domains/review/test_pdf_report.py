"""Tests for src.services.pdf_report_generator -- PDF report generation."""

import builtins
from unittest.mock import MagicMock, patch

import pytest

from src.domains.review.services.pdf_report_generator import generate_pdf_report


class TestGeneratePdfReport:
    def _sample_results(self) -> dict:
        return {
            "design_review": {
                "overall_score": 7.5,
                "strengths": ["Good layout"],
                "weaknesses": ["Poor contrast"],
                "recommendations": ["Improve contrast"],
            }
        }

    def _sample_summary(self) -> dict:
        return {
            "job_id": "test-job",
            "aggregate_score": 7.5,
            "grade": "B+",
            "per_review_scores": {"design_review": 7.5},
            "top_strengths": ["Good layout"],
            "critical_issues": [],
            "top_recommendations": ["Improve contrast"],
        }

    def test_generates_pdf_bytes(self):
        """PDF should be generated as bytes starting with %PDF."""
        mock_pdf = b"%PDF-1.4 fake pdf content"
        mock_html_instance = MagicMock()
        mock_html_instance.write_pdf.return_value = mock_pdf

        # Patch the lazy import inside the function
        mock_weasyprint = MagicMock()
        mock_weasyprint.HTML.return_value = mock_html_instance

        with patch.dict("sys.modules", {"weasyprint": mock_weasyprint}):
            result = generate_pdf_report("test-job", self._sample_results(), self._sample_summary())

        assert isinstance(result, bytes)
        assert result.startswith(b"%PDF")

    def test_requires_html_report_input(self):
        """PDF generator should call generate_html_report internally."""
        mock_pdf = b"%PDF-1.4 content"
        mock_html_instance = MagicMock()
        mock_html_instance.write_pdf.return_value = mock_pdf

        mock_weasyprint = MagicMock()
        mock_weasyprint.HTML.return_value = mock_html_instance

        with (
            patch.dict("sys.modules", {"weasyprint": mock_weasyprint}),
            patch(
                "src.domains.review.services.pdf_report_generator.generate_html_report",
                return_value="<html>test</html>",
            ) as mock_gen_html,
        ):
            generate_pdf_report("test-job", self._sample_results(), self._sample_summary())
            mock_gen_html.assert_called_once_with(
                "test-job", self._sample_results(), self._sample_summary()
            )
            mock_weasyprint.HTML.assert_called_once_with(string="<html>test</html>")

    def test_empty_results_still_generates_pdf(self):
        """Even with empty results, PDF should be generated."""
        mock_pdf = b"%PDF-1.4 empty"
        mock_html_instance = MagicMock()
        mock_html_instance.write_pdf.return_value = mock_pdf

        mock_weasyprint = MagicMock()
        mock_weasyprint.HTML.return_value = mock_html_instance

        summary = {
            "job_id": "empty-job",
            "aggregate_score": None,
            "grade": None,
            "per_review_scores": {},
            "top_strengths": [],
            "critical_issues": [],
            "top_recommendations": [],
        }

        with patch.dict("sys.modules", {"weasyprint": mock_weasyprint}):
            result = generate_pdf_report("empty-job", {}, summary)

        assert isinstance(result, bytes)

    def test_raises_import_error_when_weasyprint_missing(self):
        """Cover lines 44-46: ImportError raised when weasyprint is not installed."""
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "weasyprint":
                raise ImportError("No module named 'weasyprint'")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=mock_import),
            pytest.raises(ImportError, match="weasyprint"),
        ):
            generate_pdf_report("test-job", self._sample_results(), self._sample_summary())
