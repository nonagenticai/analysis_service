"""Resource-weight performance checks (CSS, inline styles, page weight, large SVG, CSS import)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.perf.base import _perf_finding


def check_css(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    stylesheets = soup.find_all("link", attrs={"rel": "stylesheet"})
    if len(stylesheets) > 8:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{len(stylesheets)} external stylesheets",
                description=(
                    f"Loading {len(stylesheets)} external CSS files increases HTTP requests."
                ),
                url=url,
                recommendation=("Consolidate stylesheets and consider inlining critical CSS."),
                selector='link[rel="stylesheet"]',
                check_id="perf-excessive-stylesheets",
                remediation_effort="medium",
            )
        )

    style_tags = soup.find_all("style")
    total_css = sum(len(s.string or "") for s in style_tags)
    if total_css > 50000:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"Large inline CSS: {total_css // 1000}KB",
                description=(f"Inline <style> tags contain {total_css:,} characters of CSS."),
                url=url,
                recommendation="Move large CSS blocks to external files for caching.",
                selector="style",
                check_id="perf-large-inline-css",
                remediation_effort="medium",
            )
        )

    return findings


def check_inline_styles(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    inline_styled = soup.find_all(attrs={"style": True})
    if len(inline_styled) > 20:
        findings.append(
            _perf_finding(
                severity="low",
                title=f"{len(inline_styled)} elements with inline styles",
                description=(f"{len(inline_styled)} elements use inline style attributes."),
                url=url,
                recommendation=(
                    "Move inline styles to CSS classes for maintainability and caching."
                ),
                selector="[style]",
                check_id="perf-excessive-inline-styles",
                remediation_effort="medium",
            )
        )
    return findings


def check_page_weight(soup: BeautifulSoup, html: str, url: str) -> list[dict]:
    """Check total page weight including inline styles and scripts."""
    findings: list[dict] = []

    html_bytes = len(html.encode("utf-8"))
    style_bytes = sum(len((s.string or "").encode("utf-8")) for s in soup.find_all("style"))
    inline_script_bytes = sum(
        len((s.string or "").encode("utf-8"))
        for s in soup.find_all("script")
        if isinstance(s, Tag) and not s.get("src")
    )
    total_weight = html_bytes + style_bytes + inline_script_bytes
    weight_kb = total_weight / 1024

    if total_weight > 5 * 1024 * 1024:
        findings.append(
            _perf_finding(
                severity="high",
                title="Excessive page weight",
                description=(
                    f"Total page weight is {weight_kb:.0f}KB "
                    f"({total_weight / (1024 * 1024):.1f}MB), "
                    "which exceeds 5MB. This will severely impact load times."
                ),
                url=url,
                recommendation=(
                    "Reduce page size by removing unnecessary content, "
                    "compressing assets, and deferring non-critical resources."
                ),
                check_id="perf-page-weight-excessive",
                remediation_effort="high",
            )
        )
    elif total_weight > 2 * 1024 * 1024:
        findings.append(
            _perf_finding(
                severity="medium",
                title="Large page weight",
                description=(
                    f"Total page weight is {weight_kb:.0f}KB "
                    f"({total_weight / (1024 * 1024):.1f}MB), "
                    "which exceeds 2MB."
                ),
                url=url,
                recommendation=(
                    "Reduce page size by optimizing inline content and "
                    "moving large blocks to external cacheable files."
                ),
                check_id="perf-page-weight-large",
                remediation_effort="high",
            )
        )
    elif total_weight > 1 * 1024 * 1024:
        findings.append(
            _perf_finding(
                severity="low",
                title="Moderate page weight",
                description=(
                    f"Total page weight is {weight_kb:.0f}KB "
                    f"({total_weight / (1024 * 1024):.1f}MB), "
                    "which exceeds 1MB."
                ),
                url=url,
                recommendation=("Consider reducing page size for faster initial load."),
                check_id="perf-page-weight-moderate",
                remediation_effort="high",
            )
        )

    return findings


def check_css_duplication(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for duplicated CSS selectors in <style> blocks."""
    findings: list[dict] = []
    selector_counts: dict[str, int] = {}

    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        # Extract selectors (text before {)
        selector_pattern = re.compile(r"([^{}@/]+?)\s*\{", re.DOTALL)
        for match in selector_pattern.finditer(css_text):
            selector = match.group(1).strip()
            if selector and not selector.startswith("@"):
                selector_counts[selector] = selector_counts.get(selector, 0) + 1

    duplicated = {s: c for s, c in selector_counts.items() if c >= 3}
    if duplicated:
        top_dupes = sorted(duplicated.items(), key=lambda x: -x[1])[:5]
        examples = ", ".join(f"'{s}' ({c}x)" for s, c in top_dupes)
        findings.append(
            _perf_finding(
                severity="low",
                title=f"{len(duplicated)} CSS selectors duplicated 3+ times",
                description=(
                    f"{len(duplicated)} CSS selector(s) appear 3 or more "
                    f"times in inline styles. Examples: {examples}. "
                    "Duplicated rules increase CSS size and parse time."
                ),
                url=url,
                recommendation=(
                    "Consolidate duplicate CSS selectors to reduce "
                    "stylesheet size and improve maintainability."
                ),
                selector="style",
                check_id="perf-css-duplication",
                remediation_effort="medium",
            )
        )
    return findings


