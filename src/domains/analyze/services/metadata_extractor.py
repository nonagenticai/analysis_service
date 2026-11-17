"""Extract rich metadata from HTML pages (OG tags, JSON-LD, canonical, language, robots)."""

from __future__ import annotations

import json
import logging

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class MetadataExtractor:
    """Extract rich metadata from parsed HTML."""

    @staticmethod
    def extract(soup: BeautifulSoup) -> dict:
        """Extract all metadata from a parsed HTML page."""
        return {
            "og_tags": MetadataExtractor._extract_og_tags(soup),
            "json_ld": MetadataExtractor._extract_json_ld(soup),
            "canonical_url": MetadataExtractor._extract_canonical(soup),
            "language": MetadataExtractor._extract_language(soup),
            "robots": MetadataExtractor._extract_robots(soup),
        }

    @staticmethod
    def _extract_og_tags(soup: BeautifulSoup) -> dict:
        """Extract Open Graph meta tags."""
        og_tags = {}
        for meta in soup.find_all("meta", attrs={"property": True}):
            prop = str(meta.get("property", ""))
            if prop.startswith("og:"):
                key = prop[3:]  # strip "og:" prefix
                og_tags[key] = str(meta.get("content", ""))
        return og_tags

    @staticmethod
    def _extract_json_ld(soup: BeautifulSoup) -> list[dict]:
        """Extract JSON-LD structured data."""
        results = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                text = script.string or ""
                if text.strip():
                    data = json.loads(text)
                    if isinstance(data, dict):
                        results.append(data)
                    elif isinstance(data, list):
                        results.extend(d for d in data if isinstance(d, dict))
            except json.JSONDecodeError, TypeError:
                logger.debug("Failed to parse JSON-LD block")
        return results

    @staticmethod
    def _extract_canonical(soup: BeautifulSoup) -> str:
        """Extract canonical URL from link rel=canonical."""
        link = soup.find("link", attrs={"rel": "canonical"})
        if link and link.get("href"):
            return str(link["href"])
        return ""

    @staticmethod
    def _extract_language(soup: BeautifulSoup) -> str:
        """Extract language from html lang attribute."""
        html_tag = soup.find("html")
        if html_tag and html_tag.get("lang"):
            return str(html_tag["lang"])
        return ""

    @staticmethod
    def _extract_robots(soup: BeautifulSoup) -> dict:
        """Extract robots meta directives."""
        meta = soup.find("meta", attrs={"name": "robots"})
        content = str(meta.get("content", "")).lower() if meta else ""
        return {
            "noindex": "noindex" in content,
            "nofollow": "nofollow" in content,
        }
