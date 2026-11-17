"""Shared finding factory for the SEO scanner package."""

from __future__ import annotations

import re

from src.domains.critique.services.scanners.base import make_finding

# Valid 2-letter ISO 639-1 language codes (subset covering common ones).
# We accept any 2-letter lowercase alpha code for simplicity.
_HREFLANG_RE = re.compile(r"^x-default$|^[a-z]{2}(-[a-zA-Z]{2,4})?$")


def _seo_finding(
    *,
    severity: str,
    title: str,
    description: str,
    url: str = "",
    recommendation: str = "",
    selector: str | None = None,
    check_id: str | None = None,
    evidence: str | None = None,
    false_positive_risk: str = "low",
    remediation_effort: str = "low",
) -> dict:
    """SEO scanner finding with default metadata."""
    return make_finding(
        category="seo",
        severity=severity,
        title=title,
        description=description,
        url=url,
        recommendation=recommendation,
        selector=selector,
        check_id=check_id,
        evidence=evidence,
        source_tool="seo_scanner",
        false_positive_risk=false_positive_risk,
        remediation_effort=remediation_effort,
    )
