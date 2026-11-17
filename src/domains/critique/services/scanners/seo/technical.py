"""Technical SEO checks (meta robots, favicon, hreflang, scan_live)."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.security_scanner import _is_ssrf_safe_resolved
from src.domains.critique.services.scanners.seo.base import _HREFLANG_RE, _seo_finding
from src.domains.critique.services.scanners.seo.observations import FileProbe, ProbeObservation

logger = logging.getLogger(__name__)


def check_meta_robots(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for meta robots directives that block indexing."""
    findings: list[dict] = []
    robots_tag = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.IGNORECASE)})
    if robots_tag:
        content = str(robots_tag.get("content", "")).lower()
        if "noindex" in content:
            findings.append(
                _seo_finding(
                    severity="high",
                    title="Meta robots noindex directive",
                    description=(
                        "The page has a meta robots noindex directive, "
                        "which prevents search engines from indexing it."
                    ),
                    url=url,
                    recommendation=(
                        "Remove the noindex directive if this page should appear in search results."
                    ),
                    selector='meta[name="robots"]',
                    check_id="seo-meta-robots-noindex",
                    evidence=content,
                )
            )
        if "nofollow" in content:
            findings.append(
                _seo_finding(
                    severity="medium",
                    title="Meta robots nofollow directive",
                    description=(
                        "The page has a meta robots nofollow directive, "
                        "which prevents search engines from following links."
                    ),
                    url=url,
                    recommendation=(
                        "Remove the nofollow directive if you want search "
                        "engines to follow links on this page."
                    ),
                    selector='meta[name="robots"]',
                    check_id="seo-meta-robots-nofollow",
                    evidence=content,
                )
            )
    return findings


def check_favicon(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for favicon link tags."""
    findings: list[dict] = []
    favicon = soup.find(
        "link",
        attrs={
            "rel": re.compile(r"(^|\s)(icon|shortcut icon|apple-touch-icon)(\s|$)", re.IGNORECASE)
        },
    )
    if not favicon:
        findings.append(
            _seo_finding(
                severity="low",
                title="Missing favicon",
                description=(
                    "No favicon link tag found. A favicon helps with brand "
                    "recognition in browser tabs and bookmarks."
                ),
                url=url,
                recommendation=(
                    'Add <link rel="icon" href="/favicon.ico"> or '
                    '<link rel="icon" type="image/png" href="/favicon.png"> '
                    "to the page head."
                ),
                selector='link[rel="icon"]',
                check_id="seo-favicon-missing",
            )
        )
    return findings


def check_hreflang(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check hreflang link tags for validity."""
    findings: list[dict] = []

    hreflang_tags = soup.find_all("link", attrs={"rel": "alternate", "hreflang": True})
    if not hreflang_tags:
        # No hreflang tags is fine -- not every site is multilingual.
        return findings

    hreflang_values: list[str] = []
    invalid_values: list[str] = []

    for tag in hreflang_tags:
        val = tag.get("hreflang", "")
        if isinstance(val, list):
            val = val[0] if val else ""
        val = str(val).strip()
        hreflang_values.append(val)
        if not _HREFLANG_RE.match(val):
            invalid_values.append(val)

    # Check for x-default
    if "x-default" not in hreflang_values:
        findings.append(
            _seo_finding(
                severity="low",
                title="Missing hreflang x-default",
                description=(
                    "Hreflang tags exist but no x-default is specified. "
                    "The x-default tells search engines which URL to show "
                    "for unsupported languages."
                ),
                url=url,
                recommendation=(
                    'Add <link rel="alternate" hreflang="x-default" href="..."> '
                    "to specify a default language fallback."
                ),
                selector='link[rel="alternate"][hreflang]',
                check_id="seo-hreflang-no-default",
            )
        )

    # Check for invalid hreflang values
    if invalid_values:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Invalid hreflang values",
                description=(
                    f"Invalid hreflang values found: {', '.join(invalid_values)}. "
                    "Values must be a valid 2-letter language code, optionally "
                    "with a region subtag (e.g. 'en', 'en-US')."
                ),
                url=url,
                recommendation="Fix the hreflang values to use valid ISO 639-1 language codes.",
                selector='link[rel="alternate"][hreflang]',
                check_id="seo-hreflang-invalid",
            )
        )

    # Check for duplicate hreflang values
    seen: set[str] = set()
    duplicates: list[str] = []
    for val in hreflang_values:
        if val in seen:
            duplicates.append(val)
        seen.add(val)
    if duplicates:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Duplicate hreflang values",
                description=(
                    f"Duplicate hreflang values found: {', '.join(duplicates)}. "
                    "Each language/region combination should appear only once."
                ),
                url=url,
                recommendation="Remove duplicate hreflang link elements.",
                selector='link[rel="alternate"][hreflang]',
                check_id="seo-hreflang-duplicate",
            )
        )

    return findings


