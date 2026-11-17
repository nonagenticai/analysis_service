"""Structural SEO checks (canonical, internal nofollow, URL structure, structured data, breadcrumb)."""

from __future__ import annotations

import json
from typing import cast
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.base import is_homepage_url
from src.domains.critique.services.scanners.seo.base import _seo_finding


def check_canonical(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    canonical = soup.find("link", attrs={"rel": "canonical"})
    if not canonical:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Missing canonical URL",
                description=(
                    "No canonical link tag found. This can cause duplicate content issues."
                ),
                url=url,
                recommendation='Add <link rel="canonical" href="..."> to the page head.',
                selector='link[rel="canonical"]',
                check_id="seo-canonical-missing",
            )
        )
    return findings


def jsonld_entities(data: object) -> list[dict]:
    """Top-level JSON-LD entities, unwrapping arrays and ``@graph`` containers.

    Only the entity level is returned -- nested values (``address``,
    ``aggregateRating``) are properties of an entity, not entities to audit.
    """
    entities: list[dict] = []
    stack: list[object] = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, list):
            stack[0:0] = node
        elif isinstance(node, dict):
            entity = cast("dict[str, object]", node)
            graph = entity.get("@graph")
            if isinstance(graph, list) and "@type" not in entity:
                stack[0:0] = graph
            else:
                entities.append(entity)
    return entities


def check_structured_data(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    ld_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    if not ld_scripts:
        findings.append(
            _seo_finding(
                severity="low",
                title="No structured data found",
                description=(
                    "No JSON-LD structured data found. Rich snippets improve search visibility."
                ),
                url=url,
                recommendation=(
                    "Add JSON-LD structured data (Organization, WebPage, BreadcrumbList, etc.)."
                ),
                selector='script[type="application/ld+json"]',
                check_id="seo-structured-data-missing",
                remediation_effort="medium",
            )
        )
    else:
        for script in ld_scripts:
            try:
                if script.string:
                    data = json.loads(script.string)
                    # Quality checks on valid JSON-LD entities. A document of
                    # the form {"@context": ..., "@graph": [...]} is a CONTAINER:
                    # the typed entities are its @graph members, and the
                    # container itself legitimately carries no @type.
                    for item in jsonld_entities(data):
                        if not item.get("@type"):
                            findings.append(
                                _seo_finding(
                                    severity="medium",
                                    title="Structured data missing @type",
                                    description=(
                                        "A JSON-LD block is missing the @type "
                                        "property, which is required for search "
                                        "engines to understand the data."
                                    ),
                                    url=url,
                                    recommendation=(
                                        "Add an @type property (e.g. 'Organization', "
                                        "'WebPage', 'Article') to the JSON-LD block."
                                    ),
                                    selector='script[type="application/ld+json"]',
                                    check_id="seo-structured-data-no-type",
                                    remediation_effort="medium",
                                )
                            )
                        if item.get("@type") and not (item.get("name") or item.get("headline")):
                            findings.append(
                                _seo_finding(
                                    severity="low",
                                    title="Structured data missing name/headline",
                                    description=(
                                        f"A JSON-LD block of type '{item['@type']}' "
                                        "is missing both 'name' and 'headline' "
                                        "properties."
                                    ),
                                    url=url,
                                    recommendation=(
                                        "Add a 'name' or 'headline' property to "
                                        "provide a title for the structured data."
                                    ),
                                    selector='script[type="application/ld+json"]',
                                    check_id="seo-structured-data-no-name",
                                    remediation_effort="medium",
                                )
                            )
            except json.JSONDecodeError:
                findings.append(
                    _seo_finding(
                        severity="medium",
                        title="Invalid JSON-LD structured data",
                        description="A JSON-LD script contains invalid JSON.",
                        url=url,
                        recommendation="Fix the JSON syntax in the structured data script.",
                        selector='script[type="application/ld+json"]',
                        check_id="seo-structured-data-invalid",
                        remediation_effort="medium",
                    )
                )
    return findings


def check_internal_nofollow(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag <a rel='nofollow'> links pointing to the same domain."""
    findings: list[dict] = []
    if not url:
        return findings

    page_domain = urlparse(url).netloc.lower()
    flagged: list[str] = []

    for anchor in soup.find_all("a", href=True):
        rel = anchor.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        if "nofollow" not in [r.lower() for r in rel]:
            continue
        href = str(anchor.get("href", ""))
        parsed_href = urlparse(href)
        href_domain = (parsed_href.netloc or page_domain).lower()
        if href_domain == page_domain:
            flagged.append(href)

    if flagged:
        sample = flagged[:5]
        extra = f" (and {len(flagged) - 5} more)" if len(flagged) > 5 else ""
        findings.append(
            _seo_finding(
                severity="medium",
                title=f"{len(flagged)} internal link(s) with rel='nofollow'",
                description=(
                    f"{len(flagged)} internal link(s) use rel='nofollow', "
                    "which prevents search engines from following them and "
                    "wastes internal PageRank."
                ),
                url=url,
                recommendation=(
                    "Remove rel='nofollow' from internal links so search "
                    "engines can crawl and index linked pages."
                ),
                selector="a[rel='nofollow']",
                check_id="seo-internal-nofollow",
                evidence=f"Links: {', '.join(sample)}{extra}",
            )
        )
    return findings


def check_breadcrumb_schema(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check for BreadcrumbList in JSON-LD structured data."""
    findings: list[dict] = []
    if is_homepage_url(url):
        # A site root has no parent trail; a BreadcrumbList there is a single
        # "Home" crumb that search engines do not render.
        return findings
    ld_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})

    for script in ld_scripts:
        try:
            if script.string:
                data = json.loads(script.string)
                if any(e.get("@type") == "BreadcrumbList" for e in jsonld_entities(data)):
                    return findings
        except json.JSONDecodeError, TypeError:
            continue

    findings.append(
        _seo_finding(
            severity="low",
            title="Missing BreadcrumbList structured data",
            description=(
                "No BreadcrumbList JSON-LD structured data found. "
                "Breadcrumb schema helps search engines understand site "
                "hierarchy and can generate rich breadcrumb trails in "
                "search results."
            ),
            url=url,
            recommendation=(
                "Add JSON-LD BreadcrumbList structured data to help "
                "search engines display breadcrumb navigation in results."
            ),
            selector='script[type="application/ld+json"]',
            check_id="seo-breadcrumb-missing",
            false_positive_risk="medium",
            remediation_effort="medium",
        )
    )
    return findings


