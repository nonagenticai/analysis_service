"""Generative-Engine-Optimization (GEO) / AI-discoverability checks (group: ``geo``).

Signals that determine whether AI answer engines (ChatGPT, Perplexity, Google AI
Overviews, Claude) can access and confidently cite the page. Split into:

- Tier 0 sync (:func:`check_geo`) — structured author *entity*, content freshness.
- Tier 1 network (:func:`scan_geo_live`) — AI-crawler access in robots.txt and
  llms.txt presence; runs only on the async/critique path, never the sync scorer.

Namespace ``seo-geo-<check>`` → group ``geo``.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe_resolved
from src.domains.critique.services.scanners.security_scanner.http_guard import guarded_get
from src.domains.critique.services.scanners.seo.base import _seo_finding
from src.domains.critique.services.scanners.seo.content_utils import main_text, words
from src.domains.critique.services.scanners.seo.observations import ProbeObservation
from src.domains.critique.services.scanners.seo.signals import (
    days_since,
    extract_content_date,
    has_author_entity,
)

logger = logging.getLogger(__name__)

_MIN_WORDS = 200
# AI engines favour recently-updated content for anything time-sensitive.
_STALE_DAYS = 180
# The major AI/LLM crawler user-agents.
_AI_BOTS = ("GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot", "anthropic-ai")


def check_geo(soup: BeautifulSoup, url: str) -> list[dict]:
    """Tier-0 GEO checks (no network)."""
    findings: list[dict] = []
    if len(words(main_text(soup))) < _MIN_WORDS:
        findings.append(
            _seo_finding(
                severity="low",
                title="Not enough content to assess AI-discoverability",
                description=(
                    "The page has too little body text to evaluate GEO/AI-citation signals "
                    "(author entity, freshness, definitional depth). Thin pages are rarely "
                    "cited by AI answer engines."
                ),
                url=url,
                recommendation="Add substantive, well-structured body content the page can be cited for.",
                check_id="seo-geo-insufficient-content",
                false_positive_risk="low",
            )
        )
        return findings

    if not has_author_entity(soup):
        findings.append(
            _seo_finding(
                severity="low",
                title="Author is not a structured entity",
                description=(
                    "Authorship is not expressed as a Person/Organization entity in structured "
                    "data. AI answer engines weight an explicit author entity far more than a "
                    "plain byline when deciding whether to cite a page."
                ),
                url=url,
                recommendation="Mark up the author as a Person entity (with sameAs/credentials) in JSON-LD.",
                check_id="seo-geo-no-author-entity",
                false_positive_risk="medium",
            )
        )

    date = extract_content_date(soup)
    if date is not None and days_since(date) > _STALE_DAYS:
        findings.append(
            _seo_finding(
                severity="low",
                title=f"Content not updated in {days_since(date)} days",
                description=(
                    f"The last update was {days_since(date)} days ago. A recent dateModified "
                    "(within ~90 days) markedly increases AI-answer citation for time-sensitive "
                    "queries."
                ),
                url=url,
                recommendation="Review and refresh the content, updating dateModified.",
                check_id="seo-geo-stale-content",
                evidence=date.date().isoformat(),
                false_positive_risk="medium",
            )
        )

    return findings


def _ai_blocked_finding(url: str, blocked: list[str]) -> dict:
    return _seo_finding(
        severity="medium",
        title=f"AI crawlers blocked: {', '.join(blocked)}",
        description=(
            f"robots.txt disallows {', '.join(blocked)}. Blocking AI crawlers "
            "removes the page from ChatGPT/Perplexity/Google-AI answers entirely."
        ),
        url=url,
        recommendation="Allow the AI crawlers you want to be cited by in robots.txt.",
        check_id="seo-geo-ai-crawler-blocked",
        evidence=", ".join(blocked),
        false_positive_risk="low",
    )


def _no_llms_txt_finding(url: str) -> dict:
    return _seo_finding(
        severity="info",
        title="No llms.txt",
        description=(
            "No /llms.txt was found. It is not required for AI citation, but it "
            "offers a curated map of your key content to LLM tooling."
        ),
        url=url,
        recommendation="Consider publishing an /llms.txt summarising your key pages.",
        check_id="seo-geo-no-llms-txt",
        false_positive_risk="medium",
    )


def _observe_robots(obs: ProbeObservation, origin: str, status: int, text: str) -> list[str]:
    """Record the robots.txt observation; return the AI bots it blocks."""
    blocked = _ai_bots_blocked(text) if status == 200 else []
    obs["robots"] = {
        "url": origin + "/robots.txt",
        "status": status,
        "ai_bots_blocked": blocked,
        "search_engines_blocked": _search_engines_blocked(text) if status == 200 else False,
    }
    return blocked


async def scan_geo_live(url: str, *, observe: ProbeObservation | None = None) -> list[dict]:
    """Tier-1 GEO checks needing a network fetch (robots.txt AI access, llms.txt).

    The entry screen on *url* is necessary but not sufficient, for the two
    reasons spelled out in
    :mod:`~src.domains.critique.services.scanners.security_scanner.http_guard`:
    the URLs actually dereferenced are re-derived (``{origin}/robots.txt``,
    ``{origin}/llms.txt``) rather than the string that was screened, and this
    client used to follow redirects, so a public host was free to answer
    ``302 Location: http://169.254.169.254/…`` or point at the fleet's
    ``100.64.0.0/10`` tailnet. Both fetches therefore go through
    :func:`guarded_get`, which re-screens every hop.

    A block raises :class:`ScannerSsrfBlockedError`, an ``OSError``; it is caught
    alongside the transport errors below and degrades to "no GEO-live findings",
    exactly as an unreachable site already did.

    ``observe``, when given, is filled with what was actually fetched
    (``ran``, ``reason``, ``robots`` {status, ai_bots_blocked,
    search_engines_blocked}, ``llms_txt`` {url, status}, ``error``) so the audit
    can report a check that never completed as ``not_run`` rather than ``pass``.
    """
    findings: list[dict] = []
    obs: ProbeObservation = observe if observe is not None else {}
    obs["ran"] = False
    obs["reason"] = None
    obs["robots"] = {}
    obs["llms_txt"] = {}
    obs["error"] = None
    if not url or not await _is_ssrf_safe_resolved(url):
        obs["reason"] = "the URL was rejected by the SSRF guard"
        return findings
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    obs["origin"] = origin

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            obs["ran"] = True
            robots = await guarded_get(client, f"{origin}/robots.txt")
            blocked = _observe_robots(obs, origin, robots.status_code, robots.text)
            if blocked:
                findings.append(_ai_blocked_finding(url, blocked))

            llms = await guarded_get(client, f"{origin}/llms.txt")
            obs["llms_txt"] = {"url": f"{origin}/llms.txt", "status": llms.status_code}
            if llms.status_code == 404:
                findings.append(_no_llms_txt_finding(url))
    except (httpx.HTTPError, OSError) as exc:
        # OSError covers ScannerSsrfBlockedError (a blocked hop) as well as the
        # ordinary socket failures, so a refused target degrades identically to
        # an unreachable one instead of failing the whole SEO scan.
        logger.debug("scan_geo_live fetch failed for %s: %s", url, exc)
        obs["error"] = f"{type(exc).__name__}: {exc}"[:240]

    return findings


def _parse_robots_groups(robots_text: str) -> dict[str, dict[str, list[str]]]:
    """Parse robots.txt into ``{user-agent(lower): {"disallow": [...], "allow": [...]}}``.

    Follows RFC 9309 grouping: *consecutive* ``User-agent`` lines share the rules
    that follow them, and the first rule line closes the user-agent accumulation
    so the next ``User-agent`` line starts a fresh group.
    """
    groups: dict[str, dict[str, list[str]]] = {}
    current_agents: list[str] = []
    expecting_agents = False  # True between a User-agent line and the group's first rule
    for raw in robots_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if not expecting_agents:  # a rule (or nothing) preceded this → new group
                current_agents = []
                expecting_agents = True
            agent = value.lower()
            current_agents.append(agent)
            groups.setdefault(agent, {"disallow": [], "allow": []})
        elif field in ("disallow", "allow") and current_agents:
            expecting_agents = False
            for agent in current_agents:
                groups[agent][field].append(value)
    return groups


def _root_blocked(group: dict[str, list[str]]) -> bool:
    """True if the group disallows the site root and no ``Allow: /`` re-permits it.

    An empty ``Disallow:`` means "allow everything"; a bare ``Disallow: /`` blocks
    the whole site. ``Allow: /`` wins on equal specificity (Google/RFC precedence).
    """
    if not any(rule == "/" for rule in group.get("disallow", [])):
        return False
    return not any(rule == "/" for rule in group.get("allow", []))


# Major search-engine crawlers: a root Disallow for these makes the page
# non-indexable regardless of its own meta robots.
_SEARCH_BOTS = ("Googlebot", "Bingbot")


def _search_engines_blocked(robots_text: str) -> bool:
    """True if robots.txt disallows the site root for a major search crawler."""
    groups = _parse_robots_groups(robots_text)
    star = groups.get("*")
    for bot in _SEARCH_BOTS:
        effective = groups.get(bot.lower(), star)
        if effective is not None and _root_blocked(effective):
            return True
    return False


def _ai_bots_blocked(robots_text: str) -> list[str]:
    """Return the AI bots that robots.txt disallows from the site root.

    A bot with its own user-agent group is governed by that group **only** — the
    ``*`` group does not apply to it (most-specific match wins). A bot with no own
    group falls back to ``*``.
    """
    groups = _parse_robots_groups(robots_text)
    star = groups.get("*")
    blocked: list[str] = []
    for bot in _AI_BOTS:
        effective = groups.get(bot.lower(), star)
        if effective is not None and _root_blocked(effective):
            blocked.append(bot)
    return blocked