def _robots_missing_finding(origin: str) -> dict:
    return _seo_finding(
        severity="medium",
        title="Missing robots.txt",
        description=(
            "No robots.txt file found at the site root. "
            "This file helps search engines understand crawl rules."
        ),
        url=f"{origin}/robots.txt",
        recommendation=(
            "Create a robots.txt file at the site root with appropriate crawl directives."
        ),
        check_id="seo-robots-txt-missing",
    )


def _sitemap_missing_finding(origin: str) -> dict:
    return _seo_finding(
        severity="low",
        title="Missing sitemap.xml",
        description=(
            "No sitemap.xml file found at the site root. "
            "Sitemaps help search engines discover pages."
        ),
        url=f"{origin}/sitemap.xml",
        recommendation=(
            "Create a sitemap.xml file listing all important pages and submit it to search engines."
        ),
        check_id="seo-sitemap-missing",
        remediation_effort="medium",
    )


def _errored_finding(origin: str, name: str, check_id: str, exc: BaseException) -> dict:
    """A TRANSPORT failure is NOT a site defect.

    Reporting it as ``*-missing`` costs the customer real SEO points and puts a
    false remediation ("create a robots.txt") in their PDF for a file they
    already have. Record it the way the security scanner records an unfinished
    check -- ``info``, zero penalty, and stating explicitly that this is not
    evidence of absence.
    """
    return _seo_finding(
        severity="info",
        title=f"{name} check could not complete",
        description=(
            f"{name} could not be fetched from {origin}, so this"
            " scan carries no result for it. This is NOT evidence"
            " that the file is missing."
        ),
        url=f"{origin}/{name}",
        recommendation=(
            "Re-run the scan; if it keeps failing, the origin is"
            " likely unreachable, slow, or resetting connections."
        ),
        check_id=check_id,
        evidence=f"{type(exc).__name__}: {exc}",
    )


async def _probe_file(
    client: httpx.AsyncClient,
    origin: str,
    name: str,
    *,
    missing: dict | None,
    errored_id: str,
    observed: FileProbe,
) -> list[dict]:
    """GET ``{origin}/{name}``; a 404 yields ``missing``, a transport error the errored marker.

    ``observed`` records what was actually seen (``url``, ``status``, ``error``)
    so the audit can tell "checked and present" from "never checked".
    """
    target = f"{origin}/{name}"
    observed["url"] = target
    observed["status"] = None
    observed["error"] = None
    try:
        resp = await client.get(target)
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        logger.debug("Could not fetch %s: %s", name, exc)
        observed["error"] = f"{type(exc).__name__}: {exc}"[:240]
        return [_errored_finding(origin, name, errored_id, exc)]
    observed["status"] = resp.status_code
    return [missing] if resp.status_code == 404 and missing is not None else []


async def scan_live(url: str, *, observe: ProbeObservation | None = None) -> list[dict]:
    """Make HTTP requests to check robots.txt and sitemap.xml existence.

    Called by the async critique path and (best-effort) by the synchronous SEO
    audit. ``observe``, when given, is filled with what was fetched —
    ``{"ran", "reason", "robots": {url, status, error}, "sitemap": {...}}`` —
    so a check that never ran can be reported ``not_run`` instead of ``pass``.
    """
    findings: list[dict] = []
    obs: ProbeObservation = observe if observe is not None else {}
    robots_seen: FileProbe = {}
    sitemap_seen: FileProbe = {}
    obs["ran"] = False
    obs["reason"] = None
    obs["robots"] = robots_seen
    obs["sitemap"] = sitemap_seen

    if not await _is_ssrf_safe_resolved(url):
        logger.warning("Blocked SSRF attempt to %s", url)
        obs["reason"] = "the URL was rejected by the SSRF guard"
        return findings

    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        obs["ran"] = True
        findings.extend(
            await _probe_file(
                client,
                origin,
                "robots.txt",
                missing=_robots_missing_finding(origin),
                errored_id="seo-robots-txt-errored",
                observed=robots_seen,
            )
        )
        # See _errored_finding: "we could not check" must stay distinguishable
        # from "it is missing".
        findings.extend(
            await _probe_file(
                client,
                origin,
                "sitemap.xml",
                missing=_sitemap_missing_finding(origin),
                errored_id="seo-sitemap-errored",
                observed=sitemap_seen,
            )
        )

    return findings
