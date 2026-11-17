"""Mobile SEO checks (viewport, mobile font sizes)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo.base import _seo_finding


def check_viewport(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for viewport meta tag (mobile-first indexing context)."""
    findings: list[dict] = []
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if not viewport:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Missing viewport meta tag",
                description=(
                    "No viewport meta tag found. Google uses mobile-first "
                    "indexing, so pages without a viewport tag may rank lower."
                ),
                url=url,
                recommendation=(
                    'Add <meta name="viewport" content="width=device-width, '
                    'initial-scale=1"> to the page head.'
                ),
                selector='meta[name="viewport"]',
                check_id="seo-viewport-missing",
            )
        )
    return findings


def check_mobile_font_sizes(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag font-size values below 12px in inline styles and <style> blocks."""
    findings: list[dict] = []
    small_font_re = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)\s*px", re.IGNORECASE)
    small_fonts_found: list[str] = []

    # Check inline styles
    for elem in soup.find_all(attrs={"style": True}):
        style = str(elem.get("style", ""))
        for match in small_font_re.finditer(style):
            size = float(match.group(1))
            if size < 12:
                small_fonts_found.append(f"{size}px (inline)")

    # Check <style> blocks
    for style_tag in soup.find_all("style"):
        css_text = style_tag.string or ""
        for match in small_font_re.finditer(css_text):
            size = float(match.group(1))
            if size < 12:
                small_fonts_found.append(f"{size}px (style block)")

    if small_fonts_found:
        sample = small_fonts_found[:5]
        extra = f" (and {len(small_fonts_found) - 5} more)" if len(small_fonts_found) > 5 else ""
        findings.append(
            _seo_finding(
                severity="low",
                title=f"{len(small_fonts_found)} font-size value(s) below 12px",
                description=(
                    "Font sizes below 12px are hard to read on mobile "
                    "devices and may trigger a mobile usability warning "
                    "in Google Search Console."
                ),
                url=url,
                recommendation=(
                    "Use a minimum font-size of 12px (ideally 16px) for "
                    "body text to ensure readability on mobile devices."
                ),
                check_id="seo-mobile-font-size",
                evidence=f"Sizes: {', '.join(sample)}{extra}",
            )
        )
    return findings
