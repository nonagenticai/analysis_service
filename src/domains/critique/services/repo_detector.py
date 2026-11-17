"""Detect GitHub/GitLab repository URL from HTML content.

Checks meta tags, link tags, footer/nav links, package.json links,
and common patterns like 'View on GitHub'.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

REPO_PATTERN = re.compile(
    r"https?://(?:www\.)?"
    r"(?:github\.com|gitlab\.com|bitbucket\.org)"
    r"/[\w.\-]+/[\w.\-]+",
    re.IGNORECASE,
)


@dataclass
class RepoDetection:
    """Result of repository URL detection."""

    url: str | None = None
    confidence: str = "none"
    source: str = ""
    candidates: list[str] = field(default_factory=list)


def _candidates_from_meta(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for meta in soup.find_all("meta"):
        if not isinstance(meta, Tag):
            continue
        content = str(meta.get("content", ""))
        name_or_prop = str(meta.get("name", meta.get("property", "")))
        if any(kw in name_or_prop.lower() for kw in ("repo", "github", "source")):
            match = REPO_PATTERN.search(content)
            if match:
                out.append((match.group(0), "high", "meta tag"))
    return out


def _candidates_from_links(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for link in soup.find_all("link"):
        if not isinstance(link, Tag):
            continue
        match = REPO_PATTERN.search(str(link.get("href", "")))
        if match:
            out.append((match.group(0), "medium", "link tag"))
    return out


def _candidates_from_anchors(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for anchor in soup.find_all("a"):
        if not isinstance(anchor, Tag):
            continue
        text = anchor.get_text(strip=True).lower()
        if not any(kw in text for kw in ("github", "source code", "view source", "repository")):
            continue
        match = REPO_PATTERN.search(str(anchor.get("href", "")))
        if match:
            out.append((match.group(0), "high", "anchor text"))
    return out


def _candidates_from_footer_nav(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for container in soup.find_all(["footer", "nav"]):
        if not isinstance(container, Tag):
            continue
        for anchor in container.find_all("a"):
            if not isinstance(anchor, Tag):
                continue
            match = REPO_PATTERN.search(str(anchor.get("href", "")))
            if match:
                out.append((match.group(0), "medium", "footer/nav link"))
    return out


def _candidates_from_body(
    soup: BeautifulSoup, existing: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    body = soup.find("body")
    if not (body and isinstance(body, Tag)):
        return []
    seen = {c[0] for c in existing}
    out: list[tuple[str, str, str]] = []
    for anchor in body.find_all("a"):
        if not isinstance(anchor, Tag):
            continue
        match = REPO_PATTERN.search(str(anchor.get("href", "")))
        if match and match.group(0) not in seen:
            out.append((match.group(0), "low", "body link"))
            seen.add(match.group(0))
    return out


def detect_repo_url(html: str) -> RepoDetection:
    """Detect repository URL from HTML content."""
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[str, str, str]] = []
    candidates.extend(_candidates_from_meta(soup))
    candidates.extend(_candidates_from_links(soup))
    candidates.extend(_candidates_from_anchors(soup))
    candidates.extend(_candidates_from_footer_nav(soup))
    candidates.extend(_candidates_from_body(soup, candidates))

    if not candidates:
        return RepoDetection()

    confidence_order = {"high": 0, "medium": 1, "low": 2}
    candidates.sort(key=lambda c: confidence_order.get(c[1], 3))
    best = candidates[0]
    return RepoDetection(
        url=best[0],
        confidence=best[1],
        source=best[2],
        candidates=[c[0] for c in candidates],
    )
