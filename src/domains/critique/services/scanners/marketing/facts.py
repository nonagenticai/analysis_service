"""Measured page facts behind the marketing checks (design 2026-10-17 MKT-G1).

The ``mkt-*`` checks only say *that* a signal is missing. The scorecard's
positioning / brand / channel sections need *what is there*: the fold copy, the
OG image URL, which social platforms and analytics vendors the page links. This
module reads them off the already-parsed soup using the SAME detectors the
checks use (``fold_copy``, ``logo_img``, ``has_pricing``, ``ANALYTICS_VENDOR_RES``
…), so a fact and its check can never disagree.

Rules:

* Unknown is ``None``, never a fabricated default. A section whose extraction
  raises is reported as ``None`` (logged), and the scan still returns.
* Two facts are permanently not measured by a page scan and say so:
  ``competitive_benchmark`` (MKT-G3 — no competitor pages are fetched) and
  ``channel_performance`` (MKT-G6 — needs analytics / ad-account data).
* ``dimension_sources`` is the weights table + reference block of the exec
  one-pager, derived from ``DIMENSION_WEIGHTS`` / ``DIMENSION_SOURCES``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Literal, TypedDict

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.cro.base import page_text
from src.domains.critique.services.scanners.marketing.base import (
    ANALYTICS_VENDOR_RES,
    CONSENT_RE,
    DIFFERENTIATION_RE,
    GROWTH_LOOP_RE,
    MEDIA_MENTION_RE,
    RETENTION_RE,
    REVIEW_PLATFORM_RE,
    SOCIAL_PLATFORM_RES,
)
from src.domains.critique.services.scanners.marketing.brand import (
    about_link,
    favicon_link,
    has_contact,
    logo_img,
)
from src.domains.critique.services.scanners.marketing.competitive import (
    fold_copy,
    has_comparison,
)
from src.domains.critique.services.scanners.marketing.growth import (
    has_email_capture,
    has_pricing,
    social_links,
)
from src.domains.critique.services.scanners.marketing.scoring import (
    DIMENSION_SOURCES,
    DIMENSION_WEIGHTS,
    REPORT_POINTERS,
)
from src.domains.critique.services.scanners.marketing.tracking import script_haystack

__all__ = [
    "CHANNEL_PERFORMANCE_REASON",
    "COMPETITIVE_BENCHMARK_REASON",
    "MarketingFactsDict",
    "build_marketing_facts",
    "check_measurements",
    "dimension_sources",
]

logger = logging.getLogger(__name__)

COMPETITIVE_BENCHMARK_REASON = "No competitor pages analysed — add competitor URLs"
CHANNEL_PERFORMANCE_REASON = (
    "Channel performance needs analytics/ad-account data — not available to a page scan"
)

_EXCERPT_CHARS = 280
_CLAIM_CONTEXT = 60


# Payload shapes — mirror ``domains/marketing/schemas.py::MarketingFacts`` 1:1.
class PresenceDict(TypedDict):
    present: bool
    url: str | None


class PositioningDict(TypedDict):
    fold_copy_excerpt: str | None
    differentiation_claim: str | None
    comparison_page: bool
    reputation_signals: list[str]


class ContactDict(TypedDict):
    present: bool
    email: str | None
    phone: str | None


class SocialCardDict(TypedDict):
    og_title: bool
    og_description: bool
    og_image: bool
    twitter_card: bool


class BrandDict(TypedDict):
    logo: PresenceDict
    og_image_url: str | None
    favicon: PresenceDict
    about_link: PresenceDict
    contact: ContactDict
    social_card_fields: SocialCardDict


class SocialProfileDict(TypedDict):
    platform: str
    url: str


class ChannelsDict(TypedDict):
    social_profiles: list[SocialProfileDict]
    email_capture: bool
    analytics_vendors: list[str]
    consent: bool
    pricing_visible: bool
    referral: bool
    retention_hook: bool


class NotMeasuredDict(TypedDict):
    measured: Literal[False]
    reason: str


class DimensionSourceDict(TypedDict):
    dimension: str
    weight: float
    score: float | None
    weighted_points: float | None
    source_engine: str
    reference_only: bool
    report_pointer: str | None


class MarketingFactsDict(TypedDict):
    positioning: PositioningDict | None
    brand: BrandDict | None
    channels: ChannelsDict | None
    competitive_benchmark: NotMeasuredDict
    channel_performance: NotMeasuredDict
    dimension_sources: list[DimensionSourceDict]


def _excerpt(text: str, limit: int = _EXCERPT_CHARS) -> str | None:
    """Whitespace-collapsed text capped at ``limit`` chars; ``None`` when empty."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1].rstrip() + "…"


