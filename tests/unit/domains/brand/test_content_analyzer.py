"""Tests for content_analyzer."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.content_analyzer import ContentAnalyzer
from tests.fixtures.html_content import (
    CASUAL_CONTENT,
    COMPLEX_READING,
    CORPORATE_CONTENT,
    ECOMMERCE_CONTENT,
    HERO_TAGLINE,
    LONG_TAGLINE,
    META_TAGLINE_ONLY,
    MINIMAL_CONTENT,
    TECHNICAL_CONTENT,
)


@pytest.fixture
def analyzer() -> ContentAnalyzer:
    return ContentAnalyzer()


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TestExtractTagline:
    def test_extract_tagline_from_hero(self, analyzer: ContentAnalyzer) -> None:
        """Hero h1 extracted as tagline with correct source."""
        soup = _make_soup(HERO_TAGLINE)
        tagline, source = analyzer._extract_tagline(soup)
        assert tagline == "Build better products, faster"
        assert source == "hero_h1"

    def test_extract_tagline_from_meta(self, analyzer: ContentAnalyzer) -> None:
        """Fallback to meta description when h1 is too long."""
        soup = _make_soup(META_TAGLINE_ONLY)
        tagline, source = analyzer._extract_tagline(soup)
        assert tagline == "Simple, powerful tools"
        assert source == "meta_description"

    def test_extract_tagline_from_og(self, analyzer: ContentAnalyzer) -> None:
        """Fallback to og:description when no h1 or meta description qualify."""
        html = """<!DOCTYPE html>
<html><head><title>Test</title>
<meta property="og:description" content="Great products for you">
</head><body>
<h1>This heading is way too long to be a tagline because it exceeds the one hundred and twenty character limit that we set for taglines in the content analyzer service</h1>
</body></html>
"""
        soup = _make_soup(html)
        tagline, source = analyzer._extract_tagline(soup)
        assert tagline == "Great products for you"
        assert source == "og_description"

    def test_tagline_too_long_skipped(self, analyzer: ContentAnalyzer) -> None:
        """Hero h1 exceeding 120 chars is skipped."""
        soup = _make_soup(LONG_TAGLINE)
        tagline, source = analyzer._extract_tagline(soup)
        # The hero h1 is >120 chars, no meta/og fallback, so no tagline
        assert tagline == ""
        assert source == ""

    def test_tagline_source_tracking(self, analyzer: ContentAnalyzer) -> None:
        """tagline_source field correctly identifies the source."""
        soup = _make_soup(HERO_TAGLINE)
        analysis = analyzer.extract_content_analysis(soup)
        assert analysis.tagline_source == "hero_h1"
        assert analysis.tagline == "Build better products, faster"


class TestAnalyzeBrandVoice:
    def test_brand_voice_technical(self, analyzer: ContentAnalyzer) -> None:
        """Technical content classified as technical voice."""
        soup = _make_soup(TECHNICAL_CONTENT)
        texts = analyzer._get_visible_text(soup)
        voice, _confidence = analyzer._analyze_brand_voice(texts)
        assert voice == "technical"

    def test_brand_voice_casual(self, analyzer: ContentAnalyzer) -> None:
        """Casual content classified as casual voice."""
        soup = _make_soup(CASUAL_CONTENT)
        texts = analyzer._get_visible_text(soup)
        voice, _confidence = analyzer._analyze_brand_voice(texts)
        assert voice == "casual"

    def test_brand_voice_corporate(self, analyzer: ContentAnalyzer) -> None:
        """Corporate content classified as corporate voice."""
        soup = _make_soup(CORPORATE_CONTENT)
        texts = analyzer._get_visible_text(soup)
        voice, _confidence = analyzer._analyze_brand_voice(texts)
        assert voice == "corporate"

    def test_brand_voice_confidence(self, analyzer: ContentAnalyzer) -> None:
        """Confidence is > 0.5 for content with clear voice signals."""
        soup = _make_soup(TECHNICAL_CONTENT)
        texts = analyzer._get_visible_text(soup)
        _voice, confidence = analyzer._analyze_brand_voice(texts)
        assert confidence > 0.5

    def test_brand_voice_default(self, analyzer: ContentAnalyzer) -> None:
        """No signals returns professional with 0.0 confidence."""
        voice, confidence = analyzer._analyze_brand_voice(["hello world nothing here"])
        assert voice == "professional"
        assert confidence == 0.0


class TestExtractKeyMessages:
    def test_key_messages_extracted(self, analyzer: ContentAnalyzer) -> None:
        """h1/h2/h3 text is collected as key messages."""
        soup = _make_soup(HERO_TAGLINE)
        messages = analyzer._extract_key_messages(soup)
        assert "Build better products, faster" in messages
        assert "Features" in messages
        assert "Real-time collaboration" in messages
        assert "Cloud deployment" in messages

    def test_key_messages_filter_nav(self, analyzer: ContentAnalyzer) -> None:
        """Headings inside <nav> are excluded."""
        soup = _make_soup(HERO_TAGLINE)
        messages = analyzer._extract_key_messages(soup)
        assert "Menu" not in messages

    def test_key_messages_filter_footer(self, analyzer: ContentAnalyzer) -> None:
        """Headings inside <footer> are excluded."""
        soup = _make_soup(HERO_TAGLINE)
        messages = analyzer._extract_key_messages(soup)
        assert "Company" not in messages

    def test_key_messages_limit(self, analyzer: ContentAnalyzer) -> None:
        """Key messages are limited to 10."""
        headings = "".join(f"<h2>Message {i}</h2>" for i in range(20))
        html = f"<!DOCTYPE html><html><head></head><body>{headings}</body></html>"
        soup = _make_soup(html)
        messages = analyzer._extract_key_messages(soup)
        assert len(messages) == 10


class TestInferTargetAudience:
    def test_target_audience_developers(self, analyzer: ContentAnalyzer) -> None:
        """Developer keywords produce 'developers' audience."""
        soup = _make_soup(TECHNICAL_CONTENT)
        texts = analyzer._get_visible_text(soup)
        audiences = analyzer._infer_target_audience(texts, soup)
        assert "developers" in audiences

    def test_target_audience_consumers(self, analyzer: ContentAnalyzer) -> None:
        """Ecommerce keywords produce 'consumers' audience."""
        soup = _make_soup(ECOMMERCE_CONTENT)
        texts = analyzer._get_visible_text(soup)
        audiences = analyzer._infer_target_audience(texts, soup)
        assert "consumers" in audiences

    def test_target_audience_multiple(self, analyzer: ContentAnalyzer) -> None:
        """Content with multiple audience signals returns multiple audiences."""
        html = """<!DOCTYPE html>
