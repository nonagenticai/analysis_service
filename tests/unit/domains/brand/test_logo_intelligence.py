"""Tests for logo_intelligence service."""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.brand.services.logo_intelligence import (
    LogoIntelligence,
    LogoIntelligenceExtractor,
    LogoVariant,
)
from tests.fixtures.html_logos_advanced import (
    LOGO_CENTER_POSITION,
    LOGO_IN_HEADER,
    LOGO_INLINE_SVG,
    LOGO_MULTIPLE_VARIANTS,
    LOGO_NO_HEADER,
    LOGO_RIGHT_POSITION,
    LOGO_SCHEMA_ORG,
    LOGO_SVG_IN_HEADER,
    NO_LOGOS,
)

BASE_URL = "https://example.com"


def _parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _extract(html: str, schema_org: dict | None = None) -> LogoIntelligence:
    extractor = LogoIntelligenceExtractor()
    soup = _parse(html)
    return extractor.extract(soup, BASE_URL, schema_org)


class TestExtractLogoFromHeader:
    def test_extract_logo_from_header(self) -> None:
        """img with logo class in header is found."""
        result = _extract(LOGO_IN_HEADER)
        assert result.primary_logo_url == "https://example.com/logo.png"
        img_variants = [v for v in result.variants if v.source == "img_tag"]
        assert len(img_variants) >= 1
        assert img_variants[0].in_header is True
        assert img_variants[0].format == "png"


class TestSvgPreference:
    def test_svg_preference(self) -> None:
        """SVG in header is preferred over PNG."""
        result = _extract(LOGO_SVG_IN_HEADER)
        assert result.primary_logo_url == "https://example.com/logo.svg"
        assert result.has_svg is True


class TestInlineSvgDetected:
    def test_inline_svg_detected(self) -> None:
        """Inline SVG in header is counted."""
        result = _extract(LOGO_INLINE_SVG)
        inline = [v for v in result.variants if v.source == "inline_svg"]
        assert len(inline) == 1
        assert inline[0].is_svg is True
        assert inline[0].in_header is True
        assert inline[0].format == "svg"
        assert result.inline_svg_count == 1


class TestMultipleVariants:
    def test_multiple_variants(self) -> None:
        """All sources collected and deduped."""
        result = _extract(LOGO_MULTIPLE_VARIANTS)
        sources = {v.source for v in result.variants}
        assert "img_tag" in sources
        assert "favicon" in sources
        assert "og_image" in sources
        # Should have header img + favicons + og:image
        assert len(result.variants) >= 5


class TestSchemaOrgLogo:
    def test_schema_org_logo(self) -> None:
        """schema.org logo collected when passed via schema_org dict."""
        schema = {"logo": "https://example.com/schema-logo.svg"}
        result = _extract(LOGO_SCHEMA_ORG, schema_org=schema)
        schema_variants = [v for v in result.variants if v.source == "schema_org"]
        assert len(schema_variants) == 1
        assert schema_variants[0].url == "https://example.com/schema-logo.svg"
        assert schema_variants[0].is_svg is True


class TestFaviconVariants:
    def test_favicon_variants(self) -> None:
        """Favicons with sizes collected."""
        result = _extract(LOGO_MULTIPLE_VARIANTS)
        favicons = [v for v in result.variants if v.source == "favicon"]
        assert len(favicons) >= 3
        # Check that sizes were parsed
        sizes_found = {(v.width, v.height) for v in favicons if v.width and v.height}
        assert (16, 16) in sizes_found
        assert (32, 32) in sizes_found
        assert (192, 192) in sizes_found


class TestOgImageVariant:
    def test_og_image_variant(self) -> None:
        """og:image collected."""
        result = _extract(LOGO_MULTIPLE_VARIANTS)
        og_variants = [v for v in result.variants if v.source == "og_image"]
        assert len(og_variants) == 1
        assert og_variants[0].url == "https://example.com/og-image.jpg"
        assert og_variants[0].format == "jpg"


class TestPrimaryLogoSelectionSvgHeader:
    def test_primary_logo_selection_svg_header(self) -> None:
        """SVG in header wins primary selection."""
        result = _extract(LOGO_SVG_IN_HEADER)
        assert result.primary_logo_url == "https://example.com/logo.svg"


class TestPrimaryLogoSelectionLargestHeader:
    def test_primary_logo_selection_largest_header(self) -> None:
        """Largest header img wins when no SVG in header."""
        result = _extract(LOGO_IN_HEADER)
        # The PNG in header (200x50) should be primary
        assert result.primary_logo_url == "https://example.com/logo.png"


class TestPrimaryLogoSelectionSchemaOrg:
    def test_primary_logo_selection_schema_org(self) -> None:
        """schema.org fallback when no header logo."""
        # Parse HTML with no header logo, but with schema_org
        html = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body><p>No header logo</p></body></html>
