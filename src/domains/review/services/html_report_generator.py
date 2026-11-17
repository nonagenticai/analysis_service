"""Generate styled HTML reports from review results.

Produces a self-contained HTML document with embedded CSS (no JavaScript)
that can be served directly or saved as an .html file.
"""

from __future__ import annotations

import html
import logging

from src.domains.review.services.scoring import score_to_grade
from src.domains.review.services.text_utils import extract_overall_score

logger = logging.getLogger(__name__)


def generate_html_report(
    job_id: str,
    results: dict[str, dict],
    summary: dict,
) -> str:
    """Build a styled HTML report for a completed review job.

    Args:
        job_id: The unique job identifier.
        results: Mapping of review_type -> review result dict.
        summary: Summary dict (aggregate_score, grade, top_strengths, etc.).

    Returns:
        A complete, self-contained HTML string with embedded CSS.
    """
    aggregate_score = summary.get("aggregate_score")
    grade = summary.get("grade") or (
        score_to_grade(aggregate_score) if aggregate_score is not None else "N/A"
    )
    score_display = f"{aggregate_score}/10" if aggregate_score is not None else "N/A"

    sections = _build_review_sections(results)
    executive = _build_executive_summary(summary)

    return _TEMPLATE.format(
        job_id=_esc(job_id),
        score_display=_esc(score_display),
        grade=_esc(str(grade)),
        grade_colour=_grade_colour(str(grade)),
        review_sections=sections,
        executive_summary=executive,
    )


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

_GRADE_COLOURS: dict[str, str] = {
    "A": "#16a34a",  # green
    "B": "#2563eb",  # blue
    "C": "#ca8a04",  # yellow / amber
    "D": "#ea580c",  # orange
    "F": "#dc2626",  # red
}


def _grade_colour(grade: str) -> str:
    """Return the CSS colour for a grade badge."""
    if not grade or grade == "N/A":
        return "#6b7280"  # grey
    first = grade[0].upper()
    return _GRADE_COLOURS.get(first, "#6b7280")


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_review_sections(results: dict[str, dict]) -> str:
    """Generate HTML for each review type section."""
    parts: list[str] = []
    for review_type, result in results.items():
        title = _humanise(review_type)

        if not isinstance(result, dict) or "error" in result:
            error_msg = result.get("error", "Unknown error") if isinstance(result, dict) else "N/A"
            parts.append(
                f'<div class="review-section">'
                f"<h2>{_esc(title)}</h2>"
                f'<p class="error">Review failed: {_esc(str(error_msg))}</p>'
                f"</div>"
            )
            continue

        # See report_generator: an ``or`` chain drops a legitimate 0.0.
        score = extract_overall_score(result)
        section_lines: list[str] = ['<div class="review-section">', f"<h2>{_esc(title)}</h2>"]

        if score is not None:
            review_grade = score_to_grade(float(score))
            colour = _grade_colour(review_grade)
            section_lines.append(
                f'<div class="score-row">'
                f'<span class="score">{_esc(str(score))}/10</span>'
                f'<span class="badge" style="background:{colour};">'
                f"{_esc(review_grade)}</span>"
                f"</div>"
            )

        # Subsections
        for key, heading in (
            ("strengths", "Strengths"),
            ("critical_issues", "Critical Issues"),
            ("weaknesses", "Weaknesses"),
            ("issues", "Issues"),
            ("recommendations", "Recommendations"),
            ("quick_wins", "Quick Wins"),
            ("long_term_improvements", "Long-Term Improvements"),
        ):
            items = result.get(key, [])
            if items:
                section_lines.append(f"<h3>{_esc(heading)}</h3><ul>")
                for item in items:
                    section_lines.append(f"<li>{_esc(_item_text(item))}</li>")
                section_lines.append("</ul>")

        section_lines.append("</div>")
        parts.append("\n".join(section_lines))

    return "\n".join(parts)


def _build_executive_summary(summary: dict) -> str:
    """Generate HTML for the executive summary block."""
    parts: list[str] = ['<div class="executive-summary">', "<h2>Executive Summary</h2>"]

    for key, heading in (
        ("top_strengths", "Top Strengths"),
        ("critical_issues", "Critical Issues"),
        ("top_recommendations", "Top Recommendations"),
    ):
        items = summary.get(key, [])
        if items:
            parts.append(f"<h3>{_esc(heading)}</h3><ul>")
            for item in items:
                parts.append(f"<li>{_esc(str(item))}</li>")
            parts.append("</ul>")

    parts.append("</div>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _humanise(review_type: str) -> str:
    return review_type.replace("_", " ").title()


def _item_text(item: str | dict) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return (
            item.get("description")
            or item.get("title")
            or item.get("text")
            or item.get("name")
            or str(item)
        )
    return str(item)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review Report: {job_id}</title>
<style>
  :root {{
    --bg: #f9fafb;
    --card: #ffffff;
    --text: #111827;
    --muted: #6b7280;
    --border: #e5e7eb;
    --green: #16a34a;
    --blue: #2563eb;
    --yellow: #ca8a04;
    --orange: #ea580c;
    --red: #dc2626;
  }}
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 2rem;
    max-width: 960px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 1.75rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.35rem; margin-bottom: 0.5rem; border-bottom: 1px solid var(--border);
        padding-bottom: 0.35rem; }}
  h3 {{ font-size: 1.05rem; margin: 0.75rem 0 0.35rem; color: var(--muted); }}
  .header {{ margin-bottom: 2rem; }}
  .header .meta {{ color: var(--muted); font-size: 0.9rem; }}
  .badge {{
    display: inline-block;
    padding: 0.15rem 0.6rem;
    border-radius: 0.35rem;
    color: #fff;
    font-weight: 700;
    font-size: 0.95rem;
    vertical-align: middle;
  }}
  .score-row {{
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }}
  .score {{ font-size: 1.15rem; font-weight: 600; }}
  .review-section {{
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    padding: 1.25rem;
    margin-bottom: 1.25rem;
  }}
  .executive-summary {{
    background: var(--card);
    border: 2px solid var(--border);
    border-radius: 0.5rem;
    padding: 1.25rem;
    margin-top: 2rem;
  }}
  ul {{ padding-left: 1.25rem; margin-bottom: 0.5rem; }}
  li {{ margin-bottom: 0.25rem; }}
  .error {{ color: var(--red); font-style: italic; }}
</style>
</head>
<body>
<div class="header">
  <h1>Review Report</h1>
  <p class="meta">Job ID: {job_id}</p>
  <div class="score-row">
    <span class="score">{score_display}</span>
    <span class="badge" style="background:{grade_colour};">{grade}</span>
  </div>
</div>

{review_sections}

{executive_summary}

</body>
</html>"""
