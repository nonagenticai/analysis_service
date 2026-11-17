"""Tests for industry_detector."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.services.industry_detector import IndustryClassification, IndustryDetector
from tests.fixtures.html_industries import (
    AGENCY_WEBSITE,
    AMBIGUOUS_WEBSITE,
    ECOMMERCE_WEBSITE,
    EDUCATION_WEBSITE,
    FINANCE_WEBSITE,
    HEALTHCARE_WEBSITE,
    MEDIA_WEBSITE,
    NONPROFIT_WEBSITE,
    SAAS_WEBSITE,
    SCHEMA_SAAS_WEBSITE,
)


@pytest.fixture
def detector() -> IndustryDetector:
    return IndustryDetector()


def _make_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class TestClassifyIndustries:
    def test_classify_saas(self, detector: IndustryDetector) -> None:
        """SaaS website is classified as saas industry."""
        soup = _make_soup(SAAS_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "saas"
        assert result.confidence > 0.0

    def test_classify_ecommerce(self, detector: IndustryDetector) -> None:
        """Ecommerce website is classified as ecommerce industry."""
        soup = _make_soup(ECOMMERCE_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "ecommerce"
        assert result.confidence > 0.0

    def test_classify_media(self, detector: IndustryDetector) -> None:
        """Media website is classified as media industry."""
        soup = _make_soup(MEDIA_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "media"
        assert result.confidence > 0.0

    def test_classify_healthcare(self, detector: IndustryDetector) -> None:
        """Healthcare website is classified as healthcare industry."""
        soup = _make_soup(HEALTHCARE_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "healthcare"
        assert result.confidence > 0.0

    def test_classify_finance(self, detector: IndustryDetector) -> None:
        """Finance website is classified as finance industry."""
        soup = _make_soup(FINANCE_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "finance"
        assert result.confidence > 0.0

    def test_classify_education(self, detector: IndustryDetector) -> None:
        """Education website is classified as education industry."""
        soup = _make_soup(EDUCATION_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "education"
        assert result.confidence > 0.0

    def test_classify_agency(self, detector: IndustryDetector) -> None:
        """Agency website is classified as agency industry."""
        soup = _make_soup(AGENCY_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "agency"
        assert result.confidence > 0.0

    def test_classify_nonprofit(self, detector: IndustryDetector) -> None:
        """Nonprofit website is classified as nonprofit industry."""
        soup = _make_soup(NONPROFIT_WEBSITE)
        result = detector.classify(soup)
        assert result.industry == "nonprofit"
        assert result.confidence > 0.0

    def test_classify_ambiguous(self, detector: IndustryDetector) -> None:
        """Ambiguous website produces low confidence or empty classification."""
        soup = _make_soup(AMBIGUOUS_WEBSITE)
        result = detector.classify(soup)
        # Either no industry detected or very low confidence
        if result.industry:
            assert result.confidence < 0.5
        else:
            assert result.confidence == 0.0


class TestSchemaOrg:
    def test_schema_org_saas(self, detector: IndustryDetector) -> None:
        """SoftwareApplication schema.org type boosts saas classification."""
        soup = _make_soup(SCHEMA_SAAS_WEBSITE)
        schema_org = {"@type": "SoftwareApplication", "name": "AppCo"}
        result = detector.classify(soup, schema_org=schema_org)
        assert result.industry == "saas"
        assert result.confidence > 0.5

    def test_schema_org_ecommerce(self, detector: IndustryDetector) -> None:
        """Product schema.org type boosts ecommerce classification."""
        html = """<!DOCTYPE html>
<html><head><title>Store</title></head>
<body><h1>Our Products</h1><p>Buy our items.</p></body></html>"""
        soup = _make_soup(html)
        schema_org = {"@type": "Product", "name": "Widget"}
        result = detector.classify(soup, schema_org=schema_org)
        assert result.industry == "ecommerce"

    def test_no_schema_org(self, detector: IndustryDetector) -> None:
        """Classification works correctly without schema.org data."""
        soup = _make_soup(SAAS_WEBSITE)
        result = detector.classify(soup, schema_org=None)
        assert result.industry == "saas"
        assert result.confidence > 0.0


class TestWeighting:
    def test_heading_weight_3x(self, detector: IndustryDetector) -> None:
        """Keywords in headings are weighted 3x compared to body text."""
        # Put saas keyword only in heading, ecommerce keywords only in body
        html = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<h1>software platform cloud</h1>
<p>software</p>
</body></html>"""
        soup = _make_soup(html)
        segments = detector._extract_text_segments(soup)
        scores = detector._score_keywords(segments)
        # "software" appears in heading (3x) and body (1x) = 4
        # "platform" appears in heading only (3x) = 3
        # "cloud" appears in heading only (3x) = 3
        # Total saas from these keywords = 10
        # Heading-only keywords contribute 3x per occurrence
        assert scores["saas"] > 0
        # Verify heading contribution is significant
        heading_only_html = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<h1>software platform cloud</h1>
