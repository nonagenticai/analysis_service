"""Internal-linking quality checks (group: ``internal_linking``).

Single-page, Tier 0: anchor-text quality and internal-link presence. (Site-wide
orphan/hub analysis needs a crawl and lands behind an opt-in flag in a later
phase.) Namespace ``seo-linking-<check>`` → group ``internal_linking``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import main_text, words
from src.domains.critique.services.scanners.seo.signals import internal_and_external_links

_MIN_WORDS = 200
_GENERIC_ANCHOR_RE = re.compile(
    r"^(click here|read more|learn more|more|here|this|link|continue|click|details|"
    r"see more|find out more)$",
    re.IGNORECASE,
)
_GENERIC_ANCHOR_THRESHOLD = 3


def count_generic_anchors(soup: BeautifulSoup) -> int:
    """Number of links whose anchor text is generic ('click here', 'read more', …)."""
    generic = 0
    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        text = a.get_text(" ", strip=True)
        if text and _GENERIC_ANCHOR_RE.match(text):
            generic += 1
    return generic


def check_internal_linking(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    word_count = len(words(main_text(soup)))

    generic = count_generic_anchors(soup)

    if generic >= _GENERIC_ANCHOR_THRESHOLD:
        findings.append(
            _seo_finding(
                severity="low",
                title=f"{generic} generic anchor texts",
                description=(
                    f"{generic} links use generic anchor text ('click here', 'read more', …). "
                    "Descriptive anchors pass clearer relevance signals and help accessibility."
                ),
                url=url,
                recommendation="Replace generic anchors with descriptive, keyword-relevant text.",
                check_id="seo-linking-generic-anchors",
                false_positive_risk="medium",
            )
        )

    if word_count >= _MIN_WORDS:
        internal, _external = internal_and_external_links(soup, url)
        if internal == 0:
            findings.append(
                _seo_finding(
                    severity="low",
                    title="No internal links",
                    description=(
                        "This content page links to no other pages on the site. Internal links "
                        "distribute link equity and help search engines discover related content."
                    ),
                    url=url,
                    recommendation="Add contextual internal links to related pages.",
                    check_id="seo-linking-no-internal",
                    false_positive_risk="medium",
                )
            )
    else:
        findings.append(
            _seo_finding(
                severity="low",
                title="Not enough content to assess internal linking",
                description=(
                    "The page has too little body text to evaluate its internal-link structure. "
                    "Content pages should link contextually to related pages."
                ),
                url=url,
                recommendation="Add substantive content with contextual internal links to related pages.",
                check_id="seo-linking-insufficient-content",
                false_positive_risk="low",
            )
        )

    return findings
