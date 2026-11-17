"""Accessibility (WCAG) scanner orchestrator."""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from src.domains.critique.services.scanners.a11y import (
    operable,
    perceivable,
    robust,
    understandable,
)

logger = logging.getLogger(__name__)


class A11yScanner:
    """Scans HTML for WCAG accessibility issues."""

    def scan(self, html: str, url: str = "", *, soup: BeautifulSoup | None = None) -> list[dict]:
        """Scan HTML for accessibility issues."""
        findings: list[dict] = []
        if soup is None:
            soup = BeautifulSoup(html, "lxml")

        findings.extend(self._check_lang(soup, url))
        findings.extend(self._check_alt_text(soup, url))
        findings.extend(self._check_form_labels(soup, url))
        findings.extend(self._check_skip_links(soup, url))
        findings.extend(self._check_heading_order(soup, url))
        findings.extend(self._check_aria_roles(soup, url))
        findings.extend(self._check_tabindex(soup, url))
        findings.extend(self._check_landmarks(soup, url))
        findings.extend(self._check_link_text(soup, url))
        # Color contrast is checked by axe-core (computed styles in real browser).
        findings.extend(self._check_video_captions(soup, url))
        findings.extend(self._check_button_names(soup, url))
        findings.extend(self._check_table_headers(soup, url))
        findings.extend(self._check_touch_targets(soup, url))
        findings.extend(self._check_keyboard_navigation(soup, url))
        findings.extend(self._check_autocomplete(soup, url))
        findings.extend(self._check_focus_trap(soup, url))
        findings.extend(self._check_empty_th(soup, url))
        findings.extend(self._check_iframe_title(soup, url))
        findings.extend(self._check_deprecated_motion(soup, url))
        findings.extend(self._check_lang_validity(soup, url))
        findings.extend(self._check_svg_accessibility(soup, url))
        findings.extend(self._check_aria_hidden_focusable(soup, url))
        findings.extend(self._check_aria_live(soup, url))

        return findings

    # ------------------------------------------------------------------
    # Per-check delegators (kept for backward-compatible monkey-patching).
    # ------------------------------------------------------------------

    def _check_lang(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return understandable.check_lang(soup, url)

    def _check_alt_text(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return perceivable.check_alt_text(soup, url)

    def _check_form_labels(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return understandable.check_form_labels(soup, url)

    def _check_skip_links(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return operable.check_skip_links(soup, url)

    def _check_heading_order(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return understandable.check_heading_order(soup, url)

    def _check_aria_roles(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_aria_roles(soup, url)

    def _check_tabindex(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return operable.check_tabindex(soup, url)

    def _check_landmarks(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_landmarks(soup, url)

    def _check_link_text(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return understandable.check_link_text(soup, url)

    def _check_video_captions(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return perceivable.check_video_captions(soup, url)

    def _check_button_names(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_button_names(soup, url)

    def _check_table_headers(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return perceivable.check_table_headers(soup, url)

    def _check_touch_targets(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return operable.check_touch_targets(soup, url)

    def _check_keyboard_navigation(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return operable.check_keyboard_navigation(soup, url)

    def _check_autocomplete(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return understandable.check_autocomplete(soup, url)

    def _check_focus_trap(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return operable.check_focus_trap(soup, url)

    def _check_empty_th(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_empty_th(soup, url)

    def _check_iframe_title(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_iframe_title(soup, url)

    def _check_deprecated_motion(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return perceivable.check_deprecated_motion(soup, url)

    def _check_lang_validity(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return understandable.check_lang_validity(soup, url)

    def _check_svg_accessibility(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return perceivable.check_svg_accessibility(soup, url)

    def _check_aria_hidden_focusable(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_aria_hidden_focusable(soup, url)

    def _check_aria_live(self, soup: BeautifulSoup, url: str) -> list[dict]:
        return robust.check_aria_live(soup, url)
