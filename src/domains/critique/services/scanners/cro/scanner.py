"""CRO (conversion-rate-optimization) scanner.

Deterministic, HTML-in conversion audit. Emits ``category="cro"`` findings in the
``cro-<section>-<check>`` namespace across eight sub-sections: form friction,
hero, CTA quality, social proof, value proposition, objection handling,
technical friction (speed+mobile, composed from existing scanners), and footer.
Same ``scan(html, url, *, soup)`` contract as ``SEOScanner`` — runs in a thread
from the sync fast-path and as an opt-in critique category.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.cro import (
    cta,
    footer,
    form_friction,
    hero,
    objection,
    social_proof,
    tech_friction,
    value_prop,
)

logger = logging.getLogger(__name__)


class CROScanner:
    """Scans HTML pages for conversion-rate-optimization issues."""

    def scan(self, html: str, url: str = "", *, soup: BeautifulSoup | None = None) -> list[dict]:
        """Scan HTML for CRO issues and return findings."""
        findings: list[dict] = []
        if soup is None:
            soup = BeautifulSoup(html, "lxml")

        findings.extend(form_friction.check_form_friction(soup, url))
        findings.extend(hero.check_hero(soup, url))
        findings.extend(cta.check_cta(soup, url))
        findings.extend(social_proof.check_social_proof(soup, url))
        findings.extend(value_prop.check_value_prop(soup, url))
        findings.extend(objection.check_objection(soup, url))
        findings.extend(tech_friction.check_tech_friction(soup, url))
        findings.extend(footer.check_footer(soup, url))

        return findings
