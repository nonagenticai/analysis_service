"""LogoStage: extract logo URL from HTML + logo_intelligence."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from bs4 import Tag

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.logo_intelligence import LogoIntelligenceExtractor


class LogoStage(BaseStage):
    """Extract logo URL from HTML and logo intelligence."""

    name = "logo"

    def __init__(self) -> None:
        super().__init__()
        self._logo_intelligence = LogoIntelligenceExtractor()

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.soup is not None

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("LogoStage.run() requires ctx.soup (should_run guards this)")

        # Basic logo extraction from HTML
        brand.logo_url = self._extract_logo_from_html(soup, ctx.url)
        brand.has_logo = bool(brand.logo_url)
        if brand.logo_url:
            ctx.tracker.record("html_meta", "logo_url", brand.logo_url)

        # Logo intelligence (advanced detection)
        if ctx.config.should_run("logo_intelligence"):
            try:
                logo_intel = self._logo_intelligence.extract(soup, ctx.url, ctx.schema_org_dict)
                brand.logo_intelligence = logo_intel.to_dict()
                if logo_intel.primary_logo_url and not brand.logo_url:
                    brand.logo_url = logo_intel.primary_logo_url
                    brand.has_logo = True
                    ctx.tracker.record("logo_intelligence", "logo_url", logo_intel.primary_logo_url)
            except (ValueError, TypeError, KeyError, OSError) as e:
                result.add_warning(f"Logo intelligence failed: {e}")

    @staticmethod
    def _extract_logo_from_html(soup: Any, base_url: str) -> str:
        """Extract logo URL from HTML (og:image, favicon, img with 'logo')."""
        # og:image
        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and isinstance(og_image, Tag):
            content = og_image.get("content", "")
            if content:
                return str(content) if isinstance(content, str) else str(content[0])

        # favicon
        favicon = soup.find("link", attrs={"rel": re.compile(r"icon", re.I)})
        if favicon and isinstance(favicon, Tag):
            href = favicon.get("href", "")
            if href:
                href_str = str(href) if isinstance(href, str) else str(href[0])
                if href_str.startswith("/"):
                    parsed = urlparse(base_url)
                    return f"{parsed.scheme}://{parsed.netloc}{href_str}"
                return href_str

        # img with "logo" in class/id/alt
        for img in soup.find_all("img"):
            if not isinstance(img, Tag):
                continue
            attrs = str(img.get("class", "")) + str(img.get("id", "")) + str(img.get("alt", ""))
            if "logo" in attrs.lower():
                src = img.get("src", "")
                if src:
                    return str(src) if isinstance(src, str) else str(src[0])

        return ""
