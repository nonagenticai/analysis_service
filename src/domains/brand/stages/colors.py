"""ColorStage: consolidate all color extraction (meta tags + inline CSS + external CSS)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.color_service import darken_hex
from src.domains.brand.services.css_parser import CSSBrandParser, extract_background_color_from_html

# Single canonical CSS variable pattern for inline CSS extraction
_COLOR_VAR_PATTERN = re.compile(
    r"--(?:primary|brand|main|accent|theme)[-_]?color\s*:\s*(#[0-9a-fA-F]{3,8})",
    re.IGNORECASE,
)


class ColorStage(BaseStage):
    """Extract brand colors from meta tags, inline CSS, and external CSS."""

    name = "colors"

    def __init__(self) -> None:
        super().__init__()
        self._css_parser = CSSBrandParser()

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.soup is not None

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("ColorStage.run() requires ctx.soup (should_run guards this)")

        # Track background color for WCAG
        ctx.background_color = extract_background_color_from_html(soup) or "#ffffff"

        # Extract colors from meta tags + inline CSS
        primary, secondary = _extract_colors_from_html(soup)
        if primary:
            brand.primary_color = primary
            ctx.tracker.record("html_meta", "primary_color", primary)
        if secondary:
            brand.secondary_color = secondary

        # Extract CSS URLs for external stylesheet parsing
        ctx.css_urls = _extract_css_urls(soup, ctx.url)
        css_urls = ctx.css_urls[: ctx.config.max_css_stylesheets]

        # External CSS colors
        if css_urls and ctx.config.should_run("css_parser"):
            try:
                css_colors, _, css_bg = await self._css_parser.extract_from_url(css_urls, ctx.url)
                if css_bg and ctx.background_color == "#ffffff":
                    ctx.background_color = css_bg
                if not primary and css_colors:
                    brand.primary_color = css_colors[0]
                    brand.secondary_color = darken_hex(css_colors[0])
                    ctx.tracker.record("css_parser", "primary_color", css_colors[0])
            except (ValueError, TypeError, KeyError) as e:
                result.add_warning(f"CSS color extraction failed: {e}")

        # Manifest theme color (if we don't have a primary yet)
        if (
            not primary
            and brand.manifest_theme_color
            and brand.manifest_theme_color.startswith("#")
        ):
            brand.primary_color = brand.manifest_theme_color
            brand.secondary_color = darken_hex(brand.manifest_theme_color)


def _extract_colors_from_html(soup: BeautifulSoup) -> tuple[str, str]:
    """Extract brand colors from inline CSS and meta tags."""
    # theme-color meta tag
    theme = soup.find("meta", attrs={"name": "theme-color"})
    if theme and isinstance(theme, Tag):
        content = theme.get("content", "")
        color = (
            str(content).strip()
            if isinstance(content, str)
            else str(content[0]).strip()
            if content
            else ""
        )
        if color and re.match(r"^#[0-9a-fA-F]{3,8}$", color):
            return color, darken_hex(color)

    # msapplication-TileColor
    tile = soup.find("meta", attrs={"name": "msapplication-TileColor"})
    if tile and isinstance(tile, Tag):
        content = tile.get("content", "")
        color = (
            str(content).strip()
            if isinstance(content, str)
            else str(content[0]).strip()
            if content
            else ""
        )
        if color and re.match(r"^#[0-9a-fA-F]{3,8}$", color):
            return color, darken_hex(color)

    # CSS custom properties for brand colors
    for style in soup.find_all("style"):
        css = style.string or ""
        matches = _COLOR_VAR_PATTERN.findall(css)
        if matches:
            return matches[0], darken_hex(matches[0])

    return "", ""


def _extract_css_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    """Extract external CSS stylesheet URLs from HTML."""
    urls: list[str] = []
    for link in soup.find_all("link", rel="stylesheet"):
        if not isinstance(link, Tag):
            continue
        href = link.get("href", "")
        if not href:
            continue
        href_str = str(href) if isinstance(href, str) else str(href[0])
        if href_str.startswith("//"):
            href_str = f"https:{href_str}"
        elif href_str.startswith("/"):
            parsed = urlparse(base_url)
            href_str = f"{parsed.scheme}://{parsed.netloc}{href_str}"
        elif not href_str.startswith("http"):
            continue
        # Skip Google Fonts CSS (handled separately by font extraction)
        if "fonts.googleapis.com" in href_str:
            continue
        urls.append(href_str)
    return urls[:10]
