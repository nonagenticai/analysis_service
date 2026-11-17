"""Shared finding factory for all scanners."""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

_HOMEPAGE_PATHS = frozenset({"", "/", "/index.html", "/index.htm", "/index.php"})


def is_homepage_url(url: str) -> bool:
    """True when ``url`` is a site root. Breadcrumbs have no meaning there.

    A URL with no host (a static artifact scored without an origin) is NOT
    assumed to be a homepage -- unknown never suppresses a finding.
    """
    parsed = urlparse(url)
    return bool(parsed.netloc) and parsed.path.lower() in _HOMEPAGE_PATHS


def make_finding(
    category: str,
    severity: str,
    title: str,
    description: str,
    url: str = "",
    recommendation: str = "",
    selector: str | None = None,
    check_id: str | None = None,
    wcag_criterion: str | None = None,
    evidence: str | None = None,
    source_tool: str | None = None,
    false_positive_risk: str | None = None,
    remediation_effort: str | None = None,
) -> dict:
    """Create a standardized finding dict used by all scanners.

    Args:
        category: Finding category (seo, security, accessibility, performance, ux, code_quality).
        severity: One of critical, high, medium, low, info.
        title: Short descriptive title.
        description: Detailed explanation.
        url: URL where the finding was detected.
        recommendation: Actionable fix suggestion.
        selector: CSS selector identifying the element.
        check_id: Stable identifier for this check type (e.g. "seo-missing-title").
        wcag_criterion: WCAG criterion reference (e.g. "1.1.1").
        evidence: Raw evidence snippet (header value, HTML excerpt, etc.).
        source_tool: Tool/scanner that produced this finding.
        false_positive_risk: Estimated false-positive risk (low, medium, high).
        remediation_effort: Estimated effort to fix (low, medium, high).
    """
    finding: dict = {
        "finding_id": str(uuid.uuid7()),
        "category": category,
        "severity": severity,
        "title": title,
        "description": description,
        "url": url,
        "recommendation": recommendation,
        "selector": selector,
    }
    if check_id is not None:
        finding["check_id"] = check_id
    if wcag_criterion is not None:
        finding["wcag_criterion"] = wcag_criterion
    if evidence is not None:
        finding["evidence"] = evidence
    if source_tool is not None:
        finding["source_tool"] = source_tool
    if false_positive_risk is not None:
        finding["false_positive_risk"] = false_positive_risk
    if remediation_effort is not None:
        finding["remediation_effort"] = remediation_effort
    return finding