def _attr(tag: Tag | None, name: str) -> str | None:
    """A non-empty string attribute of ``tag``, else ``None``."""
    if tag is None:
        return None
    value = tag.get(name)
    if isinstance(value, list):
        value = " ".join(value)
    value = str(value or "").strip()
    return value or None


def _presence(tag: Tag | None, attr: str) -> PresenceDict:
    return {"present": tag is not None, "url": _attr(tag, attr)}


def _meta(soup: BeautifulSoup, attr: str, value: str) -> Tag | None:
    tag = soup.find("meta", attrs={attr: value})
    return tag if isinstance(tag, Tag) else None


def _unique_matches(patterns: tuple[re.Pattern[str], ...], text: str) -> list[str]:
    """Every match of ``patterns`` in ``text``, de-duplicated case-insensitively."""
    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            token = " ".join(match.group(0).split())
            if token and token.lower() not in seen:
                seen.add(token.lower())
                out.append(token)
    return out


def _first_href(soup: BeautifulSoup, scheme: str) -> str | None:
    """The address of the first ``<a href="<scheme>…">`` (e.g. ``mailto:``)."""
    for a in soup.find_all("a"):
        href = _attr(a, "href") if isinstance(a, Tag) else None
        if href and href.lower().startswith(scheme):
            return href[len(scheme) :].split("?", 1)[0].strip() or None
    return None


def _differentiation_claim(soup: BeautifulSoup, fold: str) -> str | None:
    """The H1/H2 carrying the claim the check saw, else the fold copy around it."""
    match = DIFFERENTIATION_RE.search(fold)
    if match is None:
        return None
    for tag in soup.find_all(["h1", "h2"]):
        heading = tag.get_text(" ", strip=True) if isinstance(tag, Tag) else ""
        if DIFFERENTIATION_RE.search(heading):
            return _excerpt(heading)
    # The match spans two headings (the check joins them with a space).
    start = max(0, match.start() - _CLAIM_CONTEXT)
    return _excerpt(fold[start : match.end() + _CLAIM_CONTEXT])


def _positioning(soup: BeautifulSoup) -> PositioningDict:
    text = page_text(soup)
    fold = " ".join(fold_copy(soup).split())
    claim = _differentiation_claim(soup, fold)
    return {
        "fold_copy_excerpt": _excerpt(fold),
        "differentiation_claim": claim,
        "comparison_page": has_comparison(soup, text),
        "reputation_signals": _unique_matches((REVIEW_PLATFORM_RE, MEDIA_MENTION_RE), text),
    }


def _brand(soup: BeautifulSoup) -> BrandDict:
    og_image = _meta(soup, "property", "og:image")
    return {
        "logo": _presence(logo_img(soup), "src"),
        "og_image_url": _attr(og_image, "content"),
        "favicon": _presence(favicon_link(soup), "href"),
        "about_link": _presence(about_link(soup), "href"),
        "contact": {
            "present": has_contact(soup),
            "email": _first_href(soup, "mailto:"),
            "phone": _first_href(soup, "tel:"),
        },
        "social_card_fields": {
            "og_title": _meta(soup, "property", "og:title") is not None,
            "og_description": _meta(soup, "property", "og:description") is not None,
            "og_image": og_image is not None,
            "twitter_card": _meta(soup, "name", "twitter:card") is not None,
        },
    }


