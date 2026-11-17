"""Unit tests for MetadataExtractor."""

from __future__ import annotations

from bs4 import BeautifulSoup

from src.domains.analyze.services.metadata_extractor import MetadataExtractor


def _soup(html: str) -> BeautifulSoup:
    """Helper to parse HTML with lxml."""
    return BeautifulSoup(html, "lxml")


# --- OG Tags ---


class TestExtractOgTags:
    def test_single_og_tag(self) -> None:
        html = (
            '<html><head><meta property="og:title" content="My Title"></head><body></body></html>'
        )
        result = MetadataExtractor._extract_og_tags(_soup(html))
        assert result == {"title": "My Title"}

    def test_multiple_og_tags(self) -> None:
        html = """
        <html><head>
            <meta property="og:title" content="My Title">
            <meta property="og:description" content="My Description">
            <meta property="og:image" content="https://example.com/img.jpg">
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_og_tags(_soup(html))
        assert result == {
            "title": "My Title",
            "description": "My Description",
            "image": "https://example.com/img.jpg",
        }

    def test_no_og_tags(self) -> None:
        html = "<html><head></head><body></body></html>"
        result = MetadataExtractor._extract_og_tags(_soup(html))
        assert result == {}

    def test_non_og_meta_properties_ignored(self) -> None:
        html = """
        <html><head>
            <meta property="og:title" content="OG Title">
            <meta property="article:author" content="John">
            <meta property="fb:app_id" content="12345">
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_og_tags(_soup(html))
        assert result == {"title": "OG Title"}
        assert "article:author" not in result
        assert "fb:app_id" not in result

    def test_og_tag_empty_content(self) -> None:
        html = '<html><head><meta property="og:title" content=""></head><body></body></html>'
        result = MetadataExtractor._extract_og_tags(_soup(html))
        assert result == {"title": ""}


# --- JSON-LD ---