def check_canonical_mismatch(soup: BeautifulSoup, url: str) -> list[dict]:
    """Flag canonical URLs that use a different protocol than the page."""
    findings: list[dict] = []
    if not url:
        return findings

    canonical = soup.find("link", attrs={"rel": "canonical"})
    if not canonical:
        return findings

    canonical_href = str(canonical.get("href", "")).strip()
    if not canonical_href:
        return findings

    page_scheme = urlparse(url).scheme.lower()
    canon_scheme = urlparse(canonical_href).scheme.lower()

    if page_scheme and canon_scheme and page_scheme != canon_scheme:
        findings.append(
            _seo_finding(
                severity="medium",
                title="Canonical URL protocol mismatch",
                description=(
                    f"The page is served over {page_scheme.upper()} but "
                    f"the canonical URL uses {canon_scheme.upper()} "
                    f"({canonical_href}). This can confuse search engine "
                    "indexing."
                ),
                url=url,
                recommendation=(
                    "Ensure the canonical URL uses the same protocol "
                    "as the page (preferably HTTPS)."
                ),
                selector='link[rel="canonical"]',
                check_id="seo-canonical-protocol-mismatch",
                evidence=f"Page: {page_scheme} | Canonical: {canonical_href}",
            )
        )
    return findings


def check_url_structure(url: str) -> list[dict]:
    """Analyze the URL structure for SEO issues."""
    findings: list[dict] = []
    if not url:
        return findings

    parsed = urlparse(url)

    # Check for underscores in path
    if "_" in parsed.path:
        findings.append(
            _seo_finding(
                severity="low",
                title="URL contains underscores",
                description=(
                    "The URL path contains underscores. Search engines treat hyphens "
                    "as word separators but not underscores."
                ),
                url=url,
                recommendation="Use hyphens (-) instead of underscores (_) in URLs.",
                check_id="seo-url-underscores",
                remediation_effort="medium",
            )
        )

    # Check for excessive query parameters
    if parsed.query:
        params = parsed.query.split("&")
        if len(params) > 3:
            findings.append(
                _seo_finding(
                    severity="low",
                    title="Excessive URL query parameters",
                    description=(
                        f"The URL has {len(params)} query parameters. "
                        "Too many parameters can dilute page authority and "
                        "cause crawl issues."
                    ),
                    url=url,
                    recommendation=(
                        "Reduce query parameters to essential ones. "
                        "Consider using cleaner URL patterns."
                    ),
                    check_id="seo-url-query-params",
                    remediation_effort="medium",
                )
            )

    # Check for excessive depth
    path_segments = [s for s in parsed.path.split("/") if s]
    if len(path_segments) > 4:
        findings.append(
            _seo_finding(
                severity="info",
                title="Deep URL structure",
                description=(
                    f"The URL has {len(path_segments)} path segments. "
                    "Deep URLs may receive less crawl priority."
                ),
                url=url,
                recommendation=(
                    "Consider flattening the URL structure to keep important "
                    "pages within 3-4 levels of depth."
                ),
                check_id="seo-url-depth",
                remediation_effort="medium",
            )
        )

    return findings
