"""Competitive-positioning marketing checks (``mkt-competitive-*``).

The reference marketing rubric weights competitive positioning at 15%: how
clearly the page differentiates, whether it engages comparison intent, and
whether it borrows third-party authority. All three are detectable from the
static HTML without an LLM.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import page_text
from src.domains.critique.services.scanners.marketing.base import (
    COMPARISON_RE,
    DIFFERENTIATION_RE,
    MEDIA_MENTION_RE,
    REVIEW_PLATFORM_RE,
    _marketing_finding,
)


def fold_copy(soup: BeautifulSoup) -> str:
    """Headline + subhead copy where a differentiation claim should live."""
    parts: list[str] = []
    for tag in soup.find_all(["h1", "h2"]):
        if isinstance(tag, Tag):
            parts.append(tag.get_text(" ", strip=True))
    return " ".join(parts)


def has_comparison(soup: BeautifulSoup, text: str) -> bool:
    """A comparison / "vs" / "alternative to" surface in the copy or a link label."""
    return bool(COMPARISON_RE.search(text)) or any(
        COMPARISON_RE.search(a.get_text(" ", strip=True))
        for a in soup.find_all("a")
        if isinstance(a, Tag)
    )


def has_reputation(text: str) -> bool:
    """A third-party review platform or a media mention in the visible copy."""
    return bool(REVIEW_PLATFORM_RE.search(text) or MEDIA_MENTION_RE.search(text))


def check_competitive(soup: BeautifulSoup, url: str) -> list[dict]:
    """Detect missing differentiation, comparison, and reputation signals."""
    findings: list[dict] = []
    text = page_text(soup)

    # 1. Differentiation clarity in the fold copy (highest-weight signal).
    if not DIFFERENTIATION_RE.search(fold_copy(soup)):
        findings.append(
            _marketing_finding(
                severity="medium",
                title="No differentiation claim in the headline area",
                description=(
                    "The H1/H2 copy makes no differentiation or category claim (e.g. "
                    "'the only…', 'unlike…', 'purpose-built for…'). Visitors cannot tell "
                    "how this offer differs from alternatives in the first few seconds."
                ),
                url=url,
                recommendation=(
                    "State a concrete point of difference or category position in the "
                    "headline or subhead."
                ),
                check_id="mkt-competitive-weak-differentiation",
                false_positive_risk="high",
            )
        )

    # 2. Comparison / "vs" surface for bottom-funnel comparison intent.
    if not has_comparison(soup, text):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No comparison or alternatives content",
                description=(
                    "There is no comparison, 'vs', or 'alternative to' content. Buyers "
                    "evaluating options search for these; owning that surface captures "
                    "comparison-stage intent."
                ),
                url=url,
                recommendation=(
                    "Add a comparison table or a 'vs / alternative to' section addressing "
                    "the main competitors."
                ),
                check_id="mkt-competitive-no-comparison",
            )
        )

    # 3. Third-party reputation / authority (review platforms or media mentions).
    if not has_reputation(text):
        findings.append(
            _marketing_finding(
                severity="low",
                title="No third-party reputation signals",
                description=(
                    "No third-party review platform (G2, Capterra, Trustpilot…) or media "
                    "mention is referenced. Borrowed authority reassures skeptical buyers "
                    "comparing vendors."
                ),
                url=url,
                recommendation=("Surface review-platform ratings or 'as featured in' media logos."),
                check_id="mkt-competitive-no-reputation",
                false_positive_risk="high",
            )
        )

    return findings
