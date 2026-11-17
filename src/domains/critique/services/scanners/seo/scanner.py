"""SEO scanner for website critique.

Checks: title tag, meta description, Open Graph tags, heading hierarchy,
canonical URL, image alt text, structured data (JSON-LD), Twitter cards,
hreflang tags, URL structure, robots.txt, sitemap.xml.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.seo import (
    eeat,
    geo,
    internal_linking,
    keyword,
    mobile,
    on_page,
    readability,
    schema_coverage,
    snippet,
    social,
    structural,
    technical,
)
from src.domains.critique.services.scanners.seo.observations import ProbeObservation

logger = logging.getLogger(__name__)


class SEOScanner:
    """Scans HTML pages for SEO issues."""

    def scan(self, html: str, url: str = "", *, soup: BeautifulSoup | None = None) -> list[dict]:
        """Scan HTML for SEO issues and return findings."""
        findings: list[dict] = []
        if soup is None:
            soup = BeautifulSoup(html, "lxml")

        findings.extend(self._check_title(soup, url))
        findings.extend(self._check_meta_description(soup, url))
        findings.extend(self._check_og_tags(soup, url))
        findings.extend(self._check_heading_hierarchy(soup, url))
        findings.extend(self._check_canonical(soup, url))
        findings.extend(self._check_image_alt(soup, url))
        findings.extend(self._check_structured_data(soup, url))
        findings.extend(self._check_twitter_cards(soup, url))
        findings.extend(self._check_hreflang(soup, url))
        findings.extend(self._check_url_structure(url))
        findings.extend(self._check_meta_robots(soup, url))
        findings.extend(self._check_favicon(soup, url))
        findings.extend(self._check_viewport(soup, url))
        findings.extend(self._check_internal_nofollow(soup, url))
        findings.extend(self._check_keyword_alignment(soup, url))
        findings.extend(self._check_breadcrumb_schema(soup, url))
        findings.extend(self._check_canonical_mismatch(soup, url))
        findings.extend(self._check_duplicate_meta(soup, url))
        findings.extend(self._check_mobile_font_sizes(soup, url))

        return findings

    def scan_audit(
        self,
        html: str,
        url: str = "",
        *,
        soup: BeautifulSoup | None = None,
        include_llm: bool = False,
    ) -> list[dict]:
        """Full audit-grade SEO scan: the base checks **plus** the audit groups.

        Runs everything :meth:`scan` runs (so on-page/technical/schema signals are
        included) and adds the deterministic audit groups (content/keyword/snippet;
        more land in later phases). ``include_llm`` is accepted for wire stability;
        Tier-3 micro-judgments are layered in a later phase and never block the
        deterministic result. The base :meth:`scan` is left untouched so existing
        critique consumers keep identical ``seo`` scores.
        """
        if soup is None:
            soup = BeautifulSoup(html, "lxml")

        findings = self.scan(html, url, soup=soup)
        findings.extend(readability.check_readability(soup, url))
        findings.extend(keyword.check_keyword(soup, url))
        findings.extend(snippet.check_snippet(soup, url))
        findings.extend(eeat.check_eeat(soup, url))
        findings.extend(schema_coverage.check_schema_coverage(soup, url))
        findings.extend(internal_linking.check_internal_linking(soup, url))
        findings.extend(geo.check_geo(soup, url))
        return findings

    async def scan_audit_live(
        self, url: str, *, observe: ProbeObservation | None = None
    ) -> list[dict]:
        """Network-dependent GEO audit checks (robots.txt AI-crawler access, llms.txt).

        Called best-effort by the synchronous ``/api/seo/audit`` path for a URL
        input. ``observe`` (optional) is filled with what was fetched — see
        :func:`geo.scan_geo_live`.
        """
        return await geo.scan_geo_live(url, observe=observe)

    # ------------------------------------------------------------------
    # Per-check delegators (kept for backward-compatible monkey-patching).
    # ------------------------------------------------------------------

    def _check_title(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return on_page.check_title(soup, url)

    def _check_meta_description(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return on_page.check_meta_description(soup, url)

    def _check_og_tags(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return social.check_og_tags(soup, url)

    def _check_heading_hierarchy(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return on_page.check_heading_hierarchy(soup, url)

    def _check_canonical(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return structural.check_canonical(soup, url)

    def _check_image_alt(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return on_page.check_image_alt(soup, url)

    def _check_structured_data(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return structural.check_structured_data(soup, url)

    def _check_twitter_cards(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return social.check_twitter_cards(soup, url)

    def _check_hreflang(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return technical.check_hreflang(soup, url)

    def _check_meta_robots(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return technical.check_meta_robots(soup, url)

    def _check_favicon(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return technical.check_favicon(soup, url)

    def _check_viewport(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return mobile.check_viewport(soup, url)

    def _check_internal_nofollow(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return structural.check_internal_nofollow(soup, url)

    def _check_keyword_alignment(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return on_page.check_keyword_alignment(soup, url)

    def _check_breadcrumb_schema(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return structural.check_breadcrumb_schema(soup, url)

    def _check_canonical_mismatch(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return structural.check_canonical_mismatch(soup, url)

    def _check_duplicate_meta(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return on_page.check_duplicate_meta(soup, url)

    def _check_mobile_font_sizes(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return mobile.check_mobile_font_sizes(soup, url)

    def _check_url_structure(self, url: str) -> list[dict]:
        return structural.check_url_structure(url)

    async def scan_live(self, url: str, *, observe: ProbeObservation | None = None) -> list[dict]:
        """robots.txt / sitemap.xml checks — see :func:`technical.scan_live`."""
        return await technical.scan_live(url, observe=observe)