<html><head></head><body>
<p>Our enterprise API and SDK helps developers build applications.
Shop our developer tools. Buy now for your organization.</p>
</body></html>
"""
        soup = _make_soup(html)
        texts = analyzer._get_visible_text(soup)
        audiences = analyzer._infer_target_audience(texts, soup)
        assert "developers" in audiences
        assert "enterprise" in audiences
        assert "consumers" in audiences


class TestEstimateReadingLevel:
    def test_reading_level_simple(self, analyzer: ContentAnalyzer) -> None:
        """Short sentences with simple words are classified as simple."""
        text = "This is fun. We like it. Try it now. Go for it."
        level = analyzer._estimate_reading_level(text)
        assert level == "simple"

    def test_reading_level_complex(self, analyzer: ContentAnalyzer) -> None:
        """Long sentences with long words are classified as complex."""
        soup = _make_soup(COMPLEX_READING)
        texts = analyzer._get_visible_text(soup)
        combined = " ".join(texts)
        level = analyzer._estimate_reading_level(combined)
        assert level == "complex"

    def test_reading_level_moderate(self, analyzer: ContentAnalyzer) -> None:
        """Moderate content is classified correctly."""
        text = (
            "The platform provides comprehensive tools for building applications. "
            "Users can integrate services quickly."
        )
        level = analyzer._estimate_reading_level(text)
        assert level == "moderate"


class TestWordCountAndFullExtraction:
    def test_word_count(self, analyzer: ContentAnalyzer) -> None:
        """word_count is populated with the correct count of visible words."""
        soup = _make_soup(MINIMAL_CONTENT)
        analysis = analyzer.extract_content_analysis(soup)
        # "Hello world." -> 2 words
        assert analysis.word_count == 2

    def test_full_extraction(self, analyzer: ContentAnalyzer) -> None:
        """All fields are populated from complete HTML."""
        soup = _make_soup(HERO_TAGLINE)
        analysis = analyzer.extract_content_analysis(soup)

        # Tagline from hero h1
        assert analysis.tagline == "Build better products, faster"
        assert analysis.tagline_source == "hero_h1"

        # Brand voice should be detected (not default)
        assert analysis.brand_voice != ""

        # Key messages should include headings
        assert len(analysis.key_messages) > 0
        assert "Build better products, faster" in analysis.key_messages

        # Word count is positive
        assert analysis.word_count > 0

        # Reading level is set
        assert analysis.reading_level in ("simple", "moderate", "complex")

        # to_dict round trip
        d = analysis.to_dict()
        assert d["tagline"] == "Build better products, faster"
        assert d["tagline_source"] == "hero_h1"
        assert d["word_count"] > 0
        assert isinstance(d["key_messages"], list)
        assert isinstance(d["target_audience_hints"], list)
        assert d["brand_voice_confidence"] >= 0.0
