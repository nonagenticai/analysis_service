"""Tests for brand tone analyzer."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.tone_analyzer import BrandToneAnalyzer, ToneResult


@pytest.fixture
def analyzer() -> BrandToneAnalyzer:
    return BrandToneAnalyzer()


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

PROFESSIONAL_HTML = """<!DOCTYPE html>
<html><head><title>Acme Corp</title></head>
<body>
<p>Our comprehensive enterprise methodology facilitates strategic governance
and compliance across stakeholder infrastructure. We implement robust paradigms
to optimize organizational synergy. Furthermore, our solutions leverage
cutting-edge frameworks to maximize enterprise value. Moreover, we utilize
data-driven approaches to facilitate stakeholder engagement. Consequently,
our strategic infrastructure delivers measurable governance outcomes.</p>
</body></html>
"""

CASUAL_HTML = """<!DOCTYPE html>
<html><head><title>Cool App</title></head>
<body>
<p>Hey, we're super excited you're here! We can't wait for you to try our
awesome new stuff. It's gonna be amazing! You won't believe how cool this is!
Don't miss out on the fun! We've got something totally incredible for you.
It's basically the best thing ever! You're gonna love it! We're not kidding,
it's literally insane! Wanna check it out? Let's go!</p>
</body></html>
"""

FRIENDLY_HTML = """<!DOCTYPE html>
<html><head><title>Community Hub</title></head>
<body>
<p>Welcome to our community. We believe in bringing people together and helping
you succeed. Have questions? We are here for you. Join us on this journey and
discover how we can help your team grow. What are you looking for today?
Our team is dedicated to supporting you every step of the way. We care about
your experience and want to make sure you feel at home here with us.</p>
</body></html>
"""

TECHNICAL_HTML = """<!DOCTYPE html>
<html><head><title>DevTools API</title></head>
<body>
<p>Deploy your microservice architecture using our SDK and framework. Configure
the API endpoint for authentication and encryption. Use the Docker and Kubernetes
pipeline for scalable deployment. Integrate the repository with the CI/CD pipeline.
Our protocol supports advanced algorithm optimization. The infrastructure
includes endpoint monitoring and integration testing across the deployment
pipeline with repository-level encryption.</p>
</body></html>
"""

PLAYFUL_HTML = """<!DOCTYPE html>
<html><head><title>Fun Zone</title></head>
<body>
<p>\U0001f680 Welcome to the fun zone! \U0001f389 Everything here is designed
to make you smile! \U0001f60d We have games, prizes, and surprises!
\U0001f973 Jump in and explore! \U0001f3ae Play with friends! \U0001f3c6
Win big rewards! \U0001f381 Open mystery boxes! \U0001f308 Every day is
an adventure! \U0001f525 Hot new challenges await! \U0001f31f Shine bright
and have a blast! \U0001f389 Party time starts now!!!</p>
</body></html>
"""

LUXURY_HTML = """<!DOCTYPE html>
<html><head><title>Maison Elegance</title></head>
<body>
<p>Discover our exclusive collection of bespoke, handcrafted timepieces.
Each exquisite piece is curated with impeccable attention to detail by our
distinguished artisans. Experience the refined elegance of our prestigious
luxury brand. Our sophisticated craftsmanship and premium materials create
distinguished pieces of unparalleled quality and luxurious design.</p>
</body></html>
"""

AUTHORITATIVE_HTML = """<!DOCTYPE html>
<html><head><title>Global Institute</title></head>
<body>
<p>The comprehensive methodology established hereby facilitates governance
compliance pursuant to strategic enterprise paradigms. Furthermore, the
infrastructure therein optimizes stakeholder synergy. Moreover, our
comprehensive framework implements strategic optimization across the enterprise.
Consequently, the governance methodology facilitates compliance and
infrastructure resilience. Nevertheless, the paradigm thereof ensures
stakeholder alignment henceforth. Start building your compliance framework today.</p>
</body></html>
"""

EMPTY_HTML = """<!DOCTYPE html>
<html><head><title>Empty</title></head>
<body></body></html>
"""

MINIMAL_HTML = """<!DOCTYPE html>
<html><head><title>Short</title></head>
<body><p>Hi there.</p></body></html>
"""

SCRIPT_STYLE_NAV_HTML = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<script>var x = "this should be removed";</script>
<style>.hidden { display: none; }</style>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<noscript>Enable JavaScript please.</noscript>
<main>
<p>This is the visible content that should remain after stripping.</p>
</main>
</body></html>
"""


# ---------------------------------------------------------------------------
# Tone classification tests
# ---------------------------------------------------------------------------


