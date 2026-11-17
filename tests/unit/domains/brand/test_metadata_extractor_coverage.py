"""Extended tests for metadata_extractor.py -- covers manifest, edge cases in fonts, schema.org."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.metadata_extractor import MetadataExtractor


@pytest.fixture
def extractor() -> MetadataExtractor:
    return MetadataExtractor()


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _manifest_client_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., httpx.AsyncClient]:
    """Stand-in for ``httpx.AsyncClient`` backed by a hermetic ``MockTransport``.

    ``extract_manifest`` fetches under a byte cap and so goes through
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


def _json_handler(payload: object) -> Callable[[httpx.Request], httpx.Response]:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handle


class TestExtractManifest:
    """Test extract_manifest method."""

    @pytest.mark.asyncio
    async def test_no_manifest_link(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup("<html><head></head></html>")
        color, icons = await extractor.extract_manifest(soup, "https://example.com")
        assert color == ""
        assert icons == []

    @pytest.mark.asyncio
    async def test_manifest_link_no_href(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="manifest" href="">')
        color, icons = await extractor.extract_manifest(soup, "https://example.com")
        assert color == ""
        assert icons == []

    @pytest.mark.asyncio
    async def test_manifest_success(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="manifest" href="/manifest.json">')

        manifest_data = {
            "theme_color": "#FF5733",
            "icons": [
                {"src": "/icon-192.png", "sizes": "192x192"},
                {"src": "/icon-512.png", "sizes": "512x512"},
            ],
        }

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _manifest_client_factory(_json_handler(manifest_data)),
        ):
            color, icons = await extractor.extract_manifest(soup, "https://example.com")
            assert color == "#FF5733"
            assert len(icons) == 2

    @pytest.mark.asyncio
    async def test_manifest_fetch_failure(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="manifest" href="/manifest.json">')

        def handle(_request: httpx.Request) -> httpx.Response:
            raise OSError("Network error")

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _manifest_client_factory(handle),
        ):
            color, icons = await extractor.extract_manifest(soup, "https://example.com")
            assert color == ""
            assert icons == []

    @pytest.mark.asyncio
    async def test_manifest_no_icons(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="manifest" href="/manifest.json">')

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _manifest_client_factory(_json_handler({"theme_color": "#333333"})),
        ):
            color, icons = await extractor.extract_manifest(soup, "https://example.com")
            assert color == "#333333"
            assert icons == []

    @pytest.mark.asyncio
    async def test_manifest_empty_theme_color(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="manifest" href="/manifest.json">')

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _manifest_client_factory(_json_handler({"name": "My App"})),
        ):
            color, _icons = await extractor.extract_manifest(soup, "https://example.com")
            assert color == ""


class TestExtractFaviconsEdge:
    def test_favicon_with_no_href(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="icon" href="">')
        result = extractor.extract_favicons(soup, "https://example.com")
        assert result == []

    def test_shortcut_icon(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="shortcut icon" href="/shortcut.ico">')
        result = extractor.extract_favicons(soup, "https://example.com")
        assert len(result) == 1

    def test_unknown_size_sorted_last(self, extractor: MetadataExtractor) -> None:
        html = """
        <link rel="icon" href="/unknown.ico">
        <link rel="icon" sizes="32x32" href="/32.png">
        """
        soup = _make_soup(html)
        result = extractor.extract_favicons(soup, "https://example.com")
        assert result[0].endswith("/32.png")
        assert result[-1].endswith("/unknown.ico")

    def test_non_icon_rel_skipped(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="stylesheet" href="/style.css">')
        result = extractor.extract_favicons(soup, "https://example.com")
        assert result == []


class TestExtractSocialImagesEdge:
    def test_twitter_property_fallback(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<meta property="twitter:image" content="https://example.com/tw.png">')
        result = extractor.extract_social_images(soup)
        assert result.twitter_image == "https://example.com/tw.png"

    def test_apple_touch_icon(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="apple-touch-icon" href="/apple-touch.png">')
        result = extractor.extract_social_images(soup)
        assert result.apple_touch_icon == "/apple-touch.png"


class TestExtractSchemaOrgEdge:
    def test_corporation_type(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "Corporation", "name": "Corp Inc", "url": "https://corp.com"}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.type == "Corporation"

    def test_local_business_type(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "LocalBusiness", "name": "Pizza Shop", "url": "https://pizza.com"}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.type == "LocalBusiness"

    def test_type_as_list(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": ["Organization", "Corporation"], "name": "Multi", "url": "https://multi.com"}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.type == "Organization"

    def test_same_as_string(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "Organization", "name": "Test", "sameAs": "https://twitter.com/test"}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert "https://twitter.com/test" in result.same_as

    def test_empty_script_tag(self, extractor: MetadataExtractor) -> None:
        html = '<script type="application/ld+json"></script>'
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is None

    def test_skips_non_relevant_types(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "Product", "name": "Widget"}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is None

    def test_list_format(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        [{"@type": "Organization", "name": "First"}, {"@type": "Product", "name": "Second"}]
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.name == "First"

    def test_non_dict_items_skipped(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        ["not a dict", {"@type": "Organization", "name": "Real"}]
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.name == "Real"


class TestExtractFontsFromHtmlEdge:
    def test_google_fonts_with_weights(self, extractor: MetadataExtractor) -> None:
        html = '<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;700" rel="stylesheet">'
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        roboto = next((f for f in result if f.family == "Roboto"), None)
        assert roboto is not None
        assert "400" in roboto.weights
        assert "700" in roboto.weights

    def test_multiple_google_fonts_families(self, extractor: MetadataExtractor) -> None:
        html = '<link href="https://fonts.googleapis.com/css2?family=Roboto&family=Open+Sans" rel="stylesheet">'
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        families = [f.family for f in result]
        assert "Roboto" in families
        assert "Open Sans" in families

    def test_dedup_fonts(self, extractor: MetadataExtractor) -> None:
        html = """
        <link href="https://fonts.googleapis.com/css2?family=Roboto" rel="stylesheet">
        <style>body { font-family: 'Roboto', sans-serif; }</style>
        """
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        roboto_count = sum(1 for f in result if f.family == "Roboto")
        assert roboto_count == 1

    def test_categorize_font_monospace(self) -> None:
        from src.domains.brand.services.css_parser import _categorize_font

        assert _categorize_font("Fira Code") == "monospace"

    def test_categorize_font_serif(self) -> None:
        from src.domains.brand.services.css_parser import _categorize_font

        assert _categorize_font("Georgia") == "serif"

    def test_categorize_font_sans_serif(self) -> None:
        from src.domains.brand.services.css_parser import _categorize_font

        # "sans" in name prevents it from being serif
        assert _categorize_font("Open Sans") == "sans-serif"

    def test_categorize_font_default(self) -> None:
        from src.domains.brand.services.css_parser import _categorize_font

        assert _categorize_font("Inter") == "sans-serif"

    def test_non_link_tag_skipped(self, extractor: MetadataExtractor) -> None:
        html = '<div href="https://fonts.googleapis.com/css2?family=Roboto">Not a link</div>'
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        assert len(result) == 0

    def test_empty_font_family_in_style(self, extractor: MetadataExtractor) -> None:
        html = "<style>body { font-family: ; }</style>"
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        assert result == []
