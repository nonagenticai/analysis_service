"""PDF report generator for critique results.

Converts HTML reports to PDF using WeasyPrint or xhtml2pdf.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def html_to_pdf(html: str) -> bytes:
    """Convert HTML report to PDF bytes.

    Tries WeasyPrint first, falls back to xhtml2pdf.
    """
    try:
        from weasyprint import HTML

        return HTML(string=html).write_pdf()
    except ImportError:
        pass

    try:
        import io

        from xhtml2pdf import pisa  # optional dep

        result = io.BytesIO()
        pisa_status = pisa.CreatePDF(io.StringIO(html), dest=result)
        if pisa_status.err:
            raise RuntimeError(f"PDF generation failed with {pisa_status.err} errors")
        return result.getvalue()
    except ImportError, RuntimeError:
        raise RuntimeError(
            "Neither weasyprint nor xhtml2pdf is installed. "
            "Install one: pip install weasyprint or pip install xhtml2pdf"
        ) from None
