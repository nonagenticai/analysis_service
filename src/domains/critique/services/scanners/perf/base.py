"""Shared finding factory for the performance scanner package."""

from __future__ import annotations

from src.domains.critique.services.scanners.base import make_finding


def _perf_finding(
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
    """Performance scanner finding with default metadata."""
    return make_finding(
        category="performance",
        severity=severity,
        title=title,
        description=description,
        url=url,
        recommendation=recommendation,
        selector=selector,
        check_id=check_id,
        evidence=evidence,
        source_tool="performance_scanner",
        false_positive_risk=false_positive_risk,
        remediation_effort=remediation_effort,
    )
