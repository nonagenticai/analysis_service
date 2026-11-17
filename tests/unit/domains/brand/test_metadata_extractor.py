"""Tests for metadata_extractor."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.metadata_extractor import MetadataExtractor


@pytest.fixture
def extractor() -> MetadataExtractor:
    return MetadataExtractor()


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TestExtractFavicons:
    def test_basic_favicon(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="icon" href="/favicon.ico">')
        result = extractor.extract_favicons(soup, "https://example.com")
        assert "https://example.com/favicon.ico" in result

    def test_apple_touch_icon(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup(
            '<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">'
        )
        result = extractor.extract_favicons(soup, "https://example.com")
        assert "https://example.com/apple-touch-icon.png" in result

    def test_sorted_by_size(self, extractor: MetadataExtractor) -> None:
        html = """
        <link rel="icon" sizes="16x16" href="/small.png">
        <link rel="icon" sizes="192x192" href="/large.png">
        <link rel="icon" sizes="32x32" href="/medium.png">
        """
        soup = _make_soup(html)
        result = extractor.extract_favicons(soup, "https://example.com")
        assert result[0].endswith("/large.png")

    def test_no_favicons(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup("<html><head></head></html>")
        result = extractor.extract_favicons(soup, "https://example.com")
        assert result == []

    def test_resolves_relative_urls(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<link rel="icon" href="favicon.ico">')
        result = extractor.extract_favicons(soup, "https://example.com/page/")
        assert result[0].startswith("https://")


class TestExtractSocialImages:
    def test_og_image(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<meta property="og:image" content="https://example.com/og.png">')
        result = extractor.extract_social_images(soup)
        assert result.og_image == "https://example.com/og.png"

    def test_twitter_image(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup('<meta name="twitter:image" content="https://example.com/tw.png">')
        result = extractor.extract_social_images(soup)
        assert result.twitter_image == "https://example.com/tw.png"

    def test_no_images(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup("<html><head></head></html>")
        result = extractor.extract_social_images(soup)
        assert result.og_image == ""
        assert result.twitter_image == ""


class TestExtractSchemaOrg:
    def test_organization(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "Organization", "name": "Acme Corp", "url": "https://acme.com", "logo": "https://acme.com/logo.png"}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.type == "Organization"
        assert result.name == "Acme Corp"
        assert result.logo == "https://acme.com/logo.png"

    def test_organization_with_logo_object(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@type": "Organization", "name": "Test", "logo": {"@type": "ImageObject", "url": "https://test.com/logo.png"}}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.logo == "https://test.com/logo.png"

    def test_graph_array(self, extractor: MetadataExtractor) -> None:
        html = """
        <script type="application/ld+json">
        {"@graph": [{"@type": "WebSite", "name": "My Site", "url": "https://example.com"}]}
        </script>
        """
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is not None
        assert result.type == "WebSite"

    def test_no_schema_org(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup("<html><head></head></html>")
        result = extractor.extract_schema_org(soup)
        assert result is None

    def test_invalid_json(self, extractor: MetadataExtractor) -> None:
        html = '<script type="application/ld+json">not valid json</script>'
        soup = _make_soup(html)
        result = extractor.extract_schema_org(soup)
        assert result is None


class TestExtractFontsFromHtml:
    def test_google_fonts_link(self, extractor: MetadataExtractor) -> None:
        html = '<link href="https://fonts.googleapis.com/css2?family=Roboto&display=swap" rel="stylesheet">'
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        assert any(f.family == "Roboto" for f in result)
        assert any(f.is_google_font for f in result)

    def test_inline_style_font(self, extractor: MetadataExtractor) -> None:
        html = "<style>body { font-family: 'Inter', sans-serif; }</style>"
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        assert any(f.family == "Inter" for f in result)

    def test_skips_generic_fonts(self, extractor: MetadataExtractor) -> None:
        html = "<style>body { font-family: sans-serif; }</style>"
        soup = _make_soup(html)
        result = extractor.extract_fonts_from_html(soup)
        assert len(result) == 0

    def test_no_fonts(self, extractor: MetadataExtractor) -> None:
        soup = _make_soup("<html><head></head></html>")
        result = extractor.extract_fonts_from_html(soup)
        assert result == []