"""
        schema = {"logo": "https://example.com/schema-logo.png"}
        result = _extract(html, schema_org=schema)
        assert result.primary_logo_url == "https://example.com/schema-logo.png"


class TestPrimaryLogoSelectionFavicon:
    def test_primary_logo_selection_favicon(self) -> None:
        """Favicon fallback when no other logo sources."""
        html = """<!DOCTYPE html>
<html><head><title>Test</title>
<link rel="icon" sizes="64x64" href="/favicon-64.png">
</head>
<body><p>No logo images</p></body></html>
"""
        result = _extract(html)
        assert result.primary_logo_url == "https://example.com/favicon-64.png"


class TestPrimaryLogoSelectionOg:
    def test_primary_logo_selection_og(self) -> None:
        """og:image last resort for primary logo."""
        html = """<!DOCTYPE html>
<html><head><title>Test</title>
<meta property="og:image" content="https://example.com/social-card.jpg">
</head>
<body><p>No logo images</p></body></html>
"""
        result = _extract(html)
        assert result.primary_logo_url == "https://example.com/social-card.jpg"


class TestPositionHeaderLeft:
    def test_position_header_left(self) -> None:
        """Default position in header is header-left."""
        result = _extract(LOGO_IN_HEADER)
        assert result.position == "header-left"


class TestPositionHeaderCenter:
    def test_position_header_center(self) -> None:
        """Center class detected."""
        result = _extract(LOGO_CENTER_POSITION)
        assert result.position == "header-center"


class TestPositionHeaderRight:
    def test_position_header_right(self) -> None:
        """Right class detected."""
        result = _extract(LOGO_RIGHT_POSITION)
        assert result.position == "header-right"


class TestPositionNoHeader:
    def test_position_no_header(self) -> None:
        """Unknown when no header."""
        result = _extract(LOGO_NO_HEADER)
        assert result.position == "unknown"


class TestHasSvgFlag:
    def test_has_svg_flag(self) -> None:
        """True when SVG variant exists."""
        result = _extract(LOGO_SVG_IN_HEADER)
        assert result.has_svg is True

        result_no_svg = _extract(LOGO_IN_HEADER)
        assert result_no_svg.has_svg is False


class TestHasRetinaFlag:
    def test_has_retina_flag(self) -> None:
        """True when width > 100."""
        result = _extract(LOGO_IN_HEADER)
        # Logo has width=200
        assert result.has_retina is True

        result_small = _extract(LOGO_SVG_IN_HEADER)
        # Logo has width=120
        assert result_small.has_retina is True


class TestInlineSvgCount:
    def test_inline_svg_count(self) -> None:
        """Counts inline SVGs."""
        result = _extract(LOGO_INLINE_SVG)
        assert result.inline_svg_count == 1

        result_none = _extract(LOGO_IN_HEADER)
        assert result_none.inline_svg_count == 0


class TestLogoAltText:
    def test_logo_alt_text(self) -> None:
        """Alt text from logo img."""
        result = _extract(LOGO_IN_HEADER)
        assert result.logo_alt_text == "Example Logo"


class TestNoLogos:
    def test_no_logos(self) -> None:
        """Empty result for no logos."""
        result = _extract(NO_LOGOS)
        assert result.primary_logo_url == ""
        assert result.variants == []
        assert result.position == "unknown"
        assert result.has_svg is False
        assert result.has_retina is False
        assert result.inline_svg_count == 0
        assert result.logo_alt_text == ""


class TestLogoVariantToDict:
    def test_logo_variant_to_dict(self) -> None:
        """Serialization works for LogoVariant and LogoIntelligence."""
        variant = LogoVariant(
            url="https://example.com/logo.png",
            source="img_tag",
            format="png",
            width=200,
            height=50,
            is_svg=False,
            in_header=True,
        )
        d = variant.to_dict()
        assert d == {
            "url": "https://example.com/logo.png",
            "source": "img_tag",
            "format": "png",
            "width": 200,
            "height": 50,
            "is_svg": False,
            "in_header": True,
        }

        intel = LogoIntelligence(
            primary_logo_url="https://example.com/logo.png",
            variants=[variant],
            position="header-left",
            has_svg=False,
            has_retina=True,
            inline_svg_count=0,
            logo_alt_text="Example",
        )
        intel_dict = intel.to_dict()
        assert intel_dict["primary_logo_url"] == "https://example.com/logo.png"
        assert intel_dict["position"] == "header-left"
        assert intel_dict["has_svg"] is False
        assert intel_dict["has_retina"] is True
        assert intel_dict["inline_svg_count"] == 0
        assert intel_dict["logo_alt_text"] == "Example"
        assert len(intel_dict["variants"]) == 1
        assert intel_dict["variants"][0]["url"] == "https://example.com/logo.png"
