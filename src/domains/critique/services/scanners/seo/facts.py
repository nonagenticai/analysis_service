"""Measured SEO page facts (design 2026-10-17 SEO-G3..G6) — the values behind the checks.

Today a measured value (title text, meta length, schema types present) exists
only inside a failing finding's prose. This module exposes them as data, built
from the **already-parsed** soup with the same helpers the checks use (no
re-fetch), plus the observations the live robots/sitemap/llms.txt probes and
the cross-page crawl recorded.

Rules:

* Never a fabricated value. Anything not measured is present with
  ``"measured": False`` and a ``reason`` (or ``None`` where absence IS the
  fact, e.g. no ``<title>`` tag).
* Every fact is computed independently and exception-safe: a bug in one fact
  yields ``{"measured": False, "reason": "fact extraction failed: …"}`` for that
  fact only, and can never fail the audit.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import NotRequired, TypedDict
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.seo.content_utils import (
    main_text,
    readability_metrics,
    words,
)
from src.domains.critique.services.scanners.seo.internal_linking import count_generic_anchors
from src.domains.critique.services.scanners.seo.keyword import (
    detect_primary_keyword,
    top_keywords,
)
from src.domains.critique.services.scanners.seo.observations import (
    CrawlObservation,
    FileProbe,
    LiveObservation,
    ProbeObservation,
    probe_part,
)
from src.domains.critique.services.scanners.seo.schema_coverage import analyze_schema_coverage
from src.domains.critique.services.scanners.seo.signals import (
    extract_content_date,
    internal_and_external_links,
)

logger = logging.getLogger(__name__)

NO_URL_REASON = "no URL — inline HTML cannot fetch robots.txt/sitemap"
PERFORMANCE_NOT_MEASURED = (
    "performance: not measured — no Lighthouse/Core Web Vitals measurement on the audit path"
)
_CRAWL_NOT_REQUESTED = "site-wide linking not measured (cross-page crawl off)"
_MAX_OUTLINE = 60
_MAX_TEXT = 200
_MIN_WORDS_FOR_READABILITY = 150
_TOP_KEYWORDS = 10
_ROBOTS_RE = re.compile(r"^robots$", re.IGNORECASE)


# --- typed shapes (wire: ``facts{}``) -------------------------------------------


class NotMeasured(TypedDict):
    """A fact that was not measured — never a fabricated value."""

    measured: bool
    reason: str


class TextFact(TypedDict):
    text: str
    length: int


class HeadingFact(TypedDict):
    level: int
    text: str


class ImagesFact(TypedDict):
    total: int
    missing_alt: int


class HreflangFact(TypedDict):
    lang: str
    href: str


class IndexableFact(TypedDict):
    value: bool
    reasons: list[str]
    checked: list[str]


class SchemaFact(TypedDict):
    types_present: list[str]
    industry: str | None
    recommended: list[str]
    missing: list[str]
    incomplete: dict[str, list[str]]


class ReadabilityFact(TypedDict):
    measured: bool
    reason: NotRequired[str]
    words: int
    grade: float | None
    flesch: float | None


class LinksFact(TypedDict):
    measured: bool
    reason: NotRequired[str]
    host: NotRequired[str]
    internal: int | None
    external: int | None
    generic_anchors: int


class RobotsFact(TypedDict):
    measured: bool
    reason: NotRequired[str]
    fetched: NotRequired[bool]
    status: NotRequired[int]
    url: NotRequired[str | None]
    ai_bots_blocked: NotRequired[list[str] | None]
    search_engines_blocked: NotRequired[bool]
    ai_bots_reason: NotRequired[str]


class FileFact(TypedDict):
    measured: bool
    reason: NotRequired[str]
    found: NotRequired[bool | None]
    status: NotRequired[int]
    url: NotRequired[str | None]


class CrawlLink(TypedDict):
    url: str | None
    evidence: str | None


class CrawlFact(TypedDict):
    measured: bool
    reason: NotRequired[str]
    broken: NotRequired[list[CrawlLink]]
    orphans: NotRequired[list[str | None]]
    hubs: NotRequired[list[CrawlLink]]


class SeoFacts(TypedDict, total=False):
    """The whole ``facts{}`` block. Every section may instead be :class:`NotMeasured`."""

    measured: bool
    reason: str
    page: NotMeasured
    title: TextFact | NotMeasured | None
    meta_description: TextFact | NotMeasured | None
    h1: list[str] | NotMeasured
    heading_outline: list[HeadingFact] | NotMeasured
    images: ImagesFact | NotMeasured
    canonical: str | NotMeasured | None
    meta_robots: str | NotMeasured | None
    hreflang: list[HreflangFact] | NotMeasured
    indexable: IndexableFact | NotMeasured
    schema: SchemaFact | NotMeasured
    primary_keyword: str | NotMeasured | None
    top_keywords: list[str] | NotMeasured
    readability: ReadabilityFact | NotMeasured
    links: LinksFact | NotMeasured
    content_date: str | NotMeasured | None
    robots: RobotsFact | NotMeasured
    sitemap: FileFact | NotMeasured
    llms_txt: FileFact | NotMeasured
    crawl: CrawlFact | NotMeasured
    performance: NotMeasured


def _not_measured(reason: str) -> NotMeasured:
    return {"measured": False, "reason": reason}


def _text(tag: Tag) -> str:
    return tag.get_text(" ", strip=True)[:_MAX_TEXT]


# --- on-page (SEO-G3) ---------------------------------------------------------


def title_fact(soup: BeautifulSoup) -> TextFact | None:
    """``{text, length}`` of the ``<title>``; ``None`` when there is no title tag."""
    tag = soup.find("title")
    if not isinstance(tag, Tag):
        return None
    text = tag.get_text(" ", strip=True)
    return {"text": text, "length": len(text)}


def meta_description_fact(soup: BeautifulSoup) -> TextFact | None:
    """``{text, length}`` of the meta description; ``None`` when absent."""
    tag = soup.find("meta", attrs={"name": "description"})
    if not isinstance(tag, Tag):
        return None
    text = str(tag.get("content", "") or "").strip()
    return {"text": text, "length": len(text)}


def h1_fact(soup: BeautifulSoup) -> list[str]:
    return [_text(h) for h in soup.find_all("h1") if isinstance(h, Tag)]


def heading_outline_fact(soup: BeautifulSoup) -> list[HeadingFact]:
    """Document-order ``[{level, text}]`` for H1-H6, capped."""
    outline: list[HeadingFact] = []
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if not isinstance(h, Tag):
            continue
        outline.append({"level": int(h.name[1]), "text": _text(h)})
        if len(outline) >= _MAX_OUTLINE:
            break
    return outline


def images_fact(soup: BeautifulSoup) -> ImagesFact:
    """Same rule as ``seo-images-missing-alt``: an empty/absent ``alt`` counts as missing."""
    images = [img for img in soup.find_all("img") if isinstance(img, Tag)]
    return {"total": len(images), "missing_alt": sum(1 for img in images if not img.get("alt"))}


def canonical_fact(soup: BeautifulSoup) -> str | None:
    tag = soup.find("link", attrs={"rel": "canonical"})
    if not isinstance(tag, Tag):
        return None
    return str(tag.get("href", "") or "").strip() or None


def meta_robots_fact(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"name": _ROBOTS_RE})
    if not isinstance(tag, Tag):
        return None
    return str(tag.get("content", "") or "").strip()


def hreflang_fact(soup: BeautifulSoup) -> list[HreflangFact]:
    out: list[HreflangFact] = []
    for tag in soup.find_all("link", attrs={"rel": "alternate", "hreflang": True}):
        if not isinstance(tag, Tag):
            continue
        lang = tag.get("hreflang", "")
        if isinstance(lang, list):
            lang = lang[0] if lang else ""
        out.append({"lang": str(lang), "href": str(tag.get("href", "") or "")})
    return out


# --- indexability -------------------------------------------------------------


def _norm(u: str) -> str:
    parsed = urlparse(u)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/') or '/'}"


def indexable_fact(
    soup: BeautifulSoup, url: str, robots: RobotsFact | NotMeasured | None
) -> IndexableFact:
    """``{value, reasons[], checked[]}`` — ``checked`` names the signals actually evaluated.

    A signal that could not be evaluated (no URL → canonical target and
    robots.txt unknown) is simply absent from ``checked``; it is never assumed
    to be clean.
    """
    reasons: list[str] = []
    checked = ["meta_robots"]
    robots_meta = (meta_robots_fact(soup) or "").lower()
    if "noindex" in robots_meta:
        reasons.append("meta robots noindex")
    canonical = canonical_fact(soup)
    if url:
        checked.append("canonical")
        if canonical and urlparse(canonical).netloc and _norm(canonical) != _norm(url):
            reasons.append(f"canonical points elsewhere ({canonical})")
    if robots and robots.get("measured") and robots.get("fetched"):
        checked.append("robots_txt")
        if robots.get("search_engines_blocked"):
            reasons.append("robots.txt disallows search crawlers from the site root")
    return {"value": not reasons, "reasons": reasons, "checked": checked}


# --- schema / content / keyword / links (SEO-G4, SEO-G5) ----------------------


def schema_fact(soup: BeautifulSoup) -> SchemaFact:
    coverage = analyze_schema_coverage(soup)
    return {
        "types_present": sorted(coverage.present),
        "industry": coverage.industry,
        "recommended": sorted(coverage.recommended),
        "missing": list(coverage.missing),
        "incomplete": {k: list(v) for k, v in coverage.incomplete.items()},
    }


def readability_fact(soup: BeautifulSoup) -> ReadabilityFact:
    """``{grade (Gunning fog), flesch, words}``; not measured under 150 words."""
    text = main_text(soup)
    word_count = len(words(text))
    if word_count < _MIN_WORDS_FOR_READABILITY:
        return {
            "measured": False,
            "reason": (
                f"main content is {word_count} words, under the "
                f"{_MIN_WORDS_FOR_READABILITY}-word minimum for a readability score"
            ),
            "words": word_count,
            "grade": None,
            "flesch": None,
        }
    metrics = readability_metrics(text)
    return {
        "measured": True,
        "words": word_count,
        "grade": metrics["gunning_fog"],
        "flesch": metrics["flesch_reading_ease"],
    }


def links_fact(soup: BeautifulSoup, url: str) -> LinksFact:
    """Internal/external anchor counts against the page host.

    Inline HTML has no URL, so the host comes from the canonical link when there
    is one; with neither, every absolute link would count as internal, so the
    split is reported unmeasured instead of guessed.
    """
    page_url = url or canonical_fact(soup) or ""
    host = urlparse(page_url).netloc.lower() or None
    generic = count_generic_anchors(soup)
    if host is None:
        return {
            "measured": False,
            "reason": "no URL or canonical — the page host is unknown",
            "internal": None,
            "external": None,
            "generic_anchors": generic,
        }
    internal, external = internal_and_external_links(soup, page_url)
    return {
        "measured": True,
        "host": host,
        "internal": internal,
        "external": external,
        "generic_anchors": generic,
    }


def content_date_fact(soup: BeautifulSoup) -> str | None:
    found = extract_content_date(soup)
    return found.date().isoformat() if found is not None else None


# --- live probes (SEO-G2 / SEO-G5) -------------------------------------------


def _probe_reason(probe: ProbeObservation | None, part: FileProbe | None) -> str:
    if probe is None:
        return "live check did not run"
    for candidate in (
        (part or {}).get("error"),
        probe.get("error"),
        probe.get("reason"),
    ):
        if candidate:
            return f"live check did not complete: {candidate}"
    return "live check did not complete"


def robots_fact(live: LiveObservation | None) -> RobotsFact:
    """robots.txt: fetched/status from the technical probe, bot blocks from the GEO probe."""
    if live is None:
        return {"measured": False, "reason": NO_URL_REASON}
    tech = live.get("technical")
    geo = live.get("geo")
    tech_part: FileProbe = (tech or {}).get("robots") or {}
    geo_part: FileProbe = (geo or {}).get("robots") or {}
    status = tech_part.get("status")
    if status is None:
        status = geo_part.get("status")
    if status is None:
        return {"measured": False, "reason": _probe_reason(tech, tech_part), "fetched": False}
    fact: RobotsFact = {
        "measured": True,
        "fetched": status == 200,
        "status": status,
        "url": tech_part.get("url") or geo_part.get("url"),
    }
    if geo_part.get("status") is not None:
        fact["ai_bots_blocked"] = list(geo_part.get("ai_bots_blocked") or [])
        fact["search_engines_blocked"] = bool(geo_part.get("search_engines_blocked"))
    else:
        fact["ai_bots_blocked"] = None
        fact["ai_bots_reason"] = _probe_reason(geo, geo_part)
    return fact


def _file_fact(probe: ProbeObservation | None, part_name: str) -> FileFact:
    part = probe_part(probe, part_name)
    status = part.get("status")
    if status is None:
        return {"measured": False, "reason": _probe_reason(probe, part), "url": part.get("url")}
    found: bool | None = True if 200 <= status < 300 else False if status == 404 else None
    return {"measured": True, "found": found, "status": status, "url": part.get("url")}


def sitemap_fact(live: LiveObservation | None) -> FileFact:
    """``found`` is True on 2xx, False on 404, ``None`` (unknown) for any other status."""
    if live is None:
        return {"measured": False, "reason": NO_URL_REASON}
    return _file_fact(live.get("technical"), "sitemap")


def llms_txt_fact(live: LiveObservation | None) -> FileFact:
    if live is None:
        return {"measured": False, "reason": "no URL — inline HTML cannot fetch llms.txt"}
    return _file_fact(live.get("geo"), "llms_txt")


# --- crawl (SEO-G6) -------------------------------------------------------------


def _str_or_none(value: object) -> str | None:
    return None if value is None else str(value)


def crawl_fact(crawl: CrawlObservation) -> CrawlFact:
    """Lists behind the ``seo-linking-*`` crawl findings (each capped at 25 by the crawler)."""
    if not crawl["requested"]:
        return {"measured": False, "reason": _CRAWL_NOT_REQUESTED}
    if not crawl["ran"]:
        return {
            "measured": False,
            "reason": crawl["note"] or "the cross-page crawl did not complete",
        }
    broken: list[CrawlLink] = []
    orphans: list[str | None] = []
    hubs: list[CrawlLink] = []
    for f in crawl["findings"]:
        cid = f.get("check_id")
        link: CrawlLink = {
            "url": _str_or_none(f.get("url")),
            "evidence": _str_or_none(f.get("evidence")),
        }
        if cid == "seo-linking-broken":
            broken.append(link)
        elif cid == "seo-linking-orphan":
            orphans.append(link["url"])
        elif cid == "seo-linking-hub":
            hubs.append(link)
    return {"measured": True, "broken": broken, "orphans": orphans, "hubs": hubs}


# --- assembly ---------------------------------------------------------------------


def _safe[T](name: str, fn: Callable[[], T]) -> T | NotMeasured:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a facts bug must never fail the audit
        logger.warning("SEO fact %r failed, reporting it unmeasured: %s", name, exc)
        return _not_measured(f"fact extraction failed: {type(exc).__name__}")


def build_seo_facts(
    soup: BeautifulSoup | None,
    url: str,
    *,
    live: LiveObservation | None,
    crawl: CrawlObservation,
) -> SeoFacts:
    """Assemble ``facts{}``. ``live`` is ``None`` when no URL was given (inline HTML)."""
    performance = _not_measured(PERFORMANCE_NOT_MEASURED)
    if soup is None:
        return {"performance": performance, "page": _not_measured("page could not be parsed")}
    robots = _safe("robots", lambda: robots_fact(live))
    primary = _safe("primary_keyword", lambda: detect_primary_keyword(soup))
    return {
        "title": _safe("title", lambda: title_fact(soup)),
        "meta_description": _safe("meta_description", lambda: meta_description_fact(soup)),
        "h1": _safe("h1", lambda: h1_fact(soup)),
        "heading_outline": _safe("heading_outline", lambda: heading_outline_fact(soup)),
        "images": _safe("images", lambda: images_fact(soup)),
        "canonical": _safe("canonical", lambda: canonical_fact(soup)),
        "meta_robots": _safe("meta_robots", lambda: meta_robots_fact(soup)),
        "hreflang": _safe("hreflang", lambda: hreflang_fact(soup)),
        "indexable": _safe("indexable", lambda: indexable_fact(soup, url, robots)),
        "schema": _safe("schema", lambda: schema_fact(soup)),
        "primary_keyword": primary,
        "top_keywords": _safe("top_keywords", lambda: top_keywords(soup, n=_TOP_KEYWORDS)),
        "readability": _safe("readability", lambda: readability_fact(soup)),
        "links": _safe("links", lambda: links_fact(soup, url)),
        "content_date": _safe("content_date", lambda: content_date_fact(soup)),
        "robots": robots,
        "sitemap": _safe("sitemap", lambda: sitemap_fact(live)),
        "llms_txt": _safe("llms_txt", lambda: llms_txt_fact(live)),
        "crawl": _safe("crawl", lambda: crawl_fact(crawl)),
        "performance": performance,
    }
