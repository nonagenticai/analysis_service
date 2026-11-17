"""DOM-related performance checks (DOM size, depth)."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

from src.domains.critique.services.scanners.perf.base import _perf_finding


def check_dom_size(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    all_elements = soup.find_all(True)
    count = len(all_elements)

    if count > 3000:
        severity = "high" if count > 5000 else "medium"
        findings.append(
            _perf_finding(
                severity=severity,
                title=f"Large DOM: {count} elements",
                description=(
                    f"The DOM contains {count} elements. Large DOMs slow "
                    "rendering and increase memory usage."
                ),
                url=url,
                recommendation=(
                    "Reduce DOM size by removing unnecessary wrappers, "
                    "using virtualization, or lazy loading content."
                ),
                check_id="perf-large-dom",
                remediation_effort="high",
            )
        )

    return findings


def check_dom_depth(soup: BeautifulSoup, url: str) -> list[dict]:
    """Check DOM tree depth."""
    findings: list[dict] = []

    def _max_depth(element: Tag, current: int = 0) -> int:
        max_d = current
        for child in element.children:
            if isinstance(child, Tag):
                max_d = max(max_d, _max_depth(child, current + 1))
        return max_d

    body = soup.find("body")
    if body and isinstance(body, Tag):
        depth = _max_depth(body)
        if depth > 20:
            findings.append(
                _perf_finding(
                    severity="high" if depth > 25 else "medium",
                    title=f"Deep DOM tree ({depth} levels)",
                    description=(
                        f"The DOM tree is {depth} levels deep. Deep DOMs "
                        "increase style recalculation time and memory usage."
                    ),
                    url=url,
                    recommendation=(
                        "Flatten the DOM structure by removing unnecessary "
                        "wrapper elements. Aim for a maximum depth of 15."
                    ),
                    check_id="perf-deep-dom",
                    remediation_effort="high",
                )
            )
        elif depth > 15:
            findings.append(
                _perf_finding(
                    severity="low",
                    title=f"Moderately deep DOM tree ({depth} levels)",
                    description=(
                        f"The DOM tree is {depth} levels deep. Consider "
                        "simplifying the nesting structure."
                    ),
                    url=url,
                    recommendation=("Review nested elements and reduce unnecessary wrapper divs."),
                    check_id="perf-deep-dom",
                    remediation_effort="high",
                )
            )
    return findings
