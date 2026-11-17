"""FontStage: consolidate font extraction from HTML + external CSS."""

from __future__ import annotations

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.services.css_parser import CSSBrandParser
from src.domains.brand.services.metadata_extractor import MetadataExtractor


# Single canonical font categorization (consolidates css_parser._categorize_font
# and metadata_extractor._guess_category)
def categorize_font(name: str) -> str:
    """Guess CSS font category from name."""
    lower = name.lower()
    if any(kw in lower for kw in ("mono", "code", "courier", "consolas")):
        return "monospace"
    if (
        any(kw in lower for kw in ("serif", "georgia", "times", "garamond", "palatino"))
        and "sans" not in lower
    ):
        return "serif"
    if any(kw in lower for kw in ("script", "cursive", "handwriting", "dancing", "pacifico")):
        return "handwriting"
    if any(kw in lower for kw in ("display", "impact", "lobster", "playfair")):
        return "display"
    return "sans-serif"


class FontStage(BaseStage):
    """Extract fonts from HTML and external CSS."""

    name = "fonts"

    def __init__(self) -> None:
        super().__init__()
        self._metadata_extractor = MetadataExtractor()
        self._css_parser = CSSBrandParser()

    def should_run(self, ctx: PipelineContext) -> bool:
        return ctx.soup is not None and ctx.include_fonts and ctx.config.is_feature_enabled("fonts")

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        soup = ctx.soup
        if soup is None:
            raise RuntimeError("FontStage.run() requires ctx.soup (should_run guards this)")

        # HTML fonts (Google Fonts links, inline <style>)
        brand.fonts = self._metadata_extractor.extract_fonts_from_html(soup)

        # External CSS fonts
        css_urls = ctx.css_urls[: ctx.config.max_css_stylesheets]
        if css_urls and ctx.config.should_run("css_parser"):
            try:
                _, css_fonts, css_bg = await self._css_parser.extract_from_url(css_urls, ctx.url)
                # Use CSS background color if not found inline
                if css_bg and ctx.background_color == "#ffffff":
                    ctx.background_color = css_bg
                # Merge CSS fonts (avoid duplicates)
                existing_families = {f.family.lower() for f in brand.fonts}
                for font in css_fonts:
                    if font.family.lower() not in existing_families:
                        brand.fonts.append(font)
                        existing_families.add(font.family.lower())
            except (ValueError, TypeError, KeyError) as e:
                result.add_warning(f"CSS font extraction failed: {e}")

        if brand.fonts:
            ctx.tracker.record("html_meta", "fonts", str(len(brand.fonts)))
