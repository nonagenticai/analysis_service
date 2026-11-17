"""Shared markup + structured-data signal extractors for the SEO audit.

Where :mod:`content_utils` reasons over prose, this module reasons over markup:
JSON-LD ``@type``s, author signals, publish/modify dates, and outbound links.
Used by the E-E-A-T, schema-coverage and GEO check groups. All Tier 0 —
non-mutating reads of the shared soup, no network, no model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

_AUTHOR_CLASS_RE = re.compile(r"author|byline", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iter_ld_objects(soup: BeautifulSoup) -> Iterator[dict]:
    """Yield every JSON-LD object on the page, flattening ``@graph`` arrays."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not isinstance(script, Tag):
            continue
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError, ValueError, TypeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if isinstance(node, dict):
                        yield node
            else:
                yield item


def extract_schema_types(soup: BeautifulSoup) -> set[str]:
    """The set of JSON-LD ``@type`` values present on the page (as plain strings)."""
    types: set[str] = set()
    for obj in _iter_ld_objects(soup):
        t = obj.get("@type")
        if isinstance(t, str):
            types.add(t)
        elif isinstance(t, list):
            types.update(str(x) for x in t if isinstance(x, str))
    return types


def schema_objects_of_type(soup: BeautifulSoup, type_name: str) -> list[dict]:
    """Every JSON-LD object whose ``@type`` matches ``type_name``."""
    out: list[dict] = []
    for obj in _iter_ld_objects(soup):
        t = obj.get("@type")
        if (isinstance(t, str) and t == type_name) or (isinstance(t, list) and type_name in t):
            out.append(obj)
    return out


def has_author_signal(soup: BeautifulSoup) -> bool:
    """True if the page exposes any author attribution signal.

    Checks: ``<meta name="author">``, ``rel="author"``, an author/byline CSS
    class, or an ``author`` property in JSON-LD.
    """
    if soup.find("meta", attrs={"name": "author"}):
        return True
    if soup.find(attrs={"rel": "author"}):
        return True
    for el in soup.find_all(attrs={"class": _AUTHOR_CLASS_RE}):
        if isinstance(el, Tag):
            return True
    for obj in _iter_ld_objects(soup):
        author = obj.get("author")
        if author:
            return True
    return False


def has_author_entity(soup: BeautifulSoup) -> bool:
    """True if authorship is a structured Person/Organization entity, not a bare string.

    AI answer engines weight an explicit author *entity* (with a type/name) far
    more than an unlinked byline string.
    """
    if schema_objects_of_type(soup, "Person"):
        return True
    for obj in _iter_ld_objects(soup):
        author = obj.get("author")
        if isinstance(author, dict) and author.get("@type") in {"Person", "Organization"}:
            return True
        if isinstance(author, list) and any(
            isinstance(a, dict) and a.get("@type") in {"Person", "Organization"} for a in author
        ):
            return True
    return False


def extract_content_date(soup: BeautifulSoup) -> datetime | None:
    """Best-effort publish/modify date, preferring the most recent one found.

    Sources (ISO-8601): ``article:modified_time`` / ``article:published_time``
    meta, ``<time datetime>``, JSON-LD ``dateModified`` / ``datePublished``.
    Returns a timezone-aware UTC datetime, or ``None`` when no date is present.
    """
    candidates: list[str] = []
    for prop in ("article:modified_time", "article:published_time"):
        meta = soup.find("meta", attrs={"property": prop})
        if isinstance(meta, Tag) and meta.get("content"):
            candidates.append(str(meta.get("content")))
    for t in soup.find_all("time"):
        if isinstance(t, Tag) and t.get("datetime"):
            candidates.append(str(t.get("datetime")))
    for obj in _iter_ld_objects(soup):
        for key in ("dateModified", "datePublished"):
            val = obj.get(key)
            if isinstance(val, str):
                candidates.append(val)

    parsed = [d for d in (_parse_date(c) for c in candidates) if d is not None]
    return max(parsed) if parsed else None


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        m = _ISO_DATE_RE.search(raw)
        if not m:
            return None
        try:
            dt = datetime.fromisoformat(m.group(0))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def days_since(dt: datetime) -> int:
    """Whole days between ``dt`` and now (UTC). Negative for future dates."""
    return (datetime.now(UTC) - dt).days


def external_link_hrefs(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Absolute http(s) links whose host differs from the page's host."""
    page_host = urlparse(page_url).netloc.lower() if page_url else ""
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href", "")).strip()
        parsed = urlparse(href)
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and (not page_host or parsed.netloc.lower() != page_host)
        ):
            out.append(href)
    return out


def internal_and_external_links(soup: BeautifulSoup, page_url: str) -> tuple[int, int]:
    """Count (internal, external) navigable http(s)/relative anchor links."""
    page_host = urlparse(page_url).netloc.lower() if page_url else ""
    internal = external = 0
    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        parsed = urlparse(href)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc and page_host and parsed.netloc.lower() != page_host:
            external += 1
        else:
            internal += 1
    return internal, external
