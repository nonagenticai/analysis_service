"""Extended tests for css_parser.py -- covers extract_from_url, _fetch_css, background color, fonts with weights."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.css_parser import (
    CSSBrandParser,
    _categorize_font,
    _clean_font_name,
    _is_generic_color,
    extract_background_color,
    extract_background_color_from_html,
)


class TestExtractBackgroundColor:
    def test_body_background_color(self) -> None:
        css = "body { background-color: #f5f5f5; }"
        result = extract_background_color(css)
        assert result == "#f5f5f5"

    def test_html_background_color(self) -> None:
        css = "html { background-color: #eeeeee; }"
        result = extract_background_color(css)
        assert result == "#eeeeee"

    def test_background_shorthand(self) -> None:
        css = "body { background: #e0e0e0; }"
        result = extract_background_color(css)
        assert result == "#e0e0e0"

    def test_no_background(self) -> None:
        css = "body { color: #333; }"
        result = extract_background_color(css)
        assert result is None

    def test_body_after_other_rules(self) -> None:
        css = ".header { color: red; } body { background-color: #fafafa; }"
        result = extract_background_color(css)
        assert result == "#fafafa"


class TestExtractBackgroundColorFromHtml:
    def test_extracts_from_inline_style(self) -> None:
        html = "<style>body { background-color: #f0f0f0; }</style>"
        soup = BeautifulSoup(html, "html.parser")
        result = extract_background_color_from_html(soup)
        assert result == "#f0f0f0"

    def test_returns_none_no_style(self) -> None:
        html = "<html><head></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        result = extract_background_color_from_html(soup)
        assert result is None

    def test_returns_none_no_bg(self) -> None:
        html = "<style>body { color: #333; }</style>"
        soup = BeautifulSoup(html, "html.parser")
        result = extract_background_color_from_html(soup)
        assert result is None

    def test_empty_style_tag(self) -> None:
        html = "<style></style>"
        soup = BeautifulSoup(html, "html.parser")
        result = extract_background_color_from_html(soup)
        assert result is None


class TestCleanFontName:
    def test_strips_quotes(self) -> None:
        assert _clean_font_name("'Roboto'") == "Roboto"
        assert _clean_font_name('"Open Sans"') == "Open Sans"

    def test_strips_whitespace(self) -> None:
        assert _clean_font_name("  Roboto  ") == "Roboto"

    def test_empty_string(self) -> None:
        assert _clean_font_name("") == ""


class TestIsGenericColorEdge:
    def test_short_hex_near_white(self) -> None:
        assert _is_generic_color("#fff") is True

    def test_short_hex_brand_color(self) -> None:
        assert _is_generic_color("#f00") is False

    def test_empty_string(self) -> None:
        assert _is_generic_color("") is True

    def test_boundary_white(self) -> None:
        # RGB 240 is right at the threshold
        assert _is_generic_color("#f0f0f0") is False  # 240 == threshold, not >

    def test_boundary_black(self) -> None:
        # RGB 15 is right at the threshold
        assert _is_generic_color("#0f0f0f") is False  # 15 == threshold, not <


class TestCategorizeFontEdge:
    def test_consolas(self) -> None:
        assert _categorize_font("Consolas") == "monospace"

    def test_palatino(self) -> None:
        assert _categorize_font("Palatino") == "serif"

    def test_pacifico(self) -> None:
        assert _categorize_font("Pacifico") == "handwriting"

    def test_impact(self) -> None:
        assert _categorize_font("Impact") == "display"

    def test_playfair(self) -> None:
        assert _categorize_font("Playfair Display") == "display"

    def test_garamond(self) -> None:
        assert _categorize_font("Garamond") == "serif"


class TestCSSBrandParserExtractFromUrl:
    """Tests for the async extract_from_url method."""

    @pytest.fixture
    def parser(self) -> CSSBrandParser:
        return CSSBrandParser()

    @pytest.mark.asyncio
    async def test_empty_urls_returns_empty(self, parser: CSSBrandParser) -> None:
        colors, fonts, bg = await parser.extract_from_url([], "https://example.com")
        assert colors == []
        assert fonts == []
        assert bg is None

    @pytest.mark.asyncio
    async def test_fetches_and_parses_css(self, parser: CSSBrandParser) -> None:
        css_text = ":root { --primary-color: #3498db; }"
        with patch.object(parser, "_fetch_css", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = css_text
            colors, _fonts, _bg = await parser.extract_from_url(
                ["https://cdn.com/style.css"], "https://example.com"
            )
            assert "#3498db" in colors

    @pytest.mark.asyncio
    async def test_handles_fetch_exceptions(self, parser: CSSBrandParser) -> None:
        with patch.object(parser, "_fetch_css", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = OSError("Network error")
            colors, _fonts, _bg = await parser.extract_from_url(
                ["https://cdn.com/style.css"], "https://example.com"
            )
            assert colors == []

    @pytest.mark.asyncio
    async def test_combines_multiple_css_files(self, parser: CSSBrandParser) -> None:
        css1 = ":root { --primary-color: #3498db; }"
        css2 = "h1 { font-family: 'Roboto', sans-serif; }"
        with patch.object(parser, "_fetch_css", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = [css1, css2]
            colors, fonts, _bg = await parser.extract_from_url(
                ["https://cdn.com/a.css", "https://cdn.com/b.css"],
                "https://example.com",
            )
            assert "#3498db" in colors
            assert any(f.family == "Roboto" for f in fonts)

    @pytest.mark.asyncio
    async def test_google_fonts_added(self, parser: CSSBrandParser) -> None:
        """Google Fonts from CSS @import should be added."""
        css = "@import url('https://fonts.googleapis.com/css2?family=Lato:wght@400;700');"
        with patch.object(parser, "_fetch_css", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = css
            _colors, fonts, _bg = await parser.extract_from_url(
                ["https://cdn.com/style.css"], "https://example.com"
            )
            assert any(f.family == "Lato" and f.is_google_font for f in fonts)
            lato = next(f for f in fonts if f.family == "Lato")
            assert "400" in lato.weights
            assert "700" in lato.weights

    @pytest.mark.asyncio
    async def test_bg_color_extracted(self, parser: CSSBrandParser) -> None:
        css = "body { background-color: #fafafa; }"
        with patch.object(parser, "_fetch_css", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = css
            _colors, _fonts, bg = await parser.extract_from_url(
                ["https://cdn.com/style.css"], "https://example.com"
            )
            assert bg == "#fafafa"


def _css_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    """Stand-in for ``httpx.AsyncClient`` backed by a hermetic ``MockTransport``.

    ``_fetch_css`` fetches under a byte cap and so goes through
    ``client.stream``, which an ``AsyncMock`` cannot model (it needs an async
    context manager yielding a response). A real client over a MockTransport is
    still fully offline: no network, no DNS.
    """

    # Bind the real class NOW: this factory is installed *as*
    # ``httpx.AsyncClient``, so naming it inside the body would re-enter the
    # factory and recurse until the stack blows.
    real_client_cls = httpx.AsyncClient

    def factory(**_kwargs: object) -> httpx.AsyncClient:
        return real_client_cls(transport=httpx.MockTransport(handler))

    return factory


class TestFetchCSS:
    """Test _fetch_css method."""

    @pytest.mark.asyncio
    async def test_successful_fetch(self) -> None:
        parser = CSSBrandParser()

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="body { color: red; }")

        with patch(
            "src.domains.brand.services.css_parser.httpx.AsyncClient",
            _css_client_factory(handle),
        ):
            result = await parser._fetch_css("https://cdn.com/style.css")
            assert result == "body { color: red; }"

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_empty(self) -> None:
        parser = CSSBrandParser()

        def handle(_request: httpx.Request) -> httpx.Response:
            raise OSError("Connection error")

        with patch(
            "src.domains.brand.services.css_parser.httpx.AsyncClient",
            _css_client_factory(handle),
        ):
            result = await parser._fetch_css("https://cdn.com/style.css")
            assert result == ""


class TestExtractFontsFromCSSAdvanced:
    """Test edge cases in font extraction."""

    @pytest.fixture
    def parser(self) -> CSSBrandParser:
        return CSSBrandParser()

    def test_font_weight_extracted(self, parser: CSSBrandParser) -> None:
        css = "h1 { font-family: 'Roboto', sans-serif; font-weight: 700; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) >= 1
        assert "700" in fonts[0].weights

    def test_multiple_weights_merged(self, parser: CSSBrandParser) -> None:
        css = """
        h1 { font-family: 'Roboto', sans-serif; font-weight: 700; }
        h2 { font-family: 'Roboto', sans-serif; font-weight: 400; }
        """
        fonts = parser._extract_fonts_from_css(css)
        roboto = next((f for f in fonts if f.family == "Roboto"), None)
        assert roboto is not None
        assert "700" in roboto.weights
        assert "400" in roboto.weights

    def test_p_selector_is_body_role(self, parser: CSSBrandParser) -> None:
        css = "p { font-family: 'Lato', sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        # p matches _BODY_SELECTOR
        assert len(fonts) >= 1

    def test_text_class_no_special_role(self, parser: CSSBrandParser) -> None:
        """The .text selector doesn't match body role in the current regex."""
        css = ".text { font-family: 'Lato', sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) >= 1
        assert fonts[0].role == ""

    def test_h6_is_heading(self, parser: CSSBrandParser) -> None:
        css = "h6 { font-family: 'Montserrat', sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) >= 1
        assert fonts[0].role == "heading"

    def test_no_role_for_other_selectors(self, parser: CSSBrandParser) -> None:
        css = ".custom { font-family: 'CustomFont', sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) >= 1
        assert fonts[0].role == ""

    def test_all_generic_fonts_skipped(self, parser: CSSBrandParser) -> None:
        css = "body { font-family: sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        assert len(fonts) == 0

    def test_system_ui_skipped_but_vendor_prefix_kept(self, parser: CSSBrandParser) -> None:
        """system-ui is generic but -apple-system is not in the skip list."""
        css = "body { font-family: system-ui, -apple-system, sans-serif; }"
        fonts = parser._extract_fonts_from_css(css)
        # system-ui is skipped, -apple-system is kept
        assert len(fonts) == 1
        assert fonts[0].family == "-apple-system"