def check_render_blocking_css(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for render-blocking CSS (stylesheets without media queries)."""
    findings: list[dict] = []
    head = soup.find("head")
    if not head or not isinstance(head, Tag):
        return findings

    blocking_css: list[str] = []
    for link in head.find_all("link", attrs={"rel": "stylesheet"}):
        if not isinstance(link, Tag):
            continue
        # A <noscript> fallback only applies with scripting disabled; it is not
        # fetched on a normal render (the async ``media="print"`` pattern pairs
        # every non-blocking stylesheet with exactly such a fallback).
        if link.find_parent("noscript") is not None:
            continue
        media = str(link.get("media", "")).strip().lower()
        # Non-blocking: media="print", media="(prefers-...)", etc.
        if media and media not in ("all", "screen"):
            continue
        href = str(link.get("href", ""))
        if href:
            blocking_css.append(href)

    if blocking_css:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{len(blocking_css)} render-blocking stylesheet(s)",
                description=(
                    f"{len(blocking_css)} stylesheet(s) in <head> block "
                    "rendering. First: " + blocking_css[0]
                ),
                url=url,
                recommendation=(
                    "Use media queries to make non-critical stylesheets "
                    "non-render-blocking, or inline critical CSS and load "
                    "the rest asynchronously."
                ),
                selector='head > link[rel="stylesheet"]',
                check_id="perf-render-blocking-css",
                remediation_effort="medium",
            )
        )
    return findings


def check_large_svg(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag inline <svg> elements larger than 5 KB."""
    findings: list[dict] = []
    large_svgs = 0
    threshold = 5 * 1024  # 5 KB

    for svg in soup.find_all("svg"):
        if not isinstance(svg, Tag):
            continue
        svg_size = len(str(svg).encode("utf-8"))
        if svg_size > threshold:
            large_svgs += 1

    if large_svgs:
        findings.append(
            _perf_finding(
                severity="low",
                title=f"{large_svgs} inline SVG(s) exceed 5 KB",
                description=(
                    f"{large_svgs} inline <svg> element(s) are larger "
                    "than 5 KB. Large inline SVGs bloat the HTML "
                    "document, increase parse time, and cannot be cached "
                    "independently."
                ),
                url=url,
                recommendation=(
                    "Move large SVGs to external files and reference "
                    "them with <img> tags or CSS background-image. This "
                    "allows caching and reduces HTML size."
                ),
                selector="svg",
                check_id="perf-large-inline-svg",
            )
        )
    return findings


def check_css_import(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag @import rules in <style> blocks that create request chains."""
    findings: list[dict] = []
    import_re = re.compile(r"@import\s", re.IGNORECASE)
    import_count = 0

    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        import_count += len(import_re.findall(css_text))

    if import_count:
        findings.append(
            _perf_finding(
                severity="medium",
                title=f"{import_count} CSS @import rule(s) in <style> blocks",
                description=(
                    f"Found {import_count} @import rule(s) inside inline "
                    "<style> blocks. CSS @import creates sequential "
                    "request chains because the browser cannot discover "
                    "the imported stylesheet until it finishes parsing "
                    "the parent CSS."
                ),
                url=url,
                recommendation=(
                    "Replace @import with <link rel='stylesheet'> tags "
                    "to enable parallel downloading, or bundle the CSS "
                    "into a single file."
                ),
                selector="style",
                check_id="perf-css-import-chain",
            )
        )
    return findings
