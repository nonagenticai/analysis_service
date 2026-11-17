"""Generate Markdown reports from review results.

Produces a self-contained Markdown document with an overall score header,
per-review-type sections, and an executive summary at the bottom.
"""

from __future__ import annotations

import logging

from src.domains.review.services.scoring import score_to_grade
from src.domains.review.services.text_utils import extract_item_text, extract_overall_score

logger = logging.getLogger(__name__)


def _bullet_section(heading: str, items: list) -> list[str]:
    """Build a markdown ``### heading`` section bullet-listing ``items``."""
    if not items:
        return []
    out = [f"### {heading}", ""]
    out.extend(f"- {extract_item_text(item)}" for item in items)
    out.append("")
    return out


def _review_section_lines(review_type: str, result: dict) -> list[str]:
    """Render a single review section (header + score + grouped lists)."""
    out = [f"## {_humanise(review_type)}", ""]
    if "error" in result:
        error_msg = result.get("error", "Unknown error")
        out.extend([f"*Review failed:* {error_msg}", ""])
        return out

    # ``extract_overall_score``, not an ``or`` chain: a legitimate 0.0 is a real
    # measurement that must still print as F, and an unmeasured review carries
    # an ``error`` marker so it prints no score line at all.
    score = extract_overall_score(result)
    if score is not None:
        out.append(f"**Score:** {score}/10 ({score_to_grade(float(score))})")
        out.append("")

    out.extend(_bullet_section("Strengths", result.get("strengths", [])))
    for key, heading in (
        ("critical_issues", "Critical Issues"),
        ("weaknesses", "Weaknesses"),
        ("issues", "Issues"),
    ):
        out.extend(_bullet_section(heading, result.get(key, [])))
    for key, heading in (
        ("recommendations", "Recommendations"),
        ("quick_wins", "Quick Wins"),
        ("long_term_improvements", "Long-Term Improvements"),
    ):
        out.extend(_bullet_section(heading, result.get(key, [])))

    out.extend(["---", ""])
    return out


def _executive_summary_lines(summary: dict) -> list[str]:
    out: list[str] = ["## Executive Summary", ""]
    out.extend(
        ["### Top Strengths", ""] + [f"- {s}" for s in summary.get("top_strengths", [])] + [""]
        if summary.get("top_strengths")
        else []
    )
    out.extend(
        ["### Critical Issues", ""] + [f"- {i}" for i in summary.get("critical_issues", [])] + [""]
        if summary.get("critical_issues")
        else []
    )
    out.extend(
        ["### Top Recommendations", ""]
        + [f"- {r}" for r in summary.get("top_recommendations", [])]
        + [""]
        if summary.get("top_recommendations")
        else []
    )
    return out


def generate_markdown_report(
    job_id: str,
    results: dict[str, dict],
    summary: dict,
) -> str:
    """Build a Markdown report for a completed review job.

    Args:
        job_id: The unique job identifier.
        results: Mapping of review_type -> review result dict.
        summary: Summary dict produced by ``generate_summary``, expected keys:
            aggregate_score, grade, top_strengths, critical_issues,
            top_recommendations.

    Returns:
        A complete Markdown string.
    """
    aggregate_score = summary.get("aggregate_score")
    grade = summary.get("grade") or (
        score_to_grade(aggregate_score) if aggregate_score is not None else "N/A"
    )
    score_display = f"{aggregate_score}/10" if aggregate_score is not None else "N/A"

    lines: list[str] = [
        f"# Review Report: {job_id}",
        "",
        f"**Overall Score:** {score_display}  ",
        f"**Grade:** {grade}",
        "",
        "---",
        "",
    ]
    for review_type, result in results.items():
        lines.extend(_review_section_lines(review_type, result))
    lines.extend(_executive_summary_lines(summary))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _humanise(review_type: str) -> str:
    """Turn a snake_case review type into a human-readable title."""
    return review_type.replace("_", " ").title()
