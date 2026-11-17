"""Detect and deduplicate language-variant pages (e.g. /en/, /de/, /fr/)."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Matches a language prefix like /en/, /de/, /fr-FR/, /pt-BR/ at the start of a path.
_LANG_PREFIX_RE = re.compile(r"^/([a-z]{2})(?:-[A-Za-z]{2})?(?:/|$)")


def _extract_lang_and_base(path: str) -> tuple[str | None, str]:
    """Extract the language prefix and base path from a URL path.

    Returns (lang, base_path) where lang is None if no language prefix is found.
    The base path has the language segment removed so variants can be grouped.

    Examples:
        "/en/products" -> ("en", "/products")
        "/de/products" -> ("de", "/products")
        "/en/"         -> ("en", "/")
        "/"            -> (None, "/")
        "/products"    -> (None, "/products")
    """
    m = _LANG_PREFIX_RE.match(path)
    if m:
        lang = m.group(1)
        rest = path[m.end() :]
        base = "/" + rest.lstrip("/") if rest else "/"
        return lang, base
    return None, path


def detect_language_variants(pages: list[dict], keep_lang: str = "en") -> set[str]:
    """Return page_ids of pages that are language duplicates of other pages.

    Groups pages by their base path (URL path with language prefix removed).
    Within each group containing multiple language variants, keeps only the
    page matching *keep_lang* (or the first page if no exact match) and flags
    the rest for removal.
    """
    if not pages:
        return set()

    groups: dict[str, list[tuple[str | None, dict]]] = defaultdict(list)
    for page in pages:
        path = urlparse(page.get("url", "")).path or "/"
        lang, base = _extract_lang_and_base(path)
        groups[base].append((lang, page))

    duplicate_ids: set[str] = set()

    for base_path, members in groups.items():
        langs_present = [lang for lang, _ in members if lang is not None]
        if len(members) < 2 or not langs_present:
            continue

        keeper: dict = members[0][1]
        for lang, page in members:
            if lang == keep_lang:
                keeper = page
                break

        for _, page in members:
            if page.get("page_id") != keeper.get("page_id"):
                pid = page.get("page_id", "")
                if pid:
                    duplicate_ids.add(pid)

        logger.info(
            "Language dedup: base path %r has %d variant(s) — keeping %s (%s), "
            "flagging %d duplicate(s)",
            base_path,
            len(members),
            keeper.get("page_id"),
            keeper.get("url"),
            len(members) - 1,
        )

    return duplicate_ids
