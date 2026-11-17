"""On-page SEO checks (title, meta description, alt, headings, keyword, duplicates)."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import (
    page_language,
    stopwords_for_language,
)


def check_title(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    title = soup.find("title")
    if not title:
        findings.append(
            _seo_finding(
                severity="high",
                title="Missing title tag",
                description="The page is missing a <title> tag, which is critical for SEO.",
                url=url,
                recommendation="Add a descriptive <title> tag between 50-60 characters.",
                selector="head > title",
                check_id="seo-missing-title",
            )
        )
    elif title.string:
        title_text = title.string.strip()
        if len(title_text) < 10:
            findings.append(
                _seo_finding(
                    severity="medium",
                    title="Title tag too short",
                    description=f"Title is only {len(title_text)} characters: '{title_text}'.",
                    url=url,
                    recommendation="Expand the title to 50-60 characters with relevant keywords.",
                    selector="head > title",
                    check_id="seo-title-too-short",
                )
            )
        elif len(title_text) > 70:
            findings.append(
                _seo_finding(
                    severity="low",
                    title="Title tag too long",
                    description=(
                        f"Title is {len(title_text)} characters and may be "
                        "truncated in search results."
                    ),
                    url=url,
                    recommendation="Shorten the title to 50-60 characters.",
                    selector="head > title",
                    check_id="seo-title-too-long",
                )
            )
    return findings


def check_meta_description(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if not meta_desc:
        findings.append(
            _seo_finding(
                severity="high",
                title="Missing meta description",
                description="No meta description found. Search engines use this for snippets.",
                url=url,
                recommendation="Add a meta description of 150-160 characters.",
                selector='meta[name="description"]',
                check_id="seo-meta-description-missing",
            )
        )
    else:
        content = meta_desc.get("content", "")
        if isinstance(content, list):
            content = content[0] if content else ""
        content = str(content).strip()
        if len(content) < 50:
            findings.append(
                _seo_finding(
                    severity="medium",
                    title="Meta description too short",
                    description=f"Meta description is only {len(content)} characters.",
                    url=url,
                    recommendation="Expand to 150-160 characters with a compelling description.",
                    selector='meta[name="description"]',
                    check_id="seo-meta-description-too-short",
                )
            )
        elif len(content) > 170:
            findings.append(
                _seo_finding(
                    severity="low",
                    title="Meta description too long",
                    description=(
                        f"Meta description is {len(content)} characters and may be truncated."
                    ),
                    url=url,
                    recommendation="Shorten to 150-160 characters.",
                    selector='meta[name="description"]',
                    check_id="seo-meta-description-too-long",
                )
            )
    return findings


def check_heading_hierarchy(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    h1s = soup.find_all("h1")
    if not h1s:
        findings.append(
            _seo_finding(
                severity="high",
                title="Missing H1 heading",
                description="No H1 heading found. Every page should have exactly one H1.",
                url=url,
                recommendation="Add a single H1 heading that describes the page content.",
                selector="h1",
                check_id="seo-missing-h1",
            )
        )
    elif len(h1s) > 1:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Multiple H1 headings",
                description=(
                    f"Found {len(h1s)} H1 headings. Best practice is exactly one per page."
                ),
                url=url,
                recommendation="Keep only one H1 and convert others to H2 or lower.",
                selector="h1",
                check_id="seo-multiple-h1",
            )
        )

    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    if headings:
        levels = [int(h.name[1]) for h in headings]
        for i in range(1, len(levels)):
            if levels[i] > levels[i - 1] + 1:
                findings.append(
                    _seo_finding(
                        severity="low",
                        title="Heading hierarchy skip",
                        description=(
                            f"Heading jumps from H{levels[i - 1]} to H{levels[i]}, "
                            "skipping a level."
                        ),
                        url=url,
                        recommendation=(
                            "Maintain sequential heading hierarchy (H1 > H2 > H3, etc.)."
                        ),
                        selector=headings[i].name,
                        check_id="seo-heading-hierarchy-skip",
                    )
                )
                break
    return findings


def check_image_alt(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    images = soup.find_all("img")
    missing_alt = [img for img in images if not img.get("alt")]
    if missing_alt:
        count = len(missing_alt)
        total = len(images)
        severity = "high" if count > total * 0.5 else "medium"
        findings.append(
            _seo_finding(
                severity=severity,
                title=f"{count} images missing alt text",
                description=(
                    f"{count} of {total} images lack alt attributes, hurting accessibility and SEO."
                ),
                url=url,
                recommendation="Add descriptive alt text to all images.",
                selector="img:not([alt])",
                check_id="seo-images-missing-alt",
                remediation_effort="medium",
            )
        )
    return findings


def check_keyword_alignment(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check whether the <title> shares keywords with the <h1>."""
    findings: list[dict] = []
    title_tag = soup.find("title")
    h1_tag = soup.find("h1")
    if not title_tag or not h1_tag:
        return findings

    title_text = (title_tag.string or "").strip().lower()
    h1_text = h1_tag.get_text(strip=True).lower()
    if not title_text or not h1_text:
        return findings

    stopwords = stopwords_for_language(page_language(soup))
    if stopwords is None:
        # No stop-word list for the declared language: function words would
        # count as "shared keywords", so the verdict would be noise either way.
        return findings

    title_words = {w for w in re.split(r"\W+", title_text) if len(w) > 2 and w not in stopwords}
    h1_words = {w for w in re.split(r"\W+", h1_text) if len(w) > 2 and w not in stopwords}

    if title_words and h1_words and not title_words & h1_words:
        findings.append(
            _seo_finding(
                severity="low",
                title="Title and H1 have no keyword overlap",
                description=(
                    "The <title> and <h1> share no significant words. "
                    "Aligning them reinforces the page topic for search "
                    "engines."
                ),
                url=url,
                recommendation=(
                    "Include the primary keyword in both the <title> and "
                    "<h1> to strengthen topical relevance."
                ),
                check_id="seo-keyword-alignment",
                evidence=f"Title: '{title_text}' | H1: '{h1_text}'",
                false_positive_risk="medium",
            )
        )
    return findings


def check_duplicate_meta(soup: BeautifulSoup, url: str) -> list[dict]:
    """Find duplicate <meta name='...'> tags with the same name."""
    findings: list[dict] = []
    meta_tags = soup.find_all("meta", attrs={"name": True})
    name_counts: dict[str, int] = {}
    for meta in meta_tags:
        name = str(meta.get("name", "")).lower().strip()
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1

    duplicates = {n: c for n, c in name_counts.items() if c > 1}
    if duplicates:
        examples = ", ".join(f"'{n}' ({c}x)" for n, c in sorted(duplicates.items()))
        findings.append(
            _seo_finding(
                severity="medium",
                title=f"{len(duplicates)} duplicate meta tag(s)",
                description=(
                    f"Found duplicate <meta name> tags: {examples}. "
                    "Duplicate meta tags can confuse search engines about "
                    "which value to use."
                ),
                url=url,
                recommendation=(
                    "Remove duplicate meta tags so each name attribute appears only once."
                ),
                selector="meta[name]",
                check_id="seo-duplicate-meta",
                evidence=examples,
            )
        )
    return findings