class TestToneClassification:
    def test_analyze_professional(self, analyzer: BrandToneAnalyzer) -> None:
        """Formal business content with enterprise vocabulary -> professional."""
        soup = _make_soup(PROFESSIONAL_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "professional"
        assert result.confidence > 0.0

    def test_analyze_casual(self, analyzer: BrandToneAnalyzer) -> None:
        """Casual language with contractions, slang, and exclamations -> casual."""
        soup = _make_soup(CASUAL_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "casual"
        assert result.confidence > 0.0

    def test_analyze_friendly(self, analyzer: BrandToneAnalyzer) -> None:
        """Welcoming, community-oriented language -> friendly."""
        soup = _make_soup(FRIENDLY_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "friendly"
        assert result.confidence > 0.0

    def test_analyze_technical(self, analyzer: BrandToneAnalyzer) -> None:
        """Developer docs with heavy technical jargon -> technical."""
        soup = _make_soup(TECHNICAL_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "technical"
        assert result.confidence > 0.0

    def test_analyze_playful(self, analyzer: BrandToneAnalyzer) -> None:
        """Exclamations, emoji, and fun language -> playful."""
        soup = _make_soup(PLAYFUL_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "playful"
        assert result.confidence > 0.0

    def test_analyze_luxury(self, analyzer: BrandToneAnalyzer) -> None:
        """Premium, exclusive, bespoke language -> luxury."""
        soup = _make_soup(LUXURY_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "luxury"
        assert result.confidence > 0.0

    def test_analyze_authoritative(self, analyzer: BrandToneAnalyzer) -> None:
        """Highly formal, authoritative tone with formal vocabulary -> authoritative."""
        soup = _make_soup(AUTHORITATIVE_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == "authoritative"
        assert result.confidence > 0.0


# ---------------------------------------------------------------------------
# Empty / minimal input tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_html(self, analyzer: BrandToneAnalyzer) -> None:
        """Empty body returns empty ToneResult with defaults."""
        soup = _make_soup(EMPTY_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == ""
        assert result.formality_score == 0.5
        assert result.vocabulary_level == ""
        assert result.confidence == 0.0
        assert result.markers == []

    def test_minimal_text(self, analyzer: BrandToneAnalyzer) -> None:
        """Very short text (< 20 chars) returns empty ToneResult."""
        soup = _make_soup(MINIMAL_HTML)
        result = analyzer.analyze(soup)
        assert result.tone == ""
        assert result.confidence == 0.0
        assert result.markers == []

    def test_no_body_tag(self, analyzer: BrandToneAnalyzer) -> None:
        """HTML without a body tag returns empty ToneResult."""
        soup = _make_soup("<html><head><title>No body</title></head></html>")
        result = analyzer.analyze(soup)
        assert result.tone == ""
        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Formality scoring tests
# ---------------------------------------------------------------------------


class TestFormalityScoring:
    def test_formality_high(self, analyzer: BrandToneAnalyzer) -> None:
        """Formal text with enterprise vocabulary scores > 0.5."""
        soup = _make_soup(PROFESSIONAL_HTML)
        text = analyzer._extract_body_text(soup)
        score = analyzer._analyze_formality(text)
        assert score > 0.5

    def test_formality_low(self, analyzer: BrandToneAnalyzer) -> None:
        """Casual text with many contractions scores < 0.5."""
        soup = _make_soup(CASUAL_HTML)
        text = analyzer._extract_body_text(soup)
        score = analyzer._analyze_formality(text)
        assert score < 0.5


# ---------------------------------------------------------------------------
# Vocabulary level tests
# ---------------------------------------------------------------------------


class TestVocabularyLevel:
    def test_vocabulary_simple(self, analyzer: BrandToneAnalyzer) -> None:
        """Short, common words -> simple vocabulary."""
        level = analyzer._analyze_vocabulary("I like fun. We go up. It is ok. Do it now.")
        assert level == "simple"

    def test_vocabulary_sophisticated(self, analyzer: BrandToneAnalyzer) -> None:
        """Long, complex words -> sophisticated vocabulary."""
        text = (
            "Comprehensive infrastructure methodologies facilitate "
            "organizational optimization throughout sophisticated "
            "enterprise architectures implementing revolutionary "
            "transformations establishing unprecedented accomplishments "
            "demonstrating extraordinary capabilities"
        )
        level = analyzer._analyze_vocabulary(text)
        assert level == "sophisticated"

    def test_vocabulary_moderate(self, analyzer: BrandToneAnalyzer) -> None:
        """Medium-length words -> moderate vocabulary."""
        text = (
            "The platform provides tools for building modern applications. "
            "Users can create and manage their projects from the dashboard."
        )
        level = analyzer._analyze_vocabulary(text)
        assert level == "moderate"


# ---------------------------------------------------------------------------
# Tone marker detection tests
# ---------------------------------------------------------------------------


class TestToneMarkers:
    def test_markers_exclamations(self, analyzer: BrandToneAnalyzer) -> None:
        """Three or more exclamation points detected as frequent_exclamations."""
        markers = analyzer._detect_tone_markers("Wow! This is great! Amazing! Incredible!")
        assert "frequent_exclamations" in markers

    def test_markers_single_exclamation(self, analyzer: BrandToneAnalyzer) -> None:
        """One exclamation point detected as uses_exclamations (not frequent)."""
        markers = analyzer._detect_tone_markers("Welcome to our website! We are glad you are here.")
        assert "uses_exclamations" in markers
        assert "frequent_exclamations" not in markers

    def test_markers_contractions(self, analyzer: BrandToneAnalyzer) -> None:
        """Contractions in text detected as uses_contractions."""
        markers = analyzer._detect_tone_markers(
            "We can't wait to show you what we've built. It's going to be great."
        )
        assert "uses_contractions" in markers

    def test_markers_imperatives(self, analyzer: BrandToneAnalyzer) -> None:
        """Imperative verbs at sentence starts detected as uses_imperatives."""
        markers = analyzer._detect_tone_markers(
            "Welcome aboard. Get started today. Build something amazing. Try it now."
        )
        assert "uses_imperatives" in markers

    def test_markers_technical_jargon(self, analyzer: BrandToneAnalyzer) -> None:
        """Three or more technical words detected as technical_jargon."""
        markers = analyzer._detect_tone_markers(
            "Our api and sdk provide a framework for microservice architecture with docker deployment."
        )
        assert "technical_jargon" in markers

    def test_markers_luxury_language(self, analyzer: BrandToneAnalyzer) -> None:
        """Two or more luxury words detected as luxury_language."""
        markers = analyzer._detect_tone_markers(
            "Our exclusive bespoke collection features curated handcrafted pieces."
        )
        assert "luxury_language" in markers

    def test_markers_first_person_plural(self, analyzer: BrandToneAnalyzer) -> None:
        """First person plural pronouns detected."""
        markers = analyzer._detect_tone_markers(
            "We believe in our mission to help us all grow together."
        )
        assert "first_person_plural" in markers

    def test_markers_second_person_address(self, analyzer: BrandToneAnalyzer) -> None:
        """Second person pronouns detected."""
        markers = analyzer._detect_tone_markers(
            "You deserve the best. Your experience matters to yours truly."
        )
        assert "second_person_address" in markers

    def test_markers_emoji(self, analyzer: BrandToneAnalyzer) -> None:
        """Emoji characters detected as uses_emoji."""
        markers = analyzer._detect_tone_markers(
            "Check out our new features \U0001f680 and celebrate \U0001f389 with us today."
        )
        assert "uses_emoji" in markers

    def test_markers_asks_questions(self, analyzer: BrandToneAnalyzer) -> None:
        """Two or more question marks detected as asks_questions."""
        markers = analyzer._detect_tone_markers(
            "What do you need? How can we help? Let us know today."
        )
        assert "asks_questions" in markers

    def test_markers_formal_language(self, analyzer: BrandToneAnalyzer) -> None:
        """Three or more formal words detected as formal_language."""
        markers = analyzer._detect_tone_markers(
            "Furthermore, the comprehensive methodology facilitates strategic governance compliance."
        )
        assert "formal_language" in markers


# ---------------------------------------------------------------------------
# Confidence range test
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_confidence_range(self, analyzer: BrandToneAnalyzer) -> None:
        """Confidence is always between 0.0 and 1.0 for all tone types."""
        html_samples = [
            PROFESSIONAL_HTML,
            CASUAL_HTML,
            FRIENDLY_HTML,
            TECHNICAL_HTML,
            PLAYFUL_HTML,
            LUXURY_HTML,
            AUTHORITATIVE_HTML,
        ]
        for html in html_samples:
            soup = _make_soup(html)
            result = analyzer.analyze(soup)
            assert 0.0 <= result.confidence <= 1.0, (
                f"Confidence {result.confidence} out of range for tone '{result.tone}'"
            )


# ---------------------------------------------------------------------------
# Serialization test
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_tone_result_to_dict(self) -> None:
        """ToneResult.to_dict serializes all fields correctly."""
        result = ToneResult(
            tone="professional",
            formality_score=0.72345,
            vocabulary_level="moderate",
            confidence=0.65432,
            markers=["formal_language", "first_person_plural"],
        )
        d = result.to_dict()
        assert d["tone"] == "professional"
        assert d["formality_score"] == 0.7235
        assert d["vocabulary_level"] == "moderate"
        assert d["confidence"] == 0.6543
        assert d["markers"] == ["formal_language", "first_person_plural"]
        assert isinstance(d, dict)

    def test_tone_result_to_dict_defaults(self) -> None:
        """Default ToneResult serializes cleanly."""
        result = ToneResult()
        d = result.to_dict()
        assert d["tone"] == ""
        assert d["formality_score"] == 0.5
        assert d["vocabulary_level"] == ""
        assert d["confidence"] == 0.0
        assert d["markers"] == []


# ---------------------------------------------------------------------------
# Body text extraction test
# ---------------------------------------------------------------------------


class TestBodyTextExtraction:
    def test_body_text_extraction(self, analyzer: BrandToneAnalyzer) -> None:
        """Script, style, nav, and noscript elements are stripped from body text."""
        soup = _make_soup(SCRIPT_STYLE_NAV_HTML)
        text = analyzer._extract_body_text(soup)

        # Content that should remain
        assert "visible content that should remain" in text

        # Content that should be stripped
        assert "this should be removed" not in text
        assert ".hidden" not in text
        assert "Home" not in text
        assert "About" not in text
        assert "Enable JavaScript" not in text
