"""HTML metadata extractor.

Extracts favicons, social images, schema.org data, web manifest info,
and font references from HTML documents.
"""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from src.domains.brand.schemas import FontInfo, SchemaOrgData, SocialImages
from src.domains.brand.security import (
    BrandResponseTooLargeError,
    BrandSsrfBlockedError,
    get_guarded,
)
from src.domains.brand.services.css_parser import _categorize_font

logger = logging.getLogger(__name__)

# Byte cap for a web app manifest. A manifest is a small JSON document (a name,
# a theme colour, a handful of icon entries); the spec has no size at all in
# mind. 1 MB is orders of magnitude above anything real and still stops a
# ``<link rel=manifest>`` pointed at an arbitrarily large body from being
# buffered into the pod.
_MAX_MANIFEST_BYTES = 1024 * 1024

# Google Fonts URL pattern
_GOOGLE_FONTS_URL = re.compile(r"fonts\.googleapis\.com", re.IGNORECASE)
# Full pattern including weight specs (e.g. family=Roboto:wght@400;700)
_GOOGLE_FONTS_FAMILY_FULL = re.compile(r"family=([^&]+)", re.IGNORECASE)


class MetadataExtractor:
    """Extracts rich metadata from parsed HTML."""

    def extract_favicons(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """Extract favicon URLs from HTML, sorted by size (largest first).

        Looks for:
        - <link rel="icon">
        - <link rel="shortcut icon">
        - <link rel="apple-touch-icon">
        """
        favicons: list[tuple[int, str]] = []  # (size, url)

        for link in soup.find_all("link", rel=True):
            if not isinstance(link, Tag):
                continue
            rel = link.get("rel") or []
            rel_str = " ".join(rel).lower() if isinstance(rel, list) else str(rel).lower()

            if not any(kw in rel_str for kw in ("icon", "apple-touch-icon", "shortcut")):
                continue

            href = link.get("href", "")
            if not href:
                continue
            href_str = str(href) if isinstance(href, str) else str(href[0])
            url = urljoin(base_url, href_str)

            # Try to parse size from sizes attribute
            sizes = link.get("sizes", "")
            size = 0
            if sizes:
                sizes_str = str(sizes) if isinstance(sizes, str) else str(sizes[0])
                size_match = re.search(r"(\d+)x(\d+)", sizes_str)
                if size_match:
                    size = int(size_match.group(1))

            favicons.append((size, url))

        # Sort by size descending (largest first), with 0 (unknown) at end
        favicons.sort(key=lambda x: (x[0] == 0, -x[0]))
        return [url for _, url in favicons]

    def extract_social_images(self, soup: BeautifulSoup) -> SocialImages:
        """Extract social media image URLs (OG, Twitter, Apple Touch Icon)."""
        og_image = ""
        twitter_image = ""
        apple_touch_icon = ""

        # og:image
        og_tag = soup.find("meta", attrs={"property": "og:image"})
        if og_tag and isinstance(og_tag, Tag):
            content = og_tag.get("content", "")
            if content:
                og_image = str(content) if isinstance(content, str) else str(content[0])

        # twitter:image
        twitter_tag = soup.find("meta", attrs={"name": "twitter:image"})
        if not twitter_tag:
            twitter_tag = soup.find("meta", attrs={"property": "twitter:image"})
        if twitter_tag and isinstance(twitter_tag, Tag):
            content = twitter_tag.get("content", "")
            if content:
                twitter_image = str(content) if isinstance(content, str) else str(content[0])

        # apple-touch-icon
        apple_tag = soup.find("link", rel="apple-touch-icon")
        if apple_tag and isinstance(apple_tag, Tag):
            href = apple_tag.get("href", "")
            if href:
                apple_touch_icon = str(href) if isinstance(href, str) else str(href[0])

        return SocialImages(
            og_image=og_image,
            twitter_image=twitter_image,
            apple_touch_icon=apple_touch_icon,
        )

    @staticmethod
    def _ld_items(data: object) -> list[dict]:
        """Normalize a parsed JSON-LD payload into a list of dict items."""
        if isinstance(data, dict):
            data_dict: dict = dict(data)
            graph = data_dict.get("@graph")
            if isinstance(graph, list):
                return [i for i in graph if isinstance(i, dict)]
            return [data_dict]
        if isinstance(data, list):
            return [i for i in data if isinstance(i, dict)]
        return []

    @staticmethod
    def _schema_from_item(item: dict) -> SchemaOrgData | None:
        schema_type = item.get("@type", "")
        if isinstance(schema_type, list):
            schema_type = schema_type[0] if schema_type else ""
        if schema_type not in ("Organization", "LocalBusiness", "WebSite", "Corporation"):
            return None

        same_as = item.get("sameAs", [])
        if isinstance(same_as, str):
            same_as = [same_as]

        logo = item.get("logo", "")
        if isinstance(logo, dict):
            logo = logo.get("url", "")

        return SchemaOrgData(
            type=schema_type,
            name=str(item.get("name", "")),
            url=str(item.get("url", "")),
            logo=str(logo),
            description=str(item.get("description", "")),
            same_as=[str(s) for s in same_as if s],
        )

    def extract_schema_org(self, soup: BeautifulSoup) -> SchemaOrgData | None:
        """Extract schema.org structured data from JSON-LD scripts.

        Looks for @type: Organization, LocalBusiness, WebSite.
        """
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                text = script.string
                if not text:
                    continue
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.debug("Failed to parse schema.org JSON-LD: %s", e)
                continue
            for item in self._ld_items(data):
                schema = self._schema_from_item(item)
                if schema is not None:
                    return schema
        return None

    async def extract_manifest(self, soup: BeautifulSoup, base_url: str) -> tuple[str, list[str]]:
        """Extract web manifest theme_color and icon URLs.

        Every failure mode here is fail-soft (``("", [])``): the manifest is one
        optional signal among a dozen the brand pipeline collects, and a site
        that serves a broken one must not take the extraction down with it. Two
        of those modes used to escape instead of degrading, because neither is
        an ``OSError`` or an ``httpx.HTTPError``:

        * a body that is not JSON at all -- ``response.json()`` raises
          ``json.JSONDecodeError``, a ``ValueError``;
        * a manifest that *is* valid JSON but not an object (a bare list is the
          realistic one; a string or a number does it too) -- ``data.get``
          raises ``AttributeError``, which nothing up the stack catches either,
          so the whole stage died on a one-line file.

        Returns:
            Tuple of (theme_color, icon_urls); ``("", [])`` on any failure.
        """
        manifest_link = soup.find("link", rel="manifest")
        if not manifest_link or not isinstance(manifest_link, Tag):
            return "", []

        href = manifest_link.get("href", "")
        if not href:
            return "", []

        href_str = str(href) if isinstance(href, str) else str(href[0])
        manifest_url = urljoin(base_url, href_str)

        fetch_start = time.monotonic()
        try:
            # SSRF-gated per hop: ``manifest_url`` is a ``<link rel=manifest>``
            # href resolved against a tenant-chosen page, so it can point
            # anywhere; redirects were previously auto-followed.
            async with httpx.AsyncClient(
                timeout=3.0,
                follow_redirects=False,
                headers={"User-Agent": "BrandExtractorService/1.0"},
            ) as client:
                response = await get_guarded(client, manifest_url, max_bytes=_MAX_MANIFEST_BYTES)
                response.raise_for_status()
                data = response.json()
            fetch_ms = int((time.monotonic() - fetch_start) * 1000)
            logger.debug("Manifest fetched, url=%s, duration_ms=%d", manifest_url, fetch_ms)
        except BrandSsrfBlockedError as e:
            logger.warning("Blocked manifest fetch: %s", e)
            return "", []
        except BrandResponseTooLargeError as e:
            logger.warning("Oversized manifest fetch from %s: %s", manifest_url, e)
            return "", []
        except (OSError, httpx.HTTPError, ValueError) as e:
            # ``ValueError`` is listed explicitly, not left to be caught by
            # accident: ``json.JSONDecodeError`` subclasses it, and relying on
            # that hierarchy is how a non-JSON manifest looked handled while
            # being one refactor away from escaping.
            fetch_ms = int((time.monotonic() - fetch_start) * 1000)
            logger.debug(
                "Failed to fetch manifest, url=%s, duration_ms=%d: %s", manifest_url, fetch_ms, e
            )
            return "", []

        if not isinstance(data, dict):
            logger.debug(
                "Manifest at %s is %s, not a JSON object -- ignoring",
                manifest_url,
                type(data).__name__,
            )
            return "", []

        theme_color = str(data.get("theme_color", ""))

        icon_urls: list[str] = []
        for icon in data.get("icons", []):
            if isinstance(icon, dict):
                src = icon.get("src", "")
                if src:
                    icon_urls.append(urljoin(manifest_url, str(src)))

        return theme_color, icon_urls

    _GENERIC_FONTS = frozenset(
        {
            "serif",
            "sans-serif",
            "monospace",
            "cursive",
            "fantasy",
            "system-ui",
            "inherit",
            "initial",
            "unset",
        }
    )

    @staticmethod
    def _parse_google_family(raw: str) -> tuple[str, list[str]]:
        if ":" not in raw:
            return raw, []
        name_part, spec = raw.split(":", 1)
        weight_match = re.search(r"(?:wght@)?(\d[\d;]+)", spec)
        weights = (
            [w for w in weight_match.group(1).split(";") if w.isdigit()] if weight_match else []
        )
        return name_part.strip(), weights

    def _google_fonts_from_links(
        self, soup: BeautifulSoup, fonts: list[FontInfo], seen: set[str]
    ) -> None:
        for link in soup.find_all("link"):
            if not isinstance(link, Tag):
                continue
            href = link.get("href", "")
            href_str = str(href) if isinstance(href, str) else str(href[0]) if href else ""
            if not _GOOGLE_FONTS_URL.search(href_str):
                continue
            for match in _GOOGLE_FONTS_FAMILY_FULL.finditer(href_str):
                raw = match.group(1).replace("+", " ").strip()
                family, weights = self._parse_google_family(raw)
                if family and family.lower() not in seen:
                    seen.add(family.lower())
                    fonts.append(
                        FontInfo(
                            family=family,
                            category=_categorize_font(family),
                            role="",
                            weights=weights,
                            is_google_font=True,
                            source_url=href_str,
                        )
                    )

    def _fonts_from_inline_styles(
        self, soup: BeautifulSoup, fonts: list[FontInfo], seen: set[str]
    ) -> None:
        for style in soup.find_all("style"):
            css = style.string or ""
            for match in re.finditer(r"font-family\s*:\s*([^;}{]+)", css, re.IGNORECASE):
                for part in match.group(1).split(","):
                    family = part.strip().strip("'\"").strip()
                    if not family or family.lower() in self._GENERIC_FONTS:
                        continue
                    if family.lower() not in seen:
                        seen.add(family.lower())
                        fonts.append(
                            FontInfo(
                                family=family,
                                category=_categorize_font(family),
                                role="",
                                is_google_font=False,
                            )
                        )
                    break  # Only first non-generic font

    def extract_fonts_from_html(self, soup: BeautifulSoup) -> list[FontInfo]:
        """Extract font references from HTML.

        Looks for:
        - Google Fonts <link> tags
        - font-family in inline <style> blocks
        """
        fonts: list[FontInfo] = []
        seen_families: set[str] = set()
        self._google_fonts_from_links(soup, fonts, seen_families)
        self._fonts_from_inline_styles(soup, fonts, seen_families)
        return fonts
