"""HTML report generator for critique results.

Generates self-contained HTML reports with embedded CSS and inline SVG charts.
"""

from __future__ import annotations

import math
from collections import defaultdict

from src.domains.critique.services.report_generator import (
    OVERALL_NOT_SCORED,
    display_category,
    overall_is_unscored,
    score_coverage_note,
    score_to_grade,
    unscored_categories,
    unscored_label,
)

# Severity badge colors
SEVERITY_COLORS = {
    "critical": "#dc2626",  # red
    "high": "#ea580c",  # orange
    "medium": "#d97706",  # amber
    "low": "#2563eb",  # blue
    "info": "#6b7280",  # gray
}

GRADE_COLORS = {
    "A+": "#16a34a",
    "A": "#22c55e",
    "B": "#84cc16",
    "C": "#eab308",
    "D": "#f97316",
    "F": "#ef4444",
}


# Canonical grade mapping lives in report_generator; kept as a module-level
# alias here so existing callers (and exports.py) continue to import it.
_score_grade = score_to_grade


_RADAR_LABEL_PAD = 60


def _generate_radar_svg(scores: dict[str, float], size: int = 300) -> str:
    """Generate an inline SVG radar chart for category scores."""
    if not scores:
        return ""

    categories = sorted(scores.keys())
    n = len(categories)
    if n < 3:
        # Need at least 3 points for a radar chart
        return ""

    cx, cy = size // 2, size // 2
    radius = size // 2 - 40
    angle_step = 2 * math.pi / n

    # Side labels ("Performance", "Accessibility") are anchored start/end at
    # the chart edge and overflow a square viewBox, where they get clipped in
    # the PDF. Widen the canvas horizontally so they always fit.
    pad = _RADAR_LABEL_PAD
    width = size + 2 * pad
    lines = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{size}"'
        f' viewBox="{-pad} 0 {width} {size}">'
    )
    lines.append(f'<rect x="{-pad}" width="{width}" height="{size}" fill="white" rx="8"/>')

    # Draw grid circles
    for level in [0.25, 0.5, 0.75, 1.0]:
        r = radius * level
        lines.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.0f}" fill="none"'
            f' stroke="#e5e7eb" stroke-width="1"/>'
        )

    # Draw axis lines and labels
    for i, cat in enumerate(categories):
        angle = -math.pi / 2 + i * angle_step
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        lines.append(
            f'<line x1="{cx}" y1="{cy}" x2="{x:.0f}" y2="{y:.0f}"'
            f' stroke="#e5e7eb" stroke-width="1"/>'
        )
        # Label
        lx = cx + (radius + 20) * math.cos(angle)
        ly = cy + (radius + 20) * math.sin(angle)
        anchor = "middle"
        if math.cos(angle) > 0.3:
            anchor = "start"
        elif math.cos(angle) < -0.3:
            anchor = "end"
        label = display_category(cat)
        lines.append(
            f'<text x="{lx:.0f}" y="{ly:.0f}" text-anchor="{anchor}"'
            f' font-size="11" fill="#374151" font-family="sans-serif">'
            f"{label}</text>"
        )

    # Draw data polygon
    points = []
    for i, cat in enumerate(categories):
        score = scores.get(cat, 0) / 100.0
        angle = -math.pi / 2 + i * angle_step
        x = cx + radius * score * math.cos(angle)
        y = cy + radius * score * math.sin(angle)
        points.append(f"{x:.0f},{y:.0f}")

    polygon = " ".join(points)
    lines.append(
        f'<polygon points="{polygon}" fill="rgba(59, 130, 246, 0.2)"'
        f' stroke="#3b82f6" stroke-width="2"/>'
    )

    # Draw score dots
    for i, cat in enumerate(categories):
        score = scores.get(cat, 0) / 100.0
        angle = -math.pi / 2 + i * angle_step
        x = cx + radius * score * math.cos(angle)
        y = cy + radius * score * math.sin(angle)
        lines.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="#3b82f6"/>')

    lines.append("</svg>")
    return "\n".join(lines)