</body></html>"""
        body_only_html = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<p>software platform cloud</p>
</body></html>"""
        heading_segments = detector._extract_text_segments(_make_soup(heading_only_html))
        body_segments = detector._extract_text_segments(_make_soup(body_only_html))
        heading_scores = detector._score_keywords(heading_segments)
        body_scores = detector._score_keywords(body_segments)
        assert heading_scores["saas"] == body_scores["saas"] * 3

    def test_meta_weight_2x(self, detector: IndustryDetector) -> None:
        """Keywords in meta tags are weighted 2x compared to body text."""
        meta_html = """<!DOCTYPE html>
<html><head><title>Test</title>
<meta name="description" content="software platform cloud">
</head><body></body></html>"""
        body_html = """<!DOCTYPE html>
<html><head><title>Test</title></head><body>
<p>software platform cloud</p>
</body></html>"""
        meta_segments = detector._extract_text_segments(_make_soup(meta_html))
        body_segments = detector._extract_text_segments(_make_soup(body_html))
        meta_scores = detector._score_keywords(meta_segments)
        body_scores = detector._score_keywords(body_segments)
        assert meta_scores["saas"] == body_scores["saas"] * 2


class TestConfidence:
    def test_confidence_calculation(self, detector: IndustryDetector) -> None:
        """Confidence equals top_score / (top_score + second_score)."""
        soup = _make_soup(SAAS_WEBSITE)
        result = detector.classify(soup)
        # Recalculate manually
        segments = detector._extract_text_segments(soup)
        scores = detector._score_keywords(segments)
        sorted_scores = sorted(scores.values(), reverse=True)
        top = sorted_scores[0]
        second = sorted_scores[1] if len(sorted_scores) > 1 else 0.0
        expected_confidence = top / (top + second) if top > 0 else 0.0
        assert abs(result.confidence - expected_confidence) < 0.01

    def test_multiple_industries_picks_highest(self, detector: IndustryDetector) -> None:
        """When multiple industries match, the highest-scoring one wins."""
        # Mix saas and ecommerce keywords, but saas should dominate
        html = """<!DOCTYPE html>
<html><head><title>SaaS Platform</title>
<meta name="description" content="Cloud SaaS software platform with API integration">
</head><body>
<h1>Cloud Software Platform</h1>
<h2>API Dashboard</h2>
<p>Our SaaS platform provides cloud automation and workflow integration.
Deploy scalable software with uptime SLA guarantees. Also shop our store.</p>
</body></html>"""
        soup = _make_soup(html)
        result = detector.classify(soup)
        assert result.industry == "saas"


class TestSubIndustry:
    def test_sub_industry_detected(self, detector: IndustryDetector) -> None:
        """Sub-industry is populated when sub-industry keywords match."""
        # SaaS with devtools keywords
        html = """<!DOCTYPE html>
<html><head><title>DevPlatform</title>
<meta name="description" content="Developer SDK and API platform for cloud software">
</head><body>
<h1>Developer Tools Platform</h1>
<h2>SDK & API</h2>
<p>Build with our developer SDK. Our software platform provides API integration,
CLI tools, and framework support for cloud deployment and automation.</p>
</body></html>"""
        soup = _make_soup(html)
        result = detector.classify(soup)
        assert result.industry == "saas"
        assert result.sub_industry != ""

    def test_sub_industry_devtools(self, detector: IndustryDetector) -> None:
        """SaaS devtools sub-industry is detected with developer/sdk/api keywords."""
        html = """<!DOCTYPE html>
<html><head><title>DevKit - Developer Tools</title>
<meta name="description" content="Developer SDK and API framework for software platform">
</head><body>
<h1>Developer SDK Platform</h1>
<h2>API Framework</h2>
<p>Our developer SDK provides a comprehensive API and CLI for building on our
cloud software platform. Deploy integrations with our framework.</p>
</body></html>"""
        soup = _make_soup(html)
        result = detector.classify(soup)
        assert result.industry == "saas"
        assert result.sub_industry == "devtools"


class TestSignalsAndSerialization:
    def test_signals_populated(self, detector: IndustryDetector) -> None:
        """Signals list is non-empty for classified industries."""
        soup = _make_soup(SAAS_WEBSITE)
        result = detector.classify(soup)
        assert len(result.signals) > 0
        # Signals should contain keywords from the saas keyword list
        assert any(
            s in IndustryDetector.INDUSTRY_KEYWORDS["saas"]["keywords"] for s in result.signals
        )

    def test_classification_to_dict(self, detector: IndustryDetector) -> None:
        """IndustryClassification serializes correctly to dict."""
        classification = IndustryClassification(
            industry="saas",
            sub_industry="devtools",
            confidence=0.82345,
            signals=["software", "platform", "api"],
        )
        d = classification.to_dict()
        assert d["industry"] == "saas"
        assert d["sub_industry"] == "devtools"
        assert d["confidence"] == 0.8235  # rounded to 4 decimal places
        assert d["signals"] == ["software", "platform", "api"]
        assert isinstance(d, dict)


class TestEdgeCases:
    def test_empty_html(self, detector: IndustryDetector) -> None:
        """Empty HTML returns empty classification."""
        soup = _make_soup("")
        result = detector.classify(soup)
        assert result.industry == ""
        assert result.sub_industry == ""
        assert result.confidence == 0.0
        assert result.signals == []

    def test_extract_text_segments(self, detector: IndustryDetector) -> None:
        """Text segments are correctly extracted from headings, meta, and body."""
        soup = _make_soup(SAAS_WEBSITE)
        segments = detector._extract_text_segments(soup)
        assert "headings" in segments
        assert "meta" in segments
        assert "body" in segments
        # Headings should contain h1 and h2 text
        assert "Automate Your Workflow" in segments["headings"]
        assert "Cloud Platform Dashboard" in segments["headings"]
        # Meta should contain title and description
        assert "CloudPlatform" in segments["meta"]
        assert "SaaS" in segments["meta"]
        # Body should contain paragraph text
        assert "scalable" in segments["body"]
        assert "API" in segments["body"]
