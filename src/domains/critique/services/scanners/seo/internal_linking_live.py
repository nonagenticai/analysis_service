"""Cross-page internal-linking analysis (Tier-1, network, opt-in, page-capped).

Site-wide orphan / broken-link / hub analysis that needs a small crawl — kept
out of the pure single-page :mod:`internal_linking` module and off the default
sync path. Enumerates the sitemap (or the ``robots.txt`` ``Sitemap:`` directive),
probes each page reachable from the home page's internal links, and compares the
sitemap's URL set against what the home page actually links to.

Every interior URL is SSRF-gated *individually* (a sitemap can list arbitrary
hosts) **and on every redirect hop**: the client is built with
``follow_redirects=False`` and every request goes through
:func:`~src.domains.critique.services.scanners.security_scanner.http_guard.guarded_get`
/ :func:`~src.domains.critique.services.scanners.security_scanner.http_guard.guarded_head`.
Screening only the entry URL was not enough — the crawl dereferences a set of
URLs it derives from sitemaps and hrefs, and any one of those public hosts could
answer ``302 Location: http://169.254.169.254/…`` (or point into the fleet's
``100.64.0.0/10`` tailnet, which ``ipaddress.is_private`` calls public).

The shared ``home_soup`` is **read-only** and never mutated — each interior
document is parsed into its own :class:`~bs4.BeautifulSoup`. One reused
``httpx.AsyncClient`` and an :class:`asyncio.Semaphore` bound the fan-out.

The broken-link probe uses ``HEAD`` (falling back to ``GET`` on 405/501); the
hub metric needs page bodies, so a 2xx page is additionally fetched with ``GET``
to tally inbound internal links. Namespace ``seo-linking-<check>`` → group
``internal_linking`` (via the ``linking`` prefix alias in :mod:`scoring`).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe_resolved
from src.domains.critique.services.scanners.security_scanner.http_guard import (
    guarded_get,
    guarded_head,
)
from src.domains.critique.services.scanners.seo.base import _seo_finding

logger = logging.getLogger(__name__)

_HTTP_HEADERS = {"User-Agent": "SeoLinkGraph/1.0"}
# Bound the sitemap-index fan-out and the number of hub findings emitted.
_MAX_CHILD_SITEMAPS = 3
_MAX_HUBS = 3
_MIN_HUB_INBOUND = 2
# Hard ceiling on emitted broken/orphan findings so one bad site cannot flood.
_MAX_REPORTED = 25


def _normalize_url(url: str) -> str:
    """Canonicalize for set-comparison: drop the fragment and trailing slash."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized


