"""Generate PDF reports from review results.

Converts the existing HTML report to PDF via WeasyPrint.
"""

from __future__ import annotations

import logging

from src.domains.review.services.html_report_generator import generate_html_report

logger = logging.getLogger(__name__)


def generate_pdf_report(
    job_id: str,
    results: dict[str, dict],
    summary: dict,
) -> bytes:
    """Build a PDF report for a completed review job.

    Generates the HTML report first, then converts it to PDF
    using WeasyPrint.

    Args:
        job_id: The unique job identifier.
        results: Mapping of review_type -> review result dict.
        summary: Summary dict (aggregate_score, grade, top_strengths, etc.).

    Returns:
        PDF file contents as bytes.

    Raises:
        ImportError: If weasyprint is not installed.
    """
    html_content = generate_html_report(job_id, results, summary)

    try:
        from weasyprint import HTML

        pdf_bytes = HTML(string=html_content).write_pdf()
        logger.info("PDF report generated for job %s (%d bytes)", job_id, len(pdf_bytes))
        return pdf_bytes
    except ImportError:
        logger.error("weasyprint not installed -- cannot generate PDF reports")
        raise
