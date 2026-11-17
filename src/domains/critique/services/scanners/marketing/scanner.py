"""Marketing scanner — the go-to-market-only checks (competitive/brand/growth).

Deterministic, HTML-in. Emits ``category="marketing"`` findings in the
``mkt-<dimension>-<check>`` namespace for the three dimensions that have no
existing scanner. Content, conversion, and SEO dimensions of the marketing
scorecard are sourced by *reusing* the CRO and SEO scanners directly (see
``domains/marketing/service.py``) — this scanner deliberately does not duplicate
them. Same ``scan(html, url, *, soup)`` contract as ``CROScanner`` / ``SEOScanner``.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.marketing import brand, competitive, growth, tracking

logger = logging.getLogger(__name__)


class MarketingScanner:
    """Scans HTML for the competitive / brand / growth marketing dimensions."""

    def scan(self, html: str, url: str = "", *, soup: BeautifulSoup | None = None) -> list[dict]:
        """Scan HTML for go-to-market issues and return findings."""
        findings: list[dict] = []
        if soup is None:
            soup = BeautifulSoup(html, "lxml")

        findings.extend(competitive.check_competitive(soup, url))
        findings.extend(brand.check_brand_trust(soup, url))
        findings.extend(growth.check_growth(soup, url))
        findings.extend(tracking.check_tracking(soup, url))

        return findings