def _internal_hrefs(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Absolute, same-origin, normalized http(s) hrefs on the page (read-only).

    A read-only sibling of :mod:`signals` (whose link helpers return counts only):
    resolves relative hrefs against ``page_url`` and keeps only same-host links.
    Non-mutating — never calls ``extract``/``decompose`` on the shared soup.
    """
    page_host = urlparse(page_url).netloc.lower()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href", "")).strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if parsed.netloc.lower() != page_host:
            continue
        out.append(_normalize_url(absolute))
    return out


async def _fetch_text(client: httpx.AsyncClient, url: str) -> str | None:
    """GET ``url`` and return its text on 200, else ``None`` (fail-soft per URL).

    SSRF-gated on every call: a sitemap index or ``robots.txt`` ``Sitemap:``
    directive can point at an arbitrary host, so no document is fetched unless the
    exact URL passes :func:`_is_ssrf_safe_resolved`.

    Resolving, not structural: a sitemap index and a ``Sitemap:`` directive both
    hand us an arbitrary attacker-chosen host, and a structural screen only ever
    inspected the string. This is the funnel every sitemap document goes
    through, so it is where the ``<loc>``-into-the-estate bypass is closed.

    The explicit pre-check is kept for the debug log; :func:`guarded_get` then
    re-screens the target *and every redirect hop* with the same resolving
    predicate, and raises ``ScannerSsrfBlockedError`` (an ``OSError``, already in
    the ``except`` below) rather than following a chain into the estate. The
    entry screen and the hop screen are both needed: neither one can speak for
    the other's URLs.
    """
    if not await _is_ssrf_safe_resolved(url):
        logger.debug("skipping SSRF-unsafe fetch: %s", url)
        return None
    try:
        resp = await guarded_get(client, url, headers=_HTTP_HEADERS)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        logger.debug("fetch failed for %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    return resp.text


async def _parse_sitemap(client: httpx.AsyncClient, sitemap_url: str, *, depth: int) -> list[str]:
    """Fetch + parse one sitemap into a fresh XML soup, returning its ``<loc>`` URLs.

    Follows a ``<sitemapindex>`` one level deep (capped at ``_MAX_CHILD_SITEMAPS``).
    """
    text = await _fetch_text(client, sitemap_url)
    if not text:
        return []
    sm_soup = BeautifulSoup(text, "xml")
    if sm_soup.find("sitemapindex") is not None and depth < 1:
        child_urls = [
            loc.get_text(strip=True)
            for sm in sm_soup.find_all("sitemap")[:_MAX_CHILD_SITEMAPS]
            if isinstance(sm, Tag)
            for loc in sm.find_all("loc")
            if isinstance(loc, Tag) and loc.get_text(strip=True)
        ]
        urls: list[str] = []
        for child in child_urls:
            urls.extend(await _parse_sitemap(client, child, depth=depth + 1))
        return urls
    return [
        loc.get_text(strip=True)
        for loc in sm_soup.find_all("loc")
        if isinstance(loc, Tag) and loc.get_text(strip=True)
    ]


async def _robots_sitemaps(client: httpx.AsyncClient, origin: str) -> list[str]:
    """Sitemap URLs declared via ``Sitemap:`` directives in ``robots.txt``."""
    text = await _fetch_text(client, f"{origin}/robots.txt")
    if not text:
        return []
    sitemaps: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        field, _, value = line.partition(":")
        if field.strip().lower() == "sitemap" and value.strip():
            sitemaps.append(value.strip())
    return sitemaps


async def _enumerate_sitemap(
    client: httpx.AsyncClient, origin: str, *, max_pages: int
) -> list[str]:
    """Same-origin, SSRF-safe, deduped, capped page URLs from the site's sitemap."""
    locs = await _parse_sitemap(client, f"{origin}/sitemap.xml", depth=0)
    if not locs:
        for sm_url in await _robots_sitemaps(client, origin):
            locs.extend(await _parse_sitemap(client, sm_url, depth=0))
    origin_host = urlparse(origin).netloc.lower()
    seen: set[str] = set()
    out: list[str] = []
    for loc in locs:
        parsed = urlparse(loc)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != origin_host:
            continue
        if not await _is_ssrf_safe_resolved(loc):
            continue
        norm = _normalize_url(loc)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
        if len(out) >= max_pages:
            break
    return out


async def _crawl_page(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, url: str
) -> tuple[str, int | None, list[str]]:
    """Probe one page; return ``(url, status_code_or_None, internal_hrefs)``.

    ``HEAD`` for the cheap broken-link status (``GET`` fallback on 405/501); a 2xx
    page is additionally ``GET``-fetched so its internal links feed the inbound
    hub tally. Both go through the per-hop guards, so a link that ``302``s into
    the estate is refused mid-chain instead of probed. Per-task failures are
    swallowed (returns ``(url, None, [])``) so one dead page never cancels its
    siblings (narrow-except + gather sibling-cancel) — and a blocked hop, being
    an ``OSError``, degrades down that same path rather than into a finding.
    """
    if not await _is_ssrf_safe_resolved(url):
        return url, None, []
    async with sem:
        try:
            head_resp = await guarded_head(client, url, headers=_HTTP_HEADERS)
            status = head_resp.status_code
            body: str | None = None
            if status in {405, 501}:
                get_resp = await guarded_get(client, url, headers=_HTTP_HEADERS)
                status = get_resp.status_code
                body = get_resp.text if 200 <= status < 300 else None
            elif 200 <= status < 300:
                body = await _fetch_text(client, url)
        except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
            logger.debug("crawl failed for %s: %s", url, exc)
            return url, None, []
        hrefs: list[str] = []
        if body:
            try:
                page_soup = BeautifulSoup(body, "lxml")
            except (ValueError, TypeError) as exc:
                logger.debug("parse failed for %s: %s", url, exc)
            else:
                hrefs = _internal_hrefs(page_soup, url)
        return url, status, hrefs


def _build_crawl_set(
    home_links: list[str], sitemap_urls: list[str], home_norm: str, max_pages: int
) -> list[str]:
    """Deduped union of home links + sitemap pages (minus the home page), capped."""
    crawl_set: list[str] = []
    seen: set[str] = set()
    for candidate in [*home_links, *sitemap_urls]:
        if candidate in seen or candidate == home_norm:
            continue
        seen.add(candidate)
        crawl_set.append(candidate)
        if len(crawl_set) >= max_pages:
            break
    return crawl_set


def _broken_findings(results: list[tuple[str, int | None, list[str]]]) -> list[dict]:
    """One ``seo-linking-broken`` (high) finding per 4xx/5xx internal link."""
    findings: list[dict] = []
    for probe_url, status, _hrefs in results:
        if status is None or status < 400:
            continue
        findings.append(
            _seo_finding(
                severity="high",
                title="Broken internal link",
                description=(
                    f"The internal link {probe_url} returned HTTP {status}. Broken internal "
                    "links waste crawl budget and dead-end users."
                ),
                url=probe_url,
                recommendation="Fix or remove the broken internal link, or add a redirect.",
                check_id="seo-linking-broken",
                evidence=f"HTTP {status}",
                false_positive_risk="low",
                remediation_effort="medium",
            )
        )
        if len(findings) >= _MAX_REPORTED:
            break
    return findings


def _orphan_findings(
    sitemap_urls: list[str], home_link_set: set[str], home_norm: str
) -> list[dict]:
    """One ``seo-linking-orphan`` (medium) per sitemap page not linked from home."""
    orphans = [loc for loc in sitemap_urls if loc not in home_link_set and loc != home_norm]
    return [
        _seo_finding(
            severity="medium",
            title="Orphaned page (not linked from the home page)",
            description=(
                f"{loc} is listed in the sitemap but is not reachable from the home page's "
                "internal links. Orphaned pages receive little internal link equity and are "
                "harder to discover."
            ),
            url=loc,
            recommendation="Add an internal link to this page from a relevant hub or nav.",
            check_id="seo-linking-orphan",
            evidence=loc,
            false_positive_risk="medium",
            remediation_effort="medium",
        )
        for loc in orphans[:_MAX_REPORTED]
    ]


def _hub_findings(
    home_link_set: set[str], results: list[tuple[str, int | None, list[str]]]
) -> list[dict]:
    """Up to ``_MAX_HUBS`` ``seo-linking-hub`` (info) findings for top inbound pages."""
    inbound: Counter[str] = Counter()
    inbound.update(home_link_set)
    for _url, _status, hrefs in results:
        inbound.update(set(hrefs))
    findings: list[dict] = []
    for target, count in inbound.most_common(_MAX_HUBS):
        if count < _MIN_HUB_INBOUND:
            break
        findings.append(
            _seo_finding(
                severity="info",
                title=f"Internal-link hub: {target}",
                description=(
                    f"{target} receives {count} inbound internal links across the crawled "
                    "pages — it acts as an internal hub. Ensure hub pages target your most "
                    "important topics and pass equity onward."
                ),
                url=target,
                recommendation="Confirm hub pages are high-value and well-optimized.",
                check_id="seo-linking-hub",
                evidence=f"inbound_links={count}",
                false_positive_risk="high",
            )
        )
    return findings


async def scan_internal_linking_live(
    url: str,
    home_soup: BeautifulSoup | None = None,
    *,
    max_pages: int = 20,
    concurrency: int = 5,
    timeout: float = 10.0,  # noqa: ASYNC109 - forwarded to httpx.AsyncClient(timeout=...)
    observe: dict | None = None,
) -> list[dict]:
    """Cross-page internal-linking findings for a site, starting from ``url``.

    ``home_soup`` (if supplied) is the already-parsed home page; it is used
    read-only and never mutated. Emits ``seo-linking-broken`` (high),
    ``seo-linking-orphan`` (medium) and ``seo-linking-hub`` (info) findings.
    Fully SSRF-gated and page-capped; a fetch failure yields fewer findings,
    never an exception.

    ⛔ An empty return is ambiguous ("crawled, nothing wrong" vs "never crawled"),
    so ``observe`` (when given) is filled with ``{"ran", "reason",
    "pages_crawled", "pages_reached"}``. ``ran`` is False when the start URL is
    SSRF-blocked, the home page is unreachable, there is nothing to crawl, or no
    crawled page answered — the caller must then report the group UNMEASURED,
    never clean (design 2026-10-17 §6).
    """
    findings: list[dict] = []
    obs = observe if observe is not None else {}
    obs.update({"ran": False, "reason": None, "pages_crawled": 0, "pages_reached": 0})
    if not url or not await _is_ssrf_safe_resolved(url):
        obs["reason"] = "the start URL was rejected by the SSRF guard"
        return findings
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # follow_redirects=False: every hop is walked and re-screened by
    # guarded_get / guarded_head instead. See the module docstring.
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, headers=_HTTP_HEADERS
    ) as client:
        if home_soup is None:
            home_text = await _fetch_text(client, url)
            if not home_text:
                obs["reason"] = "the home page could not be fetched"
                return findings
            home_soup = BeautifulSoup(home_text, "lxml")

        home_norm = _normalize_url(url)
        home_links = _internal_hrefs(home_soup, url)
        home_link_set = set(home_links)

        sitemap_urls = await _enumerate_sitemap(client, origin, max_pages=max_pages)
        crawl_set = _build_crawl_set(home_links, sitemap_urls, home_norm, max_pages)

        sem = asyncio.Semaphore(concurrency)
        results = await asyncio.gather(*(_crawl_page(client, sem, u) for u in crawl_set))

    reached = sum(1 for _u, status, _h in results if status is not None)
    obs.update({"pages_crawled": len(crawl_set), "pages_reached": reached})
    if not crawl_set:
        obs["reason"] = "no internal links or sitemap pages to crawl"
        return findings
    if reached == 0:
        obs["reason"] = f"none of the {len(crawl_set)} internal pages could be fetched"
        return findings
    obs["ran"] = True
    findings.extend(_broken_findings(results))
    findings.extend(_orphan_findings(sitemap_urls, home_link_set, home_norm))
    findings.extend(_hub_findings(home_link_set, results))
    return findings
