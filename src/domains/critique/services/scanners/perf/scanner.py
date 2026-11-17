"""Performance scanner for website critique.

Checks: render-blocking scripts, viewport meta, image dimensions,
lazy loading, DOM size, excessive CSS, inline styles, excessive scripts,
page weight, resource hints, font loading, web vitals hints, critical CSS,
modern image formats, third-party scripts, CSS duplication,
render-blocking CSS, DOM depth.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.perf import cls, dom, inp, lcp, resources

logger = logging.getLogger(__name__)


class PerformanceScanner:
    """Scans HTML for performance issues."""

    def scan(self, html: str, url: str = "", *, soup: BeautifulSoup | None = None) -> list[dict]:
        """Scan HTML for performance issues."""
        findings: list[dict] = []
        if soup is None:
            soup = BeautifulSoup(html, "lxml")

        findings.extend(self._check_render_blocking(soup, url))
        findings.extend(self._check_viewport(soup, url))
        findings.extend(self._check_image_dimensions(soup, url))
        findings.extend(self._check_lazy_loading(soup, url))
        findings.extend(self._check_dom_size(soup, url))
        findings.extend(self._check_css(soup, url))
        findings.extend(self._check_inline_styles(soup, url))
        findings.extend(self._check_scripts(soup, url))
        findings.extend(self._check_page_weight(soup, html, url))
        findings.extend(self._check_resource_hints(soup, url))
        findings.extend(self._check_font_loading(soup, url))
        findings.extend(self._check_web_vitals_hints(soup, url))
        findings.extend(self._check_critical_css(soup, url))
        findings.extend(self._check_image_formats(soup, url))
        findings.extend(self._check_third_party_scripts(soup, url))
        findings.extend(self._check_css_duplication(soup, url))
        findings.extend(self._check_render_blocking_css(soup, url))
        findings.extend(self._check_dom_depth(soup, url))
        findings.extend(self._check_large_svg(soup, url))
        findings.extend(self._check_modulepreload(soup, url))
        findings.extend(self._check_sync_xhr(soup, url))
        findings.extend(self._check_document_write(soup, url))
        findings.extend(self._check_css_import(soup, url))
        findings.extend(self._check_content_visibility(soup, url))

        return findings

    # ------------------------------------------------------------------
    # Per-check delegators (kept for backward-compatible monkey-patching).
    # ------------------------------------------------------------------

    def _check_render_blocking(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_render_blocking(soup, url)

    def _check_viewport(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return cls.check_viewport(soup, url)

    def _check_image_dimensions(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return cls.check_image_dimensions(soup, url)

    def _check_lazy_loading(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_lazy_loading(soup, url)

    def _check_dom_size(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return dom.check_dom_size(soup, url)

    def _check_css(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return resources.check_css(soup, url)

    def _check_inline_styles(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return resources.check_inline_styles(soup, url)

    def _check_scripts(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return inp.check_scripts(soup, url)

    def _check_page_weight(self, soup: BeautifulSoup, html: str, url: str) -> list[dict]:
        return resources.check_page_weight(soup, html, url)

    def _check_resource_hints(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_resource_hints(soup, url)

    def _check_font_loading(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_font_loading(soup, url)

    def _check_web_vitals_hints(self, soup: BeautifulSoup, url: str) -> list[dict]:
        findings: list[dict] = []
        findings.extend(lcp.check_web_vitals_hints(soup, url))
        findings.extend(cls.iframe_dimension_findings(soup, url))
        return findings

    def _check_critical_css(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_critical_css(soup, url)

    def _check_image_formats(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_image_formats(soup, url)

    def _check_third_party_scripts(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return inp.check_third_party_scripts(soup, url)

    def _check_css_duplication(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return resources.check_css_duplication(soup, url)

    def _check_render_blocking_css(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return resources.check_render_blocking_css(soup, url)

    def _check_dom_depth(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return dom.check_dom_depth(soup, url)

    def _check_large_svg(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return resources.check_large_svg(soup, url)

    def _check_modulepreload(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return lcp.check_modulepreload(soup, url)

    def _check_sync_xhr(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return inp.check_sync_xhr(soup, url)

    def _check_document_write(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return inp.check_document_write(soup, url)

    def _check_css_import(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return resources.check_css_import(soup, url)

    def _check_content_visibility(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return cls.check_content_visibility(soup, url)
