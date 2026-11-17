"""Tests for social media profile extractor."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.social_extractor import SocialExtractor, SocialProfiles
from tests.fixtures.html_social import (
    SOCIAL_ALL_PLATFORMS,
    SOCIAL_FOOTER,
    SOCIAL_NAV_SECTION,
    SOCIAL_NO_PROFILES,
    SOCIAL_OTHER_PLATFORMS,
    SOCIAL_SCHEMA_ORG,
    SOCIAL_WITH_SHARE_LINKS,
    SOCIAL_X_DOMAIN,
)


@pytest.fixture
def extractor() -> SocialExtractor:
    return SocialExtractor()


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TestExtractSocialProfiles:
    def test_extract_from_footer(self, extractor: SocialExtractor) -> None:
        """Basic footer extraction finds facebook/twitter/linkedin/instagram/youtube."""
        soup = _make_soup(SOCIAL_FOOTER)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.facebook == "https://facebook.com/acmecorp"
        assert profiles.twitter == "https://twitter.com/acmecorp"
        assert profiles.linkedin == "https://www.linkedin.com/company/acme-corp"
        assert profiles.instagram == "https://instagram.com/acmecorp"
        assert profiles.youtube == "https://youtube.com/c/acmecorp"
        assert profiles.has_any is True
        assert profiles.count == 5

    def test_filter_share_links(self, extractor: SocialExtractor) -> None:
        """Share/intent links are filtered out; actual profile links are kept."""
        soup = _make_soup(SOCIAL_WITH_SHARE_LINKS)
        profiles = extractor.extract_social_profiles(soup)

        # Profile links from footer should be found
        assert profiles.twitter == "https://twitter.com/realacme"
        assert profiles.facebook == "https://facebook.com/realacme"

        # Share links should NOT populate linkedin or pinterest
        assert profiles.linkedin == ""
        assert profiles.pinterest == ""

    def test_x_domain_recognized(self, extractor: SocialExtractor) -> None:
        """x.com URLs map to the twitter field."""
        soup = _make_soup(SOCIAL_X_DOMAIN)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.twitter == "https://x.com/acmecorp"
        assert profiles.github == "https://github.com/acmecorp"

    def test_all_platforms(self, extractor: SocialExtractor) -> None:
        """All 9 known platforms are populated when present."""
        soup = _make_soup(SOCIAL_ALL_PLATFORMS)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.facebook == "https://facebook.com/acme"
        assert profiles.twitter == "https://twitter.com/acme"
        assert profiles.linkedin == "https://linkedin.com/company/acme"
        assert profiles.instagram == "https://instagram.com/acme"
        assert profiles.youtube == "https://youtube.com/acme"
        assert profiles.tiktok == "https://tiktok.com/@acme"
        assert profiles.github == "https://github.com/acme"
        assert profiles.pinterest == "https://pinterest.com/acme"
        assert profiles.threads == "https://threads.net/@acme"
        assert profiles.count == 9

    def test_no_profiles_found(self, extractor: SocialExtractor) -> None:
        """Empty HTML with no social links returns empty SocialProfiles."""
        soup = _make_soup(SOCIAL_NO_PROFILES)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.has_any is False
        assert profiles.count == 0
        assert profiles.facebook == ""
        assert profiles.twitter == ""

    def test_schema_org_same_as(self, extractor: SocialExtractor) -> None:
        """Profiles are parsed from schema.org sameAs list."""
        soup = _make_soup(SOCIAL_SCHEMA_ORG)
        schema_org = {
            "same_as": [
                "https://twitter.com/acme",
                "https://facebook.com/acme",
                "https://linkedin.com/company/acme",
            ]
        }
        profiles = extractor.extract_social_profiles(soup, schema_org=schema_org)

        assert profiles.twitter == "https://twitter.com/acme"
        assert profiles.facebook == "https://facebook.com/acme"
        assert profiles.linkedin == "https://linkedin.com/company/acme"
        assert profiles.count == 3

    def test_social_nav_section(self, extractor: SocialExtractor) -> None:
        """Profiles extracted from nav elements with social-related class."""
        soup = _make_soup(SOCIAL_NAV_SECTION)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.instagram == "https://instagram.com/brandx"
        assert profiles.tiktok == "https://tiktok.com/@brandx"
        assert profiles.count == 2

    def test_other_platforms(self, extractor: SocialExtractor) -> None:
        """Mastodon and Discord (unknown platforms) do not populate named fields.

        Only the known twitter link is captured from the footer.
        """
        soup = _make_soup(SOCIAL_OTHER_PLATFORMS)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.twitter == "https://twitter.com/acme"
        # mastodon.social and discord.gg are not in the known platform map,
        # so they won't be captured by _extract_from_container
        # (only schema_org "other" adds to the other list)
        assert profiles.count >= 1


class TestParseHandle:
    def test_parse_handle_twitter(self, extractor: SocialExtractor) -> None:
        """Twitter handle parsed from URL."""
        handle = extractor._parse_handle("https://twitter.com/acme", "twitter")
        assert handle == "@acme"

    def test_parse_handle_instagram(self, extractor: SocialExtractor) -> None:
        """Instagram handle parsed from URL."""
        handle = extractor._parse_handle("https://instagram.com/acme", "instagram")
        assert handle == "@acme"

    def test_parse_handle_tiktok(self, extractor: SocialExtractor) -> None:
        """TikTok handle parsed from URL with @ in path."""
        handle = extractor._parse_handle("https://tiktok.com/@acme", "tiktok")
        assert handle == "@acme"

    def test_parse_handle_linkedin_company(self, extractor: SocialExtractor) -> None:
        """LinkedIn company handle parsed correctly (no @ prefix)."""
        handle = extractor._parse_handle("https://linkedin.com/company/acme", "linkedin")
        assert handle == "acme"

    def test_parse_handle_youtube_channel(self, extractor: SocialExtractor) -> None:
        """YouTube /c/ handle parsed correctly (no @ prefix)."""
        handle = extractor._parse_handle("https://youtube.com/c/acmecorp", "youtube")
        assert handle == "acmecorp"

    def test_parse_handle_empty_path(self, extractor: SocialExtractor) -> None:
        """Empty path returns empty handle."""
        handle = extractor._parse_handle("https://twitter.com/", "twitter")
        assert handle == ""

    def test_parse_handle_x_domain(self, extractor: SocialExtractor) -> None:
        """x.com handle parsed the same as twitter."""
        handle = extractor._parse_handle("https://x.com/acmecorp", "twitter")
        assert handle == "@acmecorp"


class TestIsShareLink:
    def test_is_share_link_true(self, extractor: SocialExtractor) -> None:
        """Share/intent URLs correctly identified."""
        assert extractor._is_share_link("https://twitter.com/intent/tweet?text=Hello") is True
        assert extractor._is_share_link("https://www.facebook.com/sharer/sharer.php?u=x") is True
        assert extractor._is_share_link("https://www.linkedin.com/shareArticle?url=x") is True
        assert extractor._is_share_link("https://pinterest.com/pin/create/button/?url=x") is True

    def test_is_share_link_false(self, extractor: SocialExtractor) -> None:
        """Profile URLs are not flagged as share links."""
        assert extractor._is_share_link("https://twitter.com/acme") is False
        assert extractor._is_share_link("https://facebook.com/acme") is False
        assert extractor._is_share_link("https://linkedin.com/company/acme") is False
        assert extractor._is_share_link("https://pinterest.com/acme") is False


class TestSocialProfilesDataclass:
    def test_social_profiles_has_any_true(self) -> None:
        """has_any is True when at least one field is populated."""
        profiles = SocialProfiles(twitter="https://twitter.com/acme")
        assert profiles.has_any is True

    def test_social_profiles_has_any_false(self) -> None:
        """has_any is False when all fields are empty."""
        profiles = SocialProfiles()
        assert profiles.has_any is False

    def test_social_profiles_has_any_other_only(self) -> None:
        """has_any is True when only `other` list is populated."""
        profiles = SocialProfiles(other=["https://mastodon.social/@acme"])
        assert profiles.has_any is True

    def test_social_profiles_count(self) -> None:
        """count matches the number of populated named fields plus other entries."""
        profiles = SocialProfiles(
            facebook="https://facebook.com/acme",
            twitter="https://twitter.com/acme",
            other=["https://mastodon.social/@acme"],
        )
        assert profiles.count == 3

    def test_social_profiles_count_empty(self) -> None:
        """count is 0 for empty profiles."""
        profiles = SocialProfiles()
        assert profiles.count == 0

    def test_social_profiles_to_dict(self) -> None:
        """to_dict serializes all fields correctly."""
        profiles = SocialProfiles(
            facebook="https://facebook.com/acme",
            twitter="https://twitter.com/acme",
            other=["https://mastodon.social/@acme"],
        )
        d = profiles.to_dict()
        assert d["facebook"] == "https://facebook.com/acme"
        assert d["twitter"] == "https://twitter.com/acme"
        assert d["linkedin"] == ""
        assert d["instagram"] == ""
        assert d["youtube"] == ""
        assert d["tiktok"] == ""
        assert d["github"] == ""
        assert d["pinterest"] == ""
        assert d["threads"] == ""
        assert d["other"] == ["https://mastodon.social/@acme"]


class TestEdgeCases:
    def test_schema_org_overrides_html(self, extractor: SocialExtractor) -> None:
        """Schema.org data merged with HTML findings; HTML takes precedence (found first)."""
        html = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <a href="https://twitter.com/htmlhandle">Twitter</a>
</footer>
</body></html>
"""
        soup = _make_soup(html)
        schema_org = {
            "same_as": [
                "https://twitter.com/schemahandle",
                "https://facebook.com/schemafb",
            ]
        }
        profiles = extractor.extract_social_profiles(soup, schema_org=schema_org)

        # HTML is scanned first, so the twitter from footer takes precedence
        assert profiles.twitter == "https://twitter.com/htmlhandle"
        # Facebook only in schema.org, so it gets added from there
        assert profiles.facebook == "https://facebook.com/schemafb"

    def test_duplicate_platforms_deduped(self, extractor: SocialExtractor) -> None:
        """Same platform from multiple sources keeps the first occurrence."""
        html = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <a href="https://twitter.com/first">Twitter 1</a>
  <a href="https://twitter.com/second">Twitter 2</a>
</footer>
</body></html>
"""
        soup = _make_soup(html)
        profiles = extractor.extract_social_profiles(soup)

        # First link encountered wins
        assert profiles.twitter == "https://twitter.com/first"

    def test_empty_soup(self, extractor: SocialExtractor) -> None:
        """Empty BeautifulSoup returns empty profiles."""
        soup = BeautifulSoup("", "html.parser")
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.has_any is False
        assert profiles.count == 0

    def test_www_prefix_handled(self, extractor: SocialExtractor) -> None:
        """www.facebook.com is still classified as facebook."""
        html = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <a href="https://www.facebook.com/acme">Facebook</a>
  <a href="https://www.twitter.com/acme">Twitter</a>
  <a href="https://www.instagram.com/acme">Instagram</a>
</footer>
</body></html>
"""
        soup = _make_soup(html)
        profiles = extractor.extract_social_profiles(soup)

        assert profiles.facebook == "https://www.facebook.com/acme"
        assert profiles.twitter == "https://www.twitter.com/acme"
        assert profiles.instagram == "https://www.instagram.com/acme"
        assert profiles.count == 3
