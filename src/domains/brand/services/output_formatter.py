"""Structured output formatting for downstream service consumption.

Provides formatters for video generation, series branding, template engines,
critique summaries, and pipeline-optimized exports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from src.domains.brand.schemas import BrandResponse


@dataclass
class VideoGenerationBrand:
    """Brand data optimized for video generation service."""

    name: str = ""
    logo_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    tone: str = ""
    font_family: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "logo_url": self.logo_url,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "tone": self.tone,
            "font_family": self.font_family,
        }


@dataclass
class SeriesBrandingData:
    """Brand data for series/recurring content branding."""

    name: str = ""
    logo_path: str = ""
    color_scheme: dict = field(default_factory=dict)
    font_family: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "logo_path": self.logo_path,
            "color_scheme": self.color_scheme,
            "font_family": self.font_family,
        }


@dataclass
class BrandSummaryForCritique:
    """Brand metrics for critique/review service."""

    domain: str = ""
    overall_quality: float = 0.0
    has_logo: bool = False
    has_custom_colors: bool = False
    has_fonts: bool = False
    has_schema_org: bool = False
    has_social_images: bool = False
    has_favicons: bool = False
    asset_count: int = 0

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "overall_quality": self.overall_quality,
            "has_logo": self.has_logo,
            "has_custom_colors": self.has_custom_colors,
            "has_fonts": self.has_fonts,
            "has_schema_org": self.has_schema_org,
            "has_social_images": self.has_social_images,
            "has_favicons": self.has_favicons,
            "asset_count": self.asset_count,
        }


class BrandOutputFormatter:
    """Format brand data for various downstream consumers."""

    def to_video_brand(self, brand: BrandResponse) -> VideoGenerationBrand:
        """Format brand data for the video generation service.

        Args:
            brand: Full brand response to format.

        Returns:
            VideoGenerationBrand with essential fields for video rendering.
        """
        font = brand.fonts[0].family if brand.fonts else ""
        tone = getattr(brand, "brand_voice", "") or ""
        return VideoGenerationBrand(
            name=brand.name,
            logo_url=brand.logo_serve_url or brand.logo_url,
            primary_color=brand.primary_color,
            secondary_color=brand.secondary_color,
            tone=tone,
            font_family=font,
        )

    def to_series_branding(self, brand: BrandResponse) -> SeriesBrandingData:
        """Format brand data for series/recurring content branding.

        Args:
            brand: Full brand response to format.

        Returns:
            SeriesBrandingData with color scheme and logo path.
        """
        color_scheme = {
            "primary": brand.primary_color,
            "secondary": brand.secondary_color,
        }
        if brand.color_palette:
            if brand.color_palette.accent:
                color_scheme["accent"] = brand.color_palette.accent.hex
            if brand.color_palette.light_variant:
                color_scheme["light"] = brand.color_palette.light_variant.hex
            if brand.color_palette.dark_variant:
                color_scheme["dark"] = brand.color_palette.dark_variant.hex

        font = brand.fonts[0].family if brand.fonts else ""
        logo_path = ""
        if brand.logo_asset and brand.logo_asset.stored_path:
            logo_path = brand.logo_asset.stored_path
        elif brand.logo_serve_url:
            logo_path = brand.logo_serve_url

        return SeriesBrandingData(
            name=brand.name,
            logo_path=logo_path,
            color_scheme=color_scheme,
            font_family=font,
        )

    def to_template_variables(self, brand: BrandResponse) -> dict[str, str]:
        """Generate flat key-value pairs for template engine variable substitution.

        Args:
            brand: Full brand response to format.

        Returns:
            Dict of brand_* prefixed template variables.
        """
        font = brand.fonts[0].family if brand.fonts else ""
        body_font = ""
        for f in brand.fonts:
            if f.role == "body":
                body_font = f.family
                break

        variables = {
            "brand_name": brand.name,
            "brand_domain": brand.domain,
            "brand_primary_color": brand.primary_color,
            "brand_secondary_color": brand.secondary_color,
            "brand_logo_url": brand.logo_serve_url or brand.logo_url,
            "brand_font_heading": font,
            "brand_font_body": body_font or font,
            "brand_has_logo": str(brand.has_logo).lower(),
        }

        if brand.color_palette and brand.color_palette.accent:
            variables["brand_accent_color"] = brand.color_palette.accent.hex
        if brand.color_palette:
            variables["brand_text_color"] = brand.color_palette.recommended_text_color

        return variables

    def to_critique_summary(self, brand: BrandResponse) -> BrandSummaryForCritique:
        """Summarize brand completeness metrics for the critique/review service.

        Args:
            brand: Full brand response to summarize.

        Returns:
            BrandSummaryForCritique with quality indicators and asset counts.
        """
        asset_count = 0
        if brand.has_logo:
            asset_count += 1
        asset_count += len(brand.favicon_urls)
        if brand.social_images:
            if brand.social_images.og_image:
                asset_count += 1
            if brand.social_images.twitter_image:
                asset_count += 1
            if brand.social_images.apple_touch_icon:
                asset_count += 1

        confidence = 0.0
        if brand.metadata:
            confidence = brand.metadata.confidence

        return BrandSummaryForCritique(
            domain=brand.domain,
            overall_quality=confidence,
            has_logo=brand.has_logo,
            has_custom_colors=brand.primary_color != "#2563EB",
            has_fonts=bool(brand.fonts),
            has_schema_org=brand.schema_org is not None,
            has_social_images=brand.social_images is not None
            and bool(brand.social_images.og_image),
            has_favicons=bool(brand.favicon_urls),
            asset_count=asset_count,
        )

    @staticmethod
    def _build_css_variables(brand: BrandResponse) -> dict[str, str]:
        css_vars = {
            "--brand-primary": brand.primary_color,
            "--brand-secondary": brand.secondary_color,
        }
        if brand.color_palette:
            if brand.color_palette.accent:
                css_vars["--brand-accent"] = brand.color_palette.accent.hex
            if brand.color_palette.light_variant:
                css_vars["--brand-light"] = brand.color_palette.light_variant.hex
            if brand.color_palette.dark_variant:
                css_vars["--brand-dark"] = brand.color_palette.dark_variant.hex
            css_vars["--brand-text"] = brand.color_palette.recommended_text_color
        return css_vars

    @staticmethod
    def _build_google_fonts_url(brand: BrandResponse) -> str:
        google_fonts = [f for f in brand.fonts if f.is_google_font]
        if not google_fonts:
            return ""
        families = []
        for font in google_fonts:
            family = quote(font.family)
            if font.weights:
                family += ":wght@" + ";".join(font.weights)
            families.append(f"family={family}")
        return "https://fonts.googleapis.com/css2?" + "&".join(families) + "&display=swap"

    @staticmethod
    def _resolve_role_fonts(brand: BrandResponse) -> tuple[str, str]:
        heading_font = ""
        body_font = ""
        for f in brand.fonts:
            if f.role == "heading" and not heading_font:
                heading_font = f.family
            elif f.role == "body" and not body_font:
                body_font = f.family
        if not heading_font and brand.fonts:
            heading_font = brand.fonts[0].family
        if not body_font:
            body_font = heading_font
        return heading_font, body_font

    def to_export(self, brand: BrandResponse) -> dict:
        """Pipeline-optimized flat export format.

        Includes: css_variables, google_fonts_url, social_profiles dict,
        all core brand fields in a flat structure.
        """
        css_vars = self._build_css_variables(brand)
        google_fonts_url = self._build_google_fonts_url(brand)

        social: dict = {}
        sp = getattr(brand, "social_profiles", None)
        if sp:
            social = sp.to_dict() if hasattr(sp, "to_dict") else {}

        heading_font, body_font = self._resolve_role_fonts(brand)

        return {
            "name": brand.name,
            "domain": brand.domain,
            "logo_url": brand.logo_serve_url or brand.logo_url,
            "primary_color": brand.primary_color,
            "secondary_color": brand.secondary_color,
            "heading_font": heading_font,
            "body_font": body_font,
            "tagline": getattr(brand, "tagline", ""),
            "tone": getattr(brand, "brand_voice", ""),
            "css_variables": css_vars,
            "google_fonts_url": google_fonts_url,
            "social_profiles": social,
            "has_logo": brand.has_logo,
            "favicon_url": brand.favicon_urls[0] if brand.favicon_urls else "",
        }