def _social_profiles(soup: BeautifulSoup) -> list[SocialProfileDict]:
    profiles: list[SocialProfileDict] = []
    seen: set[str] = set()
    for href in social_links(soup):
        platform = next((name for name, rx in SOCIAL_PLATFORM_RES if rx.search(href)), None)
        if platform is None or href in seen:
            continue
        seen.add(href)
        profiles.append({"platform": platform, "url": href})
    return profiles


def _channels(soup: BeautifulSoup) -> ChannelsDict:
    text = page_text(soup)
    scripts = script_haystack(soup)
    return {
        "social_profiles": _social_profiles(soup),
        "email_capture": has_email_capture(soup),
        "analytics_vendors": [name for name, rx in ANALYTICS_VENDOR_RES if rx.search(scripts)],
        "consent": bool(CONSENT_RE.search(f"{scripts}\n{text}")),
        "pricing_visible": has_pricing(soup, text),
        "referral": bool(GROWTH_LOOP_RE.search(text)),
        "retention_hook": bool(RETENTION_RE.search(text)),
    }


def _safe[T](name: str, fn: Callable[[BeautifulSoup], T], soup: BeautifulSoup | None) -> T | None:
    """Run one section extractor; any failure reports the section as unknown (None)."""
    if soup is None:
        return None
    try:
        return fn(soup)
    except Exception as exc:  # noqa: BLE001 — a fact never fails the scan
        logger.warning("marketing facts: %s extraction failed: %s", name, exc)
        return None


def dimension_sources(dimension_scores: Mapping[str, float]) -> list[DimensionSourceDict]:
    """One row per weighted dimension: weight, score, points and the source engine.

    ``reference_only`` marks the dimensions borrowed from the CRO / SEO engines;
    the scorecard shows them as a single row pointing at ``report_pointer``.
    An unscored dimension carries ``score``/``weighted_points`` ``None``.
    """
    rows: list[DimensionSourceDict] = []
    for dimension, weight in DIMENSION_WEIGHTS.items():
        engine = DIMENSION_SOURCES[dimension]
        score = dimension_scores.get(dimension)
        rows.append(
            {
                "dimension": dimension,
                "weight": weight,
                "score": score,
                "weighted_points": None if score is None else round(score * weight, 2),
                "source_engine": engine,
                "reference_only": engine != "marketing",
                "report_pointer": REPORT_POINTERS[engine],
            }
        )
    return rows


def build_marketing_facts(
    soup: BeautifulSoup | None, *, dimension_scores: Mapping[str, float]
) -> MarketingFactsDict:
    """The ``facts`` block of the marketing scorecard response."""
    return {
        "positioning": _safe("positioning", _positioning, soup),
        "brand": _safe("brand", _brand, soup),
        "channels": _safe("channels", _channels, soup),
        "competitive_benchmark": {"measured": False, "reason": COMPETITIVE_BENCHMARK_REASON},
        "channel_performance": {"measured": False, "reason": CHANNEL_PERFORMANCE_REASON},
        "dimension_sources": dimension_sources(dimension_scores),
    }


def check_measurements(facts: MarketingFactsDict) -> dict[str, str]:
    """The measured value shown next to a check, where a fact names it.

    Only facts that were actually extracted contribute; a section that is
    ``None`` contributes nothing (the check row's ``measured`` stays ``None``).
    """
    out: dict[str, str] = {}
    positioning = facts["positioning"]
    if positioning is not None:
        if claim := positioning["differentiation_claim"]:
            out["mkt-competitive-weak-differentiation"] = claim
        if signals := positioning["reputation_signals"]:
            out["mkt-competitive-no-reputation"] = ", ".join(signals)
    brand = facts["brand"]
    if brand is not None:
        if og := brand["og_image_url"]:
            out["mkt-brand-no-og-image"] = og
        if favicon := brand["favicon"]["url"]:
            out["mkt-brand-no-favicon"] = favicon
    channels = facts["channels"]
    if channels is not None:
        vendors = channels["analytics_vendors"]
        out["mkt-growth-no-analytics"] = ", ".join(vendors) if vendors else "none detected"
        profiles = channels["social_profiles"]
        out["mkt-growth-no-social-presence"] = (
            ", ".join(p["platform"] for p in profiles) if profiles else "none linked"
        )
    return out
