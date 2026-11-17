"""Technical-friction (speed + mobile) CRO checks — composed from existing scanners.

Page speed and mobile usability have hard conversion evidence (Google/Deloitte/
Portent: a 0.1s speed gain lifted retail conversion +8.4%). Rather than duplicate
logic, this module *reuses* the pure perf/mobile/a11y check functions on the
shared soup and re-tags a bounded, deduplicated subset under the ``cro-tech-*``
namespace so they feed the CRO conversion score. Lighthouse/CWV (render-tier) are
intentionally excluded from this deterministic path.
"""

from __future__ import annotations

from collections.abc import Callable

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.a11y import operable
from src.domains.critique.services.scanners.cro.base import _cro_finding
from src.domains.critique.services.scanners.perf import cls, dom, lcp
from src.domains.critique.services.scanners.seo import mobile

# (source check function, cro-tech check_id). One re-tagged finding per source
# check, capped so a single flaw cannot dominate the score.
_SOURCES: list[tuple[Callable[[BeautifulSoup, str], list[dict]], str]] = [
    (lcp.check_render_blocking, "cro-tech-render-blocking"),
    (cls.check_image_dimensions, "cro-tech-cls-images"),
    (dom.check_dom_size, "cro-tech-large-dom"),
    (mobile.check_viewport, "cro-tech-no-viewport"),
    (mobile.check_mobile_font_sizes, "cro-tech-small-fonts"),
    (operable.check_touch_targets, "cro-tech-small-touch-target"),
]


def check_tech_friction(soup: BeautifulSoup, url: str) -> list[dict]:
    findings: list[dict] = []
    for source_fn, check_id in _SOURCES:
        source_findings = source_fn(soup, url)
        if not source_findings:
            continue
        src = source_findings[0]  # cap at one per check — bound the penalty
        findings.append(
            _cro_finding(
                severity=src.get("severity", "low"),
                title=src.get("title", ""),
                description=src.get("description", ""),
                url=url,
                recommendation=src.get("recommendation", ""),
                selector=src.get("selector"),
                check_id=check_id,
                evidence=f"via {src.get('check_id', src.get('source_tool', 'scanner'))}",
                remediation_effort=src.get("remediation_effort", "medium"),
            )
        )
    return findings
