"""Tests for output formatter."""

import pytest

from src.domains.brand.schemas import (
    BrandResponse,
    ColorInfo,
    ColorPalette,
    ExtractionMetadata,
    FontInfo,
    LogoAsset,
    SchemaOrgData,
    SocialImages,
)
from src.domains.brand.services.output_formatter import (
    BrandOutputFormatter,
    BrandSummaryForCritique,
    SeriesBrandingData,
    VideoGenerationBrand,
)


@pytest.fixture
def formatter() -> BrandOutputFormatter:
    return BrandOutputFormatter()


@pytest.fixture
def basic_brand() -> BrandResponse:
    return BrandResponse(
        name="Acme Corp",
        domain="acme.com",
        logo_url="https://acme.com/logo.png",
        primary_color="#FF5733",
        secondary_color="#33FF57",
        has_logo=True,
        logo_serve_url="https://cdn.acme.com/logo.png",
    )


@pytest.fixture
def full_brand() -> BrandResponse:
    return BrandResponse(
        name="FullBrand Inc",
        domain="fullbrand.io",
        logo_url="https://fullbrand.io/logo.svg",
        primary_color="#E63946",
        secondary_color="#457B9D",
        has_logo=True,
        logo_serve_url="https://cdn.fullbrand.io/logo.svg",
        logo_asset=LogoAsset(
            asset_id="logo-001",
            original_url="https://fullbrand.io/logo.svg",
            stored_path="/assets/logos/fullbrand-logo.svg",
            content_type="image/svg+xml",
            size_bytes=4096,
        ),
        color_palette=ColorPalette(
            primary=ColorInfo(hex="#E63946", role="primary"),
            secondary=ColorInfo(hex="#457B9D", role="secondary"),
            accent=ColorInfo(hex="#F4A261", role="accent"),
            light_variant=ColorInfo(hex="#F1FAEE", role="light"),
            dark_variant=ColorInfo(hex="#1D3557", role="dark"),
            recommended_text_color="#FFFFFF",
        ),
        fonts=[
            FontInfo(
                family="Montserrat",
                category="sans-serif",
                role="heading",
                weights=["400", "700"],
                is_google_font=True,
                source_url="https://fonts.googleapis.com/css2?family=Montserrat",
            ),
            FontInfo(
                family="Open Sans",
                category="sans-serif",
                role="body",
                weights=["400", "600"],
                is_google_font=True,
                source_url="https://fonts.googleapis.com/css2?family=Open+Sans",
            ),
        ],
        social_images=SocialImages(
            og_image="https://fullbrand.io/og.png",
            twitter_image="https://fullbrand.io/twitter.png",
            apple_touch_icon="https://fullbrand.io/apple-touch.png",
        ),
        schema_org=SchemaOrgData(
            type="Organization",
            name="FullBrand Inc",
            url="https://fullbrand.io",
        ),
        favicon_urls=[
            "https://fullbrand.io/favicon.ico",
            "https://fullbrand.io/favicon-32.png",
        ],
        metadata=ExtractionMetadata(
            source="url",
            confidence=0.92,
            extraction_duration_ms=350,
        ),
    )


# ── to_video_brand ──────────────────────────────────────────────────


