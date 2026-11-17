"""Detect SPA-like pages that share content with the homepage."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _collect_homepage_signals(pages: list[dict]) -> tuple[set[str], set[str]]:
    """Return (titles, page_ids) of all pages classified as ``homepage``."""
    titles: set[str] = set()
    ids: set[str] = set()
    for p in pages:
        if p.get("page_type") != "homepage":
            continue
        title = p.get("title", "")
        if title:
            titles.add(title)
        pid = p.get("page_id", "")
        if pid:
            ids.add(pid)
    return titles, ids


def detect_spa_pages(pages: list[dict]) -> set[str]:
    """Return page_ids of pages that appear to be sections of a single-page application.

    Heuristics:
    - **Same title**: Multiple pages sharing an identical ``<title>`` with
      the homepage strongly suggests an SPA where the ``<title>`` tag is
      never updated per-route.

    Only non-homepage pages are flagged; the homepage itself is kept as-is.
    """
    if not pages:
        return set()

    homepage_titles, homepage_ids = _collect_homepage_signals(pages)
    if not homepage_titles:
        return set()

    spa_page_ids: set[str] = set()
    for p in pages:
        pid = p.get("page_id", "")
        if not pid or pid in homepage_ids:
            continue
        title = p.get("title", "")
        if title and title in homepage_titles:
            spa_page_ids.add(pid)

    if spa_page_ids:
        logger.info(
            "SPA detection: %d page(s) share the homepage title — flagging as SPA sections: %s",
            len(spa_page_ids),
            ", ".join(sorted(spa_page_ids)),
        )

    return spa_page_ids
