"""Tests for css_parser."""

from __future__ import annotations

import pytest

from src.domains.brand.services.css_parser import (
    CSSBrandParser,
    _categorize_font,
    _is_generic_color,
)


@pytest.fixture
def parser() -> CSSBrandParser:
    return CSSBrandParser()


class TestIsGenericColor:
    def test_near_white(self) -> None:
        assert _is_generic_color("#ffffff") is True
        assert _is_generic_color("#fefefe") is True

    def test_near_black(self) -> None:
        assert _is_generic_color("#000000") is True
        assert _is_generic_color("#0a0a0a") is True

    def test_brand_color_not_generic(self) -> None:
        assert _is_generic_color("#FF5733") is False
        assert _is_generic_color("#2563EB") is False

    def test_invalid_hex(self) -> None:
        assert _is_generic_color("not-a-color") is True


class TestCategorizeFonts:
    def test_monospace(self) -> None:
        assert _categorize_font("Fira Code") == "monospace"
        assert _categorize_font("Courier New") == "monospace"

    def test_serif(self) -> None:
        assert _categorize_font("Georgia") == "serif"
        assert _categorize_font("Times New Roman") == "serif"

    def test_sans_serif_default(self) -> None:
        assert _categorize_font("Roboto") == "sans-serif"
        assert _categorize_font("Open Sans") == "sans-serif"

    def test_handwriting(self) -> None:
        assert _categorize_font("Dancing Script") == "handwriting"

    def test_display(self) -> None:
        assert _categorize_font("Lobster") == "display"


class TestExtractColorsFromCSS:
    def test_extracts_css_variables(self, parser: CSSBrandParser) -> None:
        css = """
        :root {
            --primary-color: #FF5733;
            --brand-color: #2563EB;
            --accent_color: #10B981;
        }
        """
        colors = parser._extract_colors_from_css(css)
        assert "#ff5733" in colors
        assert "#2563eb" in colors
        assert "#10b981" in colors

    def test_skips_near_white(self, parser: CSSBrandParser) -> None:
        css = ":root { --primary-color: #ffffff; }"
        colors = parser._extract_colors_from_css(css)
        assert len(colors) == 0

    def test_skips_near_black(self, parser: CSSBrandParser) -> None:
        css = ":root { --primary-color: #000000; }"
        colors = parser._extract_colors_from_css(css)
        assert len(colors) == 0

    def test_deduplicates(self, parser: CSSBrandParser) -> None:
        css = """
        :root { --primary-color: #FF5733; }
        .theme { --brand-color: #FF5733; }
        """
        colors = parser._extract_colors_from_css(css)
        assert colors.count("#ff5733") == 1

    def test_fallback_to_color_props(self, parser: CSSBrandParser) -> None:
        css = "body { background-color: #3498db; color: #e74c3c; }"
        colors = parser._extract_colors_from_css(css)
        assert "#3498db" in colors


class TestExtractFontsFromCSS:
    def test_heading_font(self, parser: CSSBrandParser) -> None:
        css = "h1 { font-family: 'Playfair Display', serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) >= 1
        assert fonts[0].family == "Playfair Display"
        assert fonts[0].role == "heading"

    def test_body_font(self, parser: CSSBrandParser) -> None:
        css = "body { font-family: 'Open Sans', sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) >= 1
        assert fonts[0].family == "Open Sans"
        assert fonts[0].role == "body"

    def test_skips_generic_families(self, parser: CSSBrandParser) -> None:
        css = "body { font-family: sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) == 0


class TestDetectGoogleFonts:
    def test_from_import(self, parser: CSSBrandParser) -> None:
        css = "@import url('https://fonts.googleapis.com/css2?family=Roboto&display=swap');"
        fonts = parser._detect_google_fonts(css, [])
        assert "Roboto" in fonts

    def test_from_link_urls(self, parser: CSSBrandParser) -> None:
        fonts = parser._detect_google_fonts(
            "", ["https://fonts.googleapis.com/css2?family=Inter&display=swap"]
        )
        assert "Inter" in fonts

    def test_multiple_families(self, parser: CSSBrandParser) -> None:
        css = "@import url('https://fonts.googleapis.com/css2?family=Roboto&family=Open+Sans');"
        fonts = parser._detect_google_fonts(css, [])
        assert "Roboto" in fonts
        assert "Open Sans" in fonts