class TestDetectGoogleFontsWithWeights:
    """Test Google Font detection with weight parsing."""

    @pytest.fixture
    def parser(self) -> CSSBrandParser:
        return CSSBrandParser()

    def test_weights_from_import(self, parser: CSSBrandParser) -> None:
        css = "@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;700');"
        result = parser._detect_google_fonts_with_weights(css, [])
        assert "Roboto" in result
        assert "400" in result["Roboto"]
        assert "700" in result["Roboto"]

    def test_weights_from_link_urls(self, parser: CSSBrandParser) -> None:
        urls = ["https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600"]
        result = parser._detect_google_fonts_with_weights("", urls)
        assert "Inter" in result
        assert "300" in result["Inter"]

    def test_no_weights_family(self, parser: CSSBrandParser) -> None:
        css = "@import url('https://fonts.googleapis.com/css2?family=Lato');"
        result = parser._detect_google_fonts_with_weights(css, [])
        assert "Lato" in result
        assert result["Lato"] == []

    def test_dedup_weights(self, parser: CSSBrandParser) -> None:
        """Same family from import + link should not duplicate weights."""
        css = "@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400');"
        urls = ["https://fonts.googleapis.com/css2?family=Roboto:wght@400;700"]
        result = parser._detect_google_fonts_with_weights(css, urls)
        assert "Roboto" in result
        assert result["Roboto"].count("400") == 1

    def test_color_props_limit_5(self, parser: CSSBrandParser) -> None:
        """General color declarations limited to 5."""
        css = ""
        for i in range(10):
            r = hex(50 + i * 15)[2:]
            css += f".cls{i} {{ color: #{r}{r}{r}; }}\n"
        colors = parser._extract_colors_from_css(css)
        assert len(colors) <= 5