class TestToVideoBrand:
    def test_to_video_brand_basic(
        self, formatter: BrandOutputFormatter, basic_brand: BrandResponse
    ) -> None:
        result = formatter.to_video_brand(basic_brand)

        assert result.name == "Acme Corp"
        assert result.logo_url == "https://cdn.acme.com/logo.png"
        assert result.primary_color == "#FF5733"
        assert result.secondary_color == "#33FF57"

    def test_to_video_brand_prefers_serve_url(self, formatter: BrandOutputFormatter) -> None:
        brand = BrandResponse(
            name="Test",
            logo_url="https://example.com/original.png",
            logo_serve_url="https://cdn.example.com/served.png",
        )
        result = formatter.to_video_brand(brand)
        assert result.logo_url == "https://cdn.example.com/served.png"

        brand_no_serve = BrandResponse(
            name="Test",
            logo_url="https://example.com/original.png",
            logo_serve_url="",
        )
        result_no_serve = formatter.to_video_brand(brand_no_serve)
        assert result_no_serve.logo_url == "https://example.com/original.png"

    def test_to_video_brand_font(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_video_brand(full_brand)
        assert result.font_family == "Montserrat"

    def test_to_video_brand_no_fonts(self, formatter: BrandOutputFormatter) -> None:
        brand = BrandResponse(name="NoFont", fonts=[])
        result = formatter.to_video_brand(brand)
        assert result.font_family == ""


# ── to_series_branding ──────────────────────────────────────────────


class TestToSeriesBranding:
    def test_to_series_branding_basic(
        self, formatter: BrandOutputFormatter, basic_brand: BrandResponse
    ) -> None:
        result = formatter.to_series_branding(basic_brand)

        assert result.name == "Acme Corp"
        assert result.color_scheme["primary"] == "#FF5733"
        assert result.color_scheme["secondary"] == "#33FF57"

    def test_to_series_branding_color_scheme(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_series_branding(full_brand)

        assert result.color_scheme["accent"] == "#F4A261"
        assert result.color_scheme["light"] == "#F1FAEE"
        assert result.color_scheme["dark"] == "#1D3557"

    def test_to_series_branding_logo_path(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_series_branding(full_brand)
        assert result.logo_path == "/assets/logos/fullbrand-logo.svg"

        # Falls back to logo_serve_url when no stored_path
        brand_no_stored = BrandResponse(
            name="Test",
            logo_asset=LogoAsset(stored_path=""),
            logo_serve_url="https://cdn.example.com/logo.png",
        )
        result_fallback = formatter.to_series_branding(brand_no_stored)
        assert result_fallback.logo_path == "https://cdn.example.com/logo.png"


# ── to_template_variables ───────────────────────────────────────────


class TestToTemplateVariables:
    def test_to_template_variables_keys(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        variables = formatter.to_template_variables(full_brand)

        expected_keys = {
            "brand_name",
            "brand_domain",
            "brand_primary_color",
            "brand_secondary_color",
            "brand_logo_url",
            "brand_font_heading",
            "brand_font_body",
            "brand_has_logo",
        }
        assert expected_keys.issubset(set(variables.keys()))

    def test_to_template_variables_font_roles(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        variables = formatter.to_template_variables(full_brand)

        assert variables["brand_font_heading"] == "Montserrat"
        assert variables["brand_font_body"] == "Open Sans"

    def test_to_template_variables_has_logo_string(self, formatter: BrandOutputFormatter) -> None:
        brand_with = BrandResponse(has_logo=True)
        brand_without = BrandResponse(has_logo=False)

        vars_with = formatter.to_template_variables(brand_with)
        vars_without = formatter.to_template_variables(brand_without)

        assert vars_with["brand_has_logo"] == "true"
        assert vars_without["brand_has_logo"] == "false"


# ── to_critique_summary ─────────────────────────────────────────────


class TestToCritiqueSummary:
    def test_to_critique_summary_quality(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_critique_summary(full_brand)
        assert result.overall_quality == pytest.approx(0.92)

        # No metadata => 0.0
        brand_no_meta = BrandResponse(domain="example.com")
        result_no_meta = formatter.to_critique_summary(brand_no_meta)
        assert result_no_meta.overall_quality == 0.0

    def test_to_critique_summary_asset_count(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_critique_summary(full_brand)

        # 1 (has_logo) + 2 (favicon_urls) + 3 (og, twitter, apple_touch) = 6
        assert result.asset_count == 6

    def test_to_critique_summary_custom_colors(self, formatter: BrandOutputFormatter) -> None:
        default_brand = BrandResponse(primary_color="#2563EB")
        custom_brand = BrandResponse(primary_color="#FF0000")

        default_result = formatter.to_critique_summary(default_brand)
        custom_result = formatter.to_critique_summary(custom_brand)

        assert default_result.has_custom_colors is False
        assert custom_result.has_custom_colors is True


# ── to_export ────────────────────────────────────────────────────────


class TestToExport:
    def test_to_export_basic(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_export(full_brand)

        assert result["name"] == "FullBrand Inc"
        assert result["domain"] == "fullbrand.io"
        assert result["logo_url"] == "https://cdn.fullbrand.io/logo.svg"
        assert result["primary_color"] == "#E63946"
        assert result["secondary_color"] == "#457B9D"
        assert result["has_logo"] is True
        assert "css_variables" in result
        assert "google_fonts_url" in result
        assert "social_profiles" in result

    def test_to_export_css_variables(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_export(full_brand)
        css = result["css_variables"]

        assert css["--brand-primary"] == "#E63946"
        assert css["--brand-secondary"] == "#457B9D"
        assert css["--brand-accent"] == "#F4A261"
        assert css["--brand-light"] == "#F1FAEE"
        assert css["--brand-dark"] == "#1D3557"
        assert css["--brand-text"] == "#FFFFFF"

    def test_to_export_google_fonts_url(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_export(full_brand)
        url = result["google_fonts_url"]

        assert url.startswith("https://fonts.googleapis.com/css2?")
        assert "family=Montserrat" in url
        assert "family=Open%20Sans" in url
        assert "wght@400;700" in url
        assert "wght@400;600" in url
        assert url.endswith("&display=swap")

    def test_to_export_no_google_fonts(self, formatter: BrandOutputFormatter) -> None:
        brand = BrandResponse(fonts=[FontInfo(family="Custom Font", is_google_font=False)])
        result = formatter.to_export(brand)
        assert result["google_fonts_url"] == ""

    def test_to_export_heading_body_fonts(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_export(full_brand)

        assert result["heading_font"] == "Montserrat"
        assert result["body_font"] == "Open Sans"

    def test_to_export_favicon(
        self, formatter: BrandOutputFormatter, full_brand: BrandResponse
    ) -> None:
        result = formatter.to_export(full_brand)
        assert result["favicon_url"] == "https://fullbrand.io/favicon.ico"

        # No favicons => empty string
        brand_no_fav = BrandResponse()
        result_no_fav = formatter.to_export(brand_no_fav)
        assert result_no_fav["favicon_url"] == ""


# ── Dataclass serialization ─────────────────────────────────────────


class TestDataclassSerialization:
    def test_video_brand_to_dict(self) -> None:
        vb = VideoGenerationBrand(
            name="Acme",
            logo_url="https://acme.com/logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            tone="professional",
            font_family="Roboto",
        )
        d = vb.to_dict()

        assert d == {
            "name": "Acme",
            "logo_url": "https://acme.com/logo.png",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "tone": "professional",
            "font_family": "Roboto",
        }

    def test_series_branding_to_dict(self) -> None:
        sb = SeriesBrandingData(
            name="SeriesBrand",
            logo_path="/logos/series.png",
            color_scheme={"primary": "#111", "secondary": "#222"},
            font_family="Lato",
        )
        d = sb.to_dict()

        assert d == {
            "name": "SeriesBrand",
            "logo_path": "/logos/series.png",
            "color_scheme": {"primary": "#111", "secondary": "#222"},
            "font_family": "Lato",
        }

    def test_critique_summary_to_dict(self) -> None:
        cs = BrandSummaryForCritique(
            domain="example.com",
            overall_quality=0.85,
            has_logo=True,
            has_custom_colors=True,
            has_fonts=True,
            has_schema_org=False,
            has_social_images=True,
            has_favicons=True,
            asset_count=5,
        )
        d = cs.to_dict()

        assert d == {
            "domain": "example.com",
            "overall_quality": 0.85,
            "has_logo": True,
            "has_custom_colors": True,
            "has_fonts": True,
            "has_schema_org": False,
            "has_social_images": True,
            "has_favicons": True,
            "asset_count": 5,
        }
