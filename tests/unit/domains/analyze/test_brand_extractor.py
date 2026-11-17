"""Tests for BrandExtractor."""

from __future__ import annotations

from src.domains.analyze.services.brand_extractor import BrandExtractor


class TestExtractDomain:
    def test_basic_url(self) -> None:
        pages = [{"url": "https://www.example.com/page"}]
        assert BrandExtractor._extract_domain(pages) == "example.com"

    def test_no_www(self) -> None:
        pages = [{"url": "https://acme.io/about"}]
        assert BrandExtractor._extract_domain(pages) == "acme.io"

    def test_empty_pages(self) -> None:
        assert BrandExtractor._extract_domain([]) == ""

    def test_no_url(self) -> None:
        assert BrandExtractor._extract_domain([{"title": "Hi"}]) == ""


class TestExtractName:
    def test_from_short_description(self) -> None:
        pages = [{"title": "Acme - Home"}]
        sa = {"description": "Acme Corp"}
        assert BrandExtractor._extract_name(pages, sa) == "Acme Corp"

    def test_fallback_to_title(self) -> None:
        pages = [{"title": "Acme Corp | Home"}]
        result = BrandExtractor._extract_name(pages, None)
        assert result == "Acme Corp"

    def test_long_description_uses_title(self) -> None:
        pages = [{"title": "ShortBrand"}]
        sa = {"description": "A" * 80}
        assert BrandExtractor._extract_name(pages, sa) == "ShortBrand"

    def test_no_pages_no_analysis(self) -> None:
        assert BrandExtractor._extract_name([], None) == ""

    def test_title_suffix_stripping(self) -> None:
        pages = [{"title": "MySite - Welcome to our platform"}]
        result = BrandExtractor._extract_name(pages, None)
        # Should strip the "- Welcome..." suffix
        assert "Welcome" not in result


class TestExtractLogoUrl:
    def test_from_logo_candidates(self) -> None:
        pages = [{"logo_candidates": ["/img/logo.png", "/img/icon.png"]}]
        assert BrandExtractor._extract_logo_url(pages) == "/img/logo.png"

    def test_from_og_image(self) -> None:
        pages = [{"metadata": {"og_tags": {"image": "https://example.com/og.jpg"}}}]
        assert BrandExtractor._extract_logo_url(pages) == "https://example.com/og.jpg"

    def test_no_logo(self) -> None:
        pages = [{"url": "https://example.com"}]
        assert BrandExtractor._extract_logo_url(pages) == ""


class TestExtractColors:
    def test_valid_hex(self) -> None:
        sa = {"visual_identity": {"primary_color": "#FF5500"}}
        primary, secondary = BrandExtractor._extract_colors(sa)
        assert primary == "#FF5500"
        assert secondary != ""

    def test_short_hex(self) -> None:
        sa = {"visual_identity": {"primary_color": "#F50"}}
        primary, secondary = BrandExtractor._extract_colors(sa)
        assert primary == "#F50"
        assert secondary != ""

    def test_invalid_color(self) -> None:
        sa = {"visual_identity": {"primary_color": "blue"}}
        primary, secondary = BrandExtractor._extract_colors(sa)
        assert primary == ""
        assert secondary == ""

    def test_no_analysis(self) -> None:
        assert BrandExtractor._extract_colors(None) == ("", "")

    def test_alternative_key(self) -> None:
        sa = {"visual_identity": {"primary": "#ABCDEF"}}
        primary, _ = BrandExtractor._extract_colors(sa)
        assert primary == "#ABCDEF"


class TestDarkenHex:
    def test_darken(self) -> None:
        result = BrandExtractor._darken_hex("#FFFFFF")
        # 255 * 0.8 = 204 = 0xCC
        assert result == "#cccccc"

    def test_darken_short_hex(self) -> None:
        result = BrandExtractor._darken_hex("#FFF")
        assert result == "#cccccc"

    def test_darken_black(self) -> None:
        result = BrandExtractor._darken_hex("#000000")
        assert result == "#000000"


class TestExtractBrand:
    def test_full_extraction(self) -> None:
        pages = [
            {
                "url": "https://www.acme.com/",
                "title": "Acme Corp",
                "logo_candidates": ["/logo.svg"],
            }
        ]
        sa = {
            "description": "Acme Corp",
            "visual_identity": {"primary_color": "#336699"},
        }
        brand = BrandExtractor.extract_brand(pages, sa)
        assert brand["name"] == "Acme Corp"
        assert brand["domain"] == "acme.com"
        assert brand["logo_url"] == "/logo.svg"
        assert brand["primary_color"] == "#336699"
        assert brand["secondary_color"] != ""

    def test_minimal_data(self) -> None:
        brand = BrandExtractor.extract_brand([], None)
        assert brand["name"] == ""
        assert brand["domain"] == ""
        assert brand["logo_url"] == ""