class TestExtractJsonLd:
    def test_single_json_ld_object(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json">
            {"@type": "Organization", "name": "Acme"}
            </script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert len(result) == 1
        assert result[0]["@type"] == "Organization"
        assert result[0]["name"] == "Acme"

    def test_multiple_json_ld_objects(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json">{"@type": "Organization", "name": "Acme"}</script>
            <script type="application/ld+json">{"@type": "WebSite", "url": "https://acme.com"}</script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert len(result) == 2
        types = [d["@type"] for d in result]
        assert "Organization" in types
        assert "WebSite" in types

    def test_json_ld_array_format(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json">
            [{"@type": "Organization", "name": "Acme"}, {"@type": "WebSite", "url": "https://acme.com"}]
            </script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert len(result) == 2

    def test_json_ld_invalid_json(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json">
            {not valid json!!!}
            </script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert result == []

    def test_json_ld_empty_script(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json"></script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert result == []

    def test_json_ld_whitespace_only_script(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json">   \n  </script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert result == []

    def test_json_ld_array_filters_non_dicts(self) -> None:
        html = """
        <html><head>
            <script type="application/ld+json">
            [{"@type": "Thing"}, "not a dict", 42]
            </script>
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_json_ld(_soup(html))
        assert len(result) == 1
        assert result[0]["@type"] == "Thing"


# --- Canonical URL ---


class TestExtractCanonical:
    def test_canonical_present(self) -> None:
        html = '<html><head><link rel="canonical" href="https://example.com/page"></head><body></body></html>'
        result = MetadataExtractor._extract_canonical(_soup(html))
        assert result == "https://example.com/page"

    def test_canonical_absent(self) -> None:
        html = "<html><head></head><body></body></html>"
        result = MetadataExtractor._extract_canonical(_soup(html))
        assert result == ""

    def test_multiple_canonical_first_wins(self) -> None:
        html = """
        <html><head>
            <link rel="canonical" href="https://example.com/first">
            <link rel="canonical" href="https://example.com/second">
        </head><body></body></html>
        """
        result = MetadataExtractor._extract_canonical(_soup(html))
        assert result == "https://example.com/first"


# --- Language ---


class TestExtractLanguage:
    def test_language_present(self) -> None:
        html = '<html lang="en"><head></head><body></body></html>'
        result = MetadataExtractor._extract_language(_soup(html))
        assert result == "en"

    def test_language_missing(self) -> None:
        html = "<html><head></head><body></body></html>"
        result = MetadataExtractor._extract_language(_soup(html))
        assert result == ""

    def test_language_with_region(self) -> None:
        html = '<html lang="en-US"><head></head><body></body></html>'
        result = MetadataExtractor._extract_language(_soup(html))
        assert result == "en-US"


# --- Robots ---


class TestExtractRobots:
    def test_robots_noindex(self) -> None:
        html = '<html><head><meta name="robots" content="noindex"></head><body></body></html>'
        result = MetadataExtractor._extract_robots(_soup(html))
        assert result == {"noindex": True, "nofollow": False}

    def test_robots_nofollow(self) -> None:
        html = '<html><head><meta name="robots" content="nofollow"></head><body></body></html>'
        result = MetadataExtractor._extract_robots(_soup(html))
        assert result == {"noindex": False, "nofollow": True}

    def test_robots_both(self) -> None:
        html = (
            '<html><head><meta name="robots" content="noindex, nofollow">'
            "</head><body></body></html>"
        )
        result = MetadataExtractor._extract_robots(_soup(html))
        assert result == {"noindex": True, "nofollow": True}

    def test_robots_absent(self) -> None:
        html = "<html><head></head><body></body></html>"
        result = MetadataExtractor._extract_robots(_soup(html))
        assert result == {"noindex": False, "nofollow": False}

    def test_robots_index_follow(self) -> None:
        html = '<html><head><meta name="robots" content="index, follow"></head><body></body></html>'
        result = MetadataExtractor._extract_robots(_soup(html))
        assert result == {"noindex": False, "nofollow": False}


# --- Full extract() integration ---


class TestExtractIntegration:
    def test_full_extract_complete_html(self) -> None:
        html = """
        <html lang="fr">
        <head>
            <meta property="og:title" content="Mon Site">
            <meta property="og:description" content="Description du site">
            <meta name="robots" content="noindex">
            <link rel="canonical" href="https://example.fr/accueil">
            <script type="application/ld+json">
            {"@type": "WebSite", "name": "Mon Site", "url": "https://example.fr"}
            </script>
        </head>
        <body><h1>Bienvenue</h1></body>
        </html>
        """
        result = MetadataExtractor.extract(_soup(html))
        assert result["og_tags"] == {"title": "Mon Site", "description": "Description du site"}
        assert len(result["json_ld"]) == 1
        assert result["json_ld"][0]["@type"] == "WebSite"
        assert result["canonical_url"] == "https://example.fr/accueil"
        assert result["language"] == "fr"
        assert result["robots"]["noindex"] is True
        assert result["robots"]["nofollow"] is False

    def test_empty_html(self) -> None:
        html = "<html><head></head><body></body></html>"
        result = MetadataExtractor.extract(_soup(html))
        assert result["og_tags"] == {}
        assert result["json_ld"] == []
        assert result["canonical_url"] == ""
        assert result["language"] == ""
        assert result["robots"] == {"noindex": False, "nofollow": False}

    def test_minimal_html(self) -> None:
        html = "<html></html>"
        result = MetadataExtractor.extract(_soup(html))
        assert result["og_tags"] == {}
        assert result["json_ld"] == []
        assert result["canonical_url"] == ""
        assert result["language"] == ""

    def test_html_no_head_section(self) -> None:
        html = "<html><body><p>Just body content</p></body></html>"
        result = MetadataExtractor.extract(_soup(html))
        assert result["og_tags"] == {}
        assert result["json_ld"] == []
        assert result["canonical_url"] == ""
        assert result["language"] == ""
        assert result["robots"] == {"noindex": False, "nofollow": False}

    def test_unicode_content(self) -> None:
        html = """
        <html lang="ja">
        <head>
            <meta property="og:title" content="\u6771\u4eac\u30bf\u30ef\u30fc">
            <meta property="og:description" content="\u65e5\u672c\u306e\u89b3\u5149\u540d\u6240">
            <link rel="canonical" href="https://example.jp/\u6771\u4eac">
        </head>
        <body></body>
        </html>
        """
        result = MetadataExtractor.extract(_soup(html))
        assert result["og_tags"]["title"] == "\u6771\u4eac\u30bf\u30ef\u30fc"
        assert result["og_tags"]["description"] == "\u65e5\u672c\u306e\u89b3\u5149\u540d\u6240"
        assert result["language"] == "ja"
        assert "\u6771\u4eac" in result["canonical_url"]
