"""Extract brand information from analysis data."""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class BrandExtractor:
    """Extract brand identity from crawled pages and site analysis."""

    @staticmethod
    def extract_brand(
        pages: list[dict],
        site_analysis: dict | None = None,
    ) -> dict:
        """Extract brand info from pages and site analysis."""
        domain = BrandExtractor._extract_domain(pages)
        name = BrandExtractor._extract_name(pages, site_analysis) or domain
        logo_url = BrandExtractor._extract_logo_url(pages)
        primary, secondary = BrandExtractor._extract_colors(site_analysis)

        return {
            "name": name,
            "domain": domain,
            "logo_url": logo_url,
            "primary_color": primary,
            "secondary_color": secondary,
        }

    @staticmethod
    def _extract_domain(pages: list[dict]) -> str:
        """Extract domain from first page URL."""
        if not pages:
            return ""
        url = pages[0].get("url", "")
        if not url:
            return ""
        parsed = urlparse(url)
        return parsed.netloc.removeprefix("www.")

    @staticmethod
    def _extract_name(
        pages: list[dict],
        site_analysis: dict | None = None,
    ) -> str:
        """Extract site name from analysis or page titles."""
        # Try site description if short enough
        if site_analysis:
            desc = site_analysis.get("description", "")
            if desc and len(str(desc)) < 60:
                return str(desc).strip()

        # Try first page title
        if pages:
            title = pages[0].get("title", "")
            if title:
                cleaned = re.split(
                    r"\s*[|-—-]\s*(?:Home|Welcome|Homepage).*$",
                    str(title),
                    flags=re.IGNORECASE,
                )
                return cleaned[0].strip() if cleaned else str(title).strip()
        return ""

    @staticmethod
    def _extract_logo_url(pages: list[dict]) -> str:
        """Extract logo URL from page metadata."""
        for page in pages:
            # Check logo_candidates from page analyzer
            candidates = page.get("logo_candidates", [])
            if candidates:
                return candidates[0]

            # Check metadata og:image
            metadata = page.get("metadata", {})
            if isinstance(metadata, dict):
                og_tags = metadata.get("og_tags", {})
                if isinstance(og_tags, dict) and og_tags.get("image"):
                    return str(og_tags["image"])
        return ""

    @staticmethod
    def _extract_colors(
        site_analysis: dict | None = None,
    ) -> tuple[str, str]:
        """Extract brand colors from site visual_identity."""
        if not site_analysis:
            return "", ""
        vi = site_analysis.get("visual_identity", {})
        if not isinstance(vi, dict):
            return "", ""
        for key in ("primary_color", "primary", "brand_color", "color"):
            val = vi.get(key)
            if val:
                color = str(val).strip()
                if re.match(r"^#[0-9a-fA-F]{3,8}$", color):
                    return color, BrandExtractor._darken_hex(color)
        return "", ""

    @staticmethod
    def _darken_hex(hex_color: str) -> str:
        """Darken a hex color by 20% for secondary color."""
        hex_color = hex_color.lstrip("#")
        if len(hex_color) == 3:
            hex_color = "".join(c * 2 for c in hex_color)
        try:
            r, g, b = (
                int(hex_color[0:2], 16),
                int(hex_color[2:4], 16),
                int(hex_color[4:6], 16),
            )
            factor = 0.8
            r, g, b = int(r * factor), int(g * factor), int(b * factor)
            return f"#{r:02x}{g:02x}{b:02x}"
        except ValueError, IndexError:
            return hex_color
