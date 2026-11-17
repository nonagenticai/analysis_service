"""Social-proof CRO checks.

Evidence: testimonials lift sales-page conversion ~34%; reviews lift ~67%;
named/photo testimonials and quantified counts ("12,000+ customers") outperform
vague praise. Detects the *absence* of each proof type.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import (
    _LOGO_CLASS_RE,
    _RATING_CLASS_RE,
    _TESTIMONIAL_CLASS_RE,
    COUNT_RE,
    MEDIA_MENTION_RE,
    NAMED_ATTRIBUTION_RE,
    _cro_finding,
    page_text,
)


def _has_class(soup: BeautifulSoup, pattern: re.Pattern[str]) -> bool:
    return soup.find(attrs={"class": pattern}) is not None


def _has_review_schema(soup: BeautifulSoup) -> bool:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        content = script.string or ""
        if '"Review"' in content or '"AggregateRating"' in content or '"ratingValue"' in content:
            return True
    return False


def proof_signals(soup: BeautifulSoup, text: str | None = None) -> dict[str, bool]:
    """Presence of each social-proof type — the one predicate set the checks AND
    the report facts read, so the two can never disagree.

    ``named`` is only meaningful when ``testimonials`` is true (the check reports
    unnamed testimonials only then).
    """
    text = page_text(soup) if text is None else text
    review_schema = _has_review_schema(soup)
    testimonials = (
        bool(soup.find("blockquote")) or _has_class(soup, _TESTIMONIAL_CLASS_RE) or review_schema
    )
    # Client logo strip (>=3 images inside a logos/clients container).
    logos = any(
        isinstance(container, Tag) and len(container.find_all("img")) >= 3
        for container in soup.find_all(attrs={"class": _LOGO_CLASS_RE})
    )
    return {
        "testimonials": testimonials,
        "named": bool(NAMED_ATTRIBUTION_RE.search(text)),
        "logos": logos,
        "ratings": _has_class(soup, _RATING_CLASS_RE) or "★" in text or review_schema,
        "metrics": bool(COUNT_RE.search(text) or MEDIA_MENTION_RE.search(text)),
    }


def check_social_proof(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    signals = proof_signals(soup)

    if not signals["testimonials"]:
        findings.append(
            _cro_finding(
                severity="medium",
                title="No testimonials or reviews",
                description=(
                    "No testimonial, review block, or Review schema was found. Customer "
                    "proof is one of the strongest conversion levers."
                ),
                url=url,
                recommendation="Add 2-3 named customer testimonials, ideally with photos.",
                check_id="cro-social-no-testimonials",
            )
        )
    elif not signals["named"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="Testimonials lack named attribution",
                description=(
                    "Testimonials were detected but no clear name/role attribution "
                    "('— Jane Doe, CEO') was found. Anonymous quotes read as fabricated."
                ),
                url=url,
                recommendation="Attribute each testimonial to a real name, role, and company.",
                check_id="cro-social-unnamed-testimonials",
            )
        )

    if not signals["logos"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No client/logo trust strip",
                description=(
                    "No 'trusted by' logo cluster was detected. Recognisable customer or "
                    "press logos borrow authority."
                ),
                url=url,
                recommendation="Add a logo strip of known customers or media mentions.",
                check_id="cro-social-no-logos",
            )
        )

    if not signals["ratings"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No star ratings",
                description="No star-rating UI or ratingValue schema was found.",
                url=url,
                recommendation="Surface an aggregate star rating with a review count.",
                check_id="cro-social-no-ratings",
            )
        )

    if not signals["metrics"]:
        findings.append(
            _cro_finding(
                severity="low",
                title="No quantified proof or media mentions",
                description=(
                    "No customer/usage counts ('12,000+ customers') or media mentions "
                    "were found. Specific numbers make proof concrete."
                ),
                url=url,
                recommendation="Add a headline metric (users, reviews, results) or press mentions.",
                check_id="cro-social-no-metrics",
            )
        )

    return findings