_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _bucket_findings(
    findings: list[dict],
) -> tuple[dict[str, int], dict[str, list[dict]], list[dict]]:
    """Build severity counts, category buckets, and the top-10 prioritized issues."""
    severity_counts: dict[str, int] = defaultdict(int)
    by_category: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        severity_counts[f.get("severity", "info")] += 1
        by_category[f.get("category", "other")].append(f)
    for cat in by_category:
        by_category[cat].sort(key=lambda x: _SEVERITY_ORDER.get(x.get("severity", "info"), 5))
    top_issues = sorted(findings, key=lambda x: _SEVERITY_ORDER.get(x.get("severity", "info"), 5))[
        :10
    ]
    return severity_counts, by_category, top_issues


def generate_html_report(
    url: str,
    findings: list[dict],
    scores: dict[str, float],
    summary: str,
    overall_score: float | None = None,
    scan_duration: float | None = None,
    github_repo: str | None = None,
    confidence_scores: dict[str, float] | None = None,
) -> str:
    """Generate a self-contained HTML critique report.

    A category with findings but no verified evidence basis (see
    ``unscored_categories``) gets no number and no grade anywhere: its score card
    and heading say "Not scored", it is left off the radar chart, and the report
    states how many categories the overall score is based on.
    """
    unscored_cats = unscored_categories(findings)
    scores = {c: s for c, s in scores.items() if c not in unscored_cats}
    coverage = score_coverage_note(scores, unscored_cats)

    if overall_score is None and scores:
        overall_score = sum(scores.values()) / len(scores)
    if overall_score is None:
        overall_score = 0.0

    if overall_is_unscored(scores, unscored_cats):
        # No category has verified evidence: no overall number and no grade, in any
        # format -- a stored 100.0 here would print "100.0 (A+) based on 0 of N".
        grade = "&ndash;"
        grade_color = "#6b7280"
        score_text = OVERALL_NOT_SCORED
    else:
        grade = _score_grade(overall_score)
        grade_color = GRADE_COLORS.get(grade, "#6b7280")
        score_text = f"{overall_score:.1f}/100"

    severity_counts, by_category, top_issues = _bucket_findings(findings)
    radar_svg = _generate_radar_svg(scores)

    # Build HTML
    html_parts = []
    html_parts.append(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Website Critique Report: {_escape(url)}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; \
line-height: 1.6; color: #1f2937; background: #f9fafb; padding: 2rem; }}
.container {{ max-width: 900px; margin: 0 auto; background: white; border-radius: 12px; \
box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden; }}
.header {{ background: linear-gradient(135deg, #1e40af, #3b82f6); color: white; \
padding: 2rem; }}
.header h1 {{ font-size: 1.5rem; margin-bottom: 0.5rem; }}
.header .url {{ opacity: 0.9; font-size: 0.9rem; word-break: break-all; }}
.header .meta {{ margin-top: 0.5rem; font-size: 0.85rem; opacity: 0.8; }}
.grade-badge {{ display: block; float: right; width: 64px; height: 64px; \
line-height: 64px; text-align: center; border-radius: 50%; font-size: 1.5rem; \
font-weight: 700; color: white; margin-top: -0.5rem; }}
.section {{ padding: 1.5rem 2rem; border-bottom: 1px solid #e5e7eb; }}
.section h2 {{ font-size: 1.2rem; margin-bottom: 1rem; color: #111827; }}
.score-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); \
gap: 1rem; break-inside: avoid; }}
.score-card {{ text-align: center; padding: 1rem; border-radius: 8px; \
background: #f3f4f6; }}
.score-card .score {{ font-size: 1.5rem; font-weight: 700; }}
.score-card .label {{ font-size: 0.8rem; color: #6b7280; text-transform: capitalize; }}
.unverified-badge {{ font-size: 0.7em; color: #6b7280; border: 1px solid #d1d5db; \
border-radius: 4px; padding: 1px 6px; vertical-align: middle; }}
.severity-badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; \
font-size: 0.75rem; font-weight: 600; color: white; text-transform: uppercase; }}
.finding {{ break-inside: avoid; padding: 1rem; margin-bottom: 0.75rem; border-radius: 8px; \
border-left: 4px solid #e5e7eb; background: #f9fafb; }}
.finding h4 {{ font-size: 0.95rem; margin-bottom: 0.25rem; }}
.finding p {{ font-size: 0.85rem; color: #4b5563; margin-bottom: 0.25rem; }}
.finding .recommendation {{ font-size: 0.85rem; color: #059669; font-style: italic; }}
details {{ margin-top: 0.5rem; }}
details summary {{ cursor: pointer; font-size: 0.85rem; color: #6b7280; }}
details summary:hover {{ color: #374151; }}
.confidence-bar {{ display: inline-block; width: 50px; height: 8px; background: #e5e7eb; \
border-radius: 4px; margin-left: 0.5rem; vertical-align: middle; }}
.confidence-fill {{ display: block; height: 100%; border-radius: 4px; background: #3b82f6; }}
.summary {{ white-space: pre-wrap; color: #374151; }}
.chart-container {{ text-align: center; margin: 1rem 0; }}
.stats {{ display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; }}
.stat {{ padding: 0.5rem 1rem; border-radius: 6px; font-size: 0.85rem; \
font-weight: 600; }}
.footer {{ padding: 1rem 2rem; text-align: center; font-size: 0.8rem; \
color: #9ca3af; }}
</style>
</head>
<body>
<div class="container">
""")

    # Header with grade
    duration_text = f" in {scan_duration:.1f}s" if scan_duration else ""
    html_parts.append(f"""<div class="header">
<div class="grade-badge" style="background-color: {grade_color}">{grade}</div>
<h1>Website Critique Report</h1>
<div class="url">{_escape(url)}</div>
<div class="meta">\
Score: {score_text}{f" ({coverage})" if coverage else ""} | \
{len(findings)} findings{duration_text}</div>
</div>""")

    # Executive Summary
    html_parts.append(f"""<div class="section">
<h2>Executive Summary</h2>
<div class="summary">{_escape(summary)}</div>
<div class="stats">""")

    html_parts.extend(_render_severity_stats(severity_counts))
    html_parts.append("</div></div>")
    unscored = _unverified_counts(findings)
    html_parts.extend(
        _render_category_scores(
            scores, radar_svg, confidence_scores, unscored, unscored_cats, coverage
        )
    )
    html_parts.extend(_render_top_issues(top_issues))
    html_parts.extend(
        _render_findings_by_category(findings, by_category, scores, unscored, unscored_cats)
    )
    if github_repo:
        html_parts.append(
            f'<div class="section"><h2>Code Quality</h2>'
            f'<p>Repository: <a href="{_escape(github_repo)}">'
            f"{_escape(github_repo)}</a></p></div>"
        )
    html_parts.append('<div class="footer">Generated by Critique Service</div>')
    html_parts.append("</div></body></html>")
    return "\n".join(html_parts)


def _render_severity_stats(severity_counts: dict[str, int]) -> list[str]:
    out: list[str] = []
    for sev in ["critical", "high", "medium", "low", "info"]:
        count = severity_counts.get(sev, 0)
        if count:
            color = SEVERITY_COLORS.get(sev, "#6b7280")
            out.append(
                f'<span class="stat" style="background: {color}20;'
                f' color: {color}">{count} {sev}</span>'
            )
    return out


def _unverified_counts(findings: list[dict]) -> dict[str, int]:
    """Per category, the LLM findings the page could not confirm (listed, never scored)."""
    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        if f.get("verified") is False:
            counts[f.get("category", "other")] += 1
    return counts


def _unscored_note(count: int) -> str:
    """Printed text, not a tooltip: a PDF has no hover, so the caveat must be on the page."""
    return f" ({count} unverified, not scored)" if count else ""


def _render_category_scores(
    scores: dict[str, float],
    radar_svg: str,
    confidence_scores: dict[str, float] | None,
    unscored: dict[str, int],
    unscored_cats: dict[str, int],
    coverage: str,
) -> list[str]:
    if not scores and not unscored_cats:
        return []
    out: list[str] = ['<div class="section"><h2>Category Scores</h2>']
    if radar_svg:
        out.append(f'<div class="chart-container">{radar_svg}</div>')
    if coverage:
        names = ", ".join(display_category(c) for c in unscored_cats)
        out.append(
            f'<p class="coverage-note" style="font-size:0.85rem;color:#4b5563">'
            f"{coverage}. Not scored (no verified evidence): {_escape(names)}.</p>"
        )
    out.append('<div class="score-grid">')
    for cat, score in sorted(scores.items()):
        cat_grade = _score_grade(score)
        cat_color = GRADE_COLORS.get(cat_grade, "#6b7280")
        label = display_category(cat)
        confidence_html = ""
        if confidence_scores and cat in confidence_scores:
            pct = int(confidence_scores[cat] * 100)
            confidence_html = (
                f'<div style="font-size:0.75rem;color:#6b7280;margin-top:0.25rem">'
                f"Confidence: {pct}%"
                f'<span class="confidence-bar">'
                f'<span class="confidence-fill" style="width:{pct}%"></span>'
                f"</span></div>"
            )
        out.append(
            f'<div class="score-card">'
            f'<div class="score" style="color: {cat_color}">{score}</div>'
            f'<div class="label">{label} ({cat_grade})</div>'
            f"{_unscored_card_note(unscored.get(cat, 0))}"
            f"{confidence_html}</div>"
        )
    for cat, count in unscored_cats.items():
        out.append(
            f'<div class="score-card not-scored">'
            f'<div class="score" style="color: #9ca3af">&mdash;</div>'
            f'<div class="label">{display_category(cat)}</div>'
            f'<div class="unscored-note" style="font-size:0.75rem;color:#6b7280">'
            f"{unscored_label(count)}</div></div>"
        )
    out.append("</div></div>")
    return out


def _unscored_card_note(count: int) -> str:
    if not count:
        return ""
    return (
        f'<div class="unscored-note" style="font-size:0.75rem;color:#6b7280">'
        f"{count} unverified, not scored</div>"
    )


def _render_finding_card(f: dict, *, with_details: bool) -> list[str]:
    sev = f.get("severity", "info")
    color = SEVERITY_COLORS.get(sev, "#6b7280")
    rec = f.get("recommendation", "")
    # An LLM claim the page could not confirm is listed but carries no score penalty.
    unverified = (
        ' <span class="unverified-badge" title="Not confirmed on the page; not scored">'
        "unverified</span>"
        if f.get("verified") is False
        else ""
    )
    out = [
        f'<div class="finding" style="border-left-color: {color}">',
        (
            f'<h4><span class="severity-badge" style="background: {color}">'
            f"{sev}</span> {_escape(f.get('title', ''))}{unverified}</h4>"
        ),
    ]
    if with_details:
        out.append("<details><summary>Details &amp; Recommendation</summary>")
    out.append(f"<p>{_escape(f.get('description', ''))}</p>")
    if rec:
        out.append(f'<p class="recommendation">{_escape(rec)}</p>')
    if with_details:
        out.append("</details>")
    out.append("</div>")
    return out


def _render_top_issues(top_issues: list[dict]) -> list[str]:
    if not top_issues:
        return []
    out = ['<div class="section"><h2>Top Prioritized Issues</h2>']
    for f in top_issues:
        out.extend(_render_finding_card(f, with_details=False))
    out.append("</div>")
    return out


def _render_findings_by_category(
    findings: list[dict],
    by_category: dict[str, list[dict]],
    scores: dict[str, float],
    unscored: dict[str, int],
    unscored_cats: dict[str, int],
) -> list[str]:
    if not findings:
        return []
    out: list[str] = []
    for cat, cat_findings in sorted(by_category.items()):
        cat_label = display_category(cat)
        cat_score = scores.get(cat)
        if cat in unscored_cats:
            score_text = f" ({unscored_label(unscored_cats[cat])})"
        else:
            score_text = f" (Score: {cat_score})" if cat_score is not None else ""
            score_text += _unscored_note(unscored.get(cat, 0))
        out.append(
            f'<div class="section"><h2>{cat_label}{score_text} - {len(cat_findings)} findings</h2>'
        )
        for f in cat_findings:
            out.extend(_render_finding_card(f, with_details=True))
        out.append("</div>")
    return out


def _escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
