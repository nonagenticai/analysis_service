"""Tests for brand consistency scorer."""

import pytest

from src.domains.brand.schemas import (
    BrandResponse,
    ColorInfo,
    ColorPalette,
    FontInfo,
    SchemaOrgData,
    SocialImages,
)
from src.domains.brand.services.consistency_scorer import BrandConsistencyScorer


@pytest.fixture
def scorer() -> BrandConsistencyScorer:
    return BrandConsistencyScorer()


@pytest.fixture
def empty_brand() -> BrandResponse:
    """Default BrandResponse with no customisation."""
    return BrandResponse()


@pytest.fixture
def full_brand() -> BrandResponse:
    """Fully populated BrandResponse that should score high."""
    return BrandResponse(
        name="Acme Corp",
        domain="acme.com",
        logo_url="https://acme.com/logo.png",
        primary_color="#FF5733",
        secondary_color="#C70039",
        has_logo=True,
        color_palette=ColorPalette(
            primary=ColorInfo(hex="#FF5733", role="primary"),
            secondary=ColorInfo(hex="#C70039", role="secondary"),
        ),
        fonts=[
            FontInfo(family="Inter", role="heading", category="sans-serif"),
            FontInfo(family="Roboto", role="body", category="sans-serif"),
        ],
        social_images=SocialImages(
            og_image="https://acme.com/og.png",
            twitter_image="https://acme.com/twitter.png",
        ),
        schema_org=SchemaOrgData(
            type="Organization",
            name="Acme Corp",
            url="https://acme.com",
        ),
        manifest_theme_color="#FF5733",
        favicon_urls=["https://acme.com/favicon.ico"],
    )


# ------------------------------------------------------------------
# 1. Overall scoring
# ------------------------------------------------------------------


class TestEmptyBrand:
    def test_empty_brand_low_score(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse
    ) -> None:
        """A default BrandResponse with nothing filled in should get a low score."""
        report = scorer.score(empty_brand)
        # Identity: no name (0), no domain (0), no logo (0) -> 0.0
        # Visual: default colors (0), no palette (0), no fonts (0) -> 0.0
        # Metadata: nothing -> 0.0
        # Content: nothing -> 0.0
        assert report.overall_score < 0.1
        assert len(report.missing_fields) > 0
        assert len(report.recommendations) > 0


class TestFullBrand:
    def test_full_brand_high_score(
        self, scorer: BrandConsistencyScorer, full_brand: BrandResponse
    ) -> None:
        """A fully populated brand (minus Phase B fields) should score high."""
        report = scorer.score(full_brand)
        # Identity: name!=domain (0.4) + domain (0.2) + logo (0.4) = 1.0
        # Visual: custom primary (0.3) + custom secondary (0.1) + palette (0.2)
        #         + fonts (0.3) + heading+body (0.1) = 1.0
        # Metadata: schema_org (0.35) + social_images (0.25) + favicons (0.2)
        #           + manifest (0.2) = 1.0
        # Content: no Phase B fields = 0.0
        # Overall: 0.3*1.0 + 0.3*1.0 + 0.2*1.0 + 0.2*0.0 = 0.80
        assert report.overall_score >= 0.75
        assert report.identity_score == pytest.approx(1.0)
        assert report.visual_score == pytest.approx(1.0)
        assert report.metadata_score == pytest.approx(1.0)


# ------------------------------------------------------------------
# 2. Identity scoring
# ------------------------------------------------------------------


class TestIdentityScore:
    def test_identity_score_with_name_and_logo(self, scorer: BrandConsistencyScorer) -> None:
        """Name (different from domain) + domain + logo should give 1.0."""
        brand = BrandResponse(name="Acme Corp", domain="acme.com", has_logo=True)
        report = scorer.score(brand)
        assert report.identity_score == pytest.approx(1.0)

    def test_identity_score_name_equals_domain(self, scorer: BrandConsistencyScorer) -> None:
        """When name equals domain, the +0.4 name bonus is not awarded."""
        brand = BrandResponse(name="acme.com", domain="acme.com", has_logo=True)
        report = scorer.score(brand)
        # domain (0.2) + logo (0.4) = 0.6, name==domain so no +0.4
        assert report.identity_score == pytest.approx(0.6)

    def test_identity_score_no_logo(self, scorer: BrandConsistencyScorer) -> None:
        """Missing logo should be detected in missing_fields."""
        brand = BrandResponse(name="Acme Corp", domain="acme.com", has_logo=False)
        report = scorer.score(brand)
        assert "logo" in report.missing_fields
        assert report.identity_score == pytest.approx(0.6)
        assert any("logo" in r.lower() for r in report.recommendations)


# ------------------------------------------------------------------
# 3. Visual scoring
# ------------------------------------------------------------------


class TestVisualScore:
    def test_visual_score_custom_colors(self, scorer: BrandConsistencyScorer) -> None:
        """Non-default primary and secondary colors should contribute to score."""
        brand = BrandResponse(
            primary_color="#FF0000",
            secondary_color="#00FF00",
        )
        report = scorer.score(brand)
        # primary (0.3) + secondary (0.1) = 0.4
        assert report.visual_score == pytest.approx(0.4)

    def test_visual_score_default_colors(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse
    ) -> None:
        """Default colors should contribute 0 to visual score."""
        report = scorer.score(empty_brand)
        assert report.visual_score == pytest.approx(0.0)
        assert "custom_colors" in report.missing_fields

    def test_visual_score_with_fonts(self, scorer: BrandConsistencyScorer) -> None:
        """Fonts without heading+body should add 0.3."""
        brand = BrandResponse(
            fonts=[FontInfo(family="Inter", role="body", category="sans-serif")],
        )
        report = scorer.score(brand)
        # Only fonts (0.3), default colors (0), no palette (0)
        assert report.visual_score == pytest.approx(0.3)

    def test_visual_score_heading_body_fonts(self, scorer: BrandConsistencyScorer) -> None:
        """Both heading and body font roles should award the 0.1 bonus."""
        brand = BrandResponse(
            fonts=[
                FontInfo(family="Inter", role="heading", category="sans-serif"),
                FontInfo(family="Roboto", role="body", category="sans-serif"),
            ],
        )
        report = scorer.score(brand)
        # fonts (0.3) + heading+body bonus (0.1) = 0.4
        assert report.visual_score == pytest.approx(0.4)


# ------------------------------------------------------------------
# 4. Metadata scoring
# ------------------------------------------------------------------


class TestMetadataScore:
    def test_metadata_score_schema_org(self, scorer: BrandConsistencyScorer) -> None:
        """Schema.org data alone should add 0.35."""
        brand = BrandResponse(
            schema_org=SchemaOrgData(type="Organization", name="Test"),
        )
        report = scorer.score(brand)
        assert report.metadata_score == pytest.approx(0.35)

    def test_metadata_score_social_images(self, scorer: BrandConsistencyScorer) -> None:
        """Social images with og_image should add 0.25."""
        brand = BrandResponse(
            social_images=SocialImages(og_image="https://example.com/og.png"),
        )
        report = scorer.score(brand)
        assert report.metadata_score == pytest.approx(0.25)

    def test_metadata_score_favicons(self, scorer: BrandConsistencyScorer) -> None:
        """Non-empty favicon_urls should add 0.2."""
        brand = BrandResponse(
            favicon_urls=["https://example.com/favicon.ico"],
        )
        report = scorer.score(brand)
        assert report.metadata_score == pytest.approx(0.2)


# ------------------------------------------------------------------
# 5. Content scoring
# ------------------------------------------------------------------


class TestContentScore:
    def test_content_score_empty(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse
    ) -> None:
        """No Phase B content fields means content score is 0."""
        report = scorer.score(empty_brand)
        assert report.content_score == pytest.approx(0.0)

    def test_content_score_with_tagline(self, scorer: BrandConsistencyScorer) -> None:
        """A brand with a tagline attribute should get +0.3 content score."""
        brand = BrandResponse()
        # Simulate Phase B field via object attribute injection
        object.__setattr__(brand, "tagline", "Innovation for everyone")
        report = scorer.score(brand)
        assert report.content_score == pytest.approx(0.3)


# ------------------------------------------------------------------
# 6. Missing fields and recommendations
# ------------------------------------------------------------------


class TestMissingFieldsAndRecommendations:
    def test_missing_fields_populated(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse
    ) -> None:
        """An empty brand should have many missing fields."""
        report = scorer.score(empty_brand)
        # Should include at least: name, logo, custom_colors, fonts,
        # schema_org, social_images, favicon, manifest, tagline,
        # social_profiles, brand_voice, tone_result
        expected_missing = {
            "name",
            "logo",
            "custom_colors",
            "fonts",
            "schema_org",
            "social_images",
            "favicon",
            "manifest",
            "tagline",
            "social_profiles",
            "brand_voice",
            "tone_result",
        }
        assert expected_missing.issubset(set(report.missing_fields))

    def test_recommendations_populated(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse
    ) -> None:
        """An incomplete brand should have recommendations."""
        report = scorer.score(empty_brand)
        assert len(report.recommendations) > 0
        # Should include logo and name recommendations
        rec_text = " ".join(report.recommendations).lower()
        assert "logo" in rec_text
        assert "name" in rec_text


# ------------------------------------------------------------------
# 7. Overall weighting
# ------------------------------------------------------------------


class TestOverallWeighting:
    def test_overall_score_weighted(self, scorer: BrandConsistencyScorer) -> None:
        """Overall score should be the weighted average of sub-scores."""
        # Build a brand with known sub-scores:
        # identity: name+domain+logo = 1.0
        # visual: 0.0 (all defaults)
        # metadata: 0.0
        # content: 0.0
        brand = BrandResponse(name="Acme Corp", domain="acme.com", has_logo=True)
        report = scorer.score(brand)
        expected = (
            0.3 * report.identity_score
            + 0.3 * report.visual_score
            + 0.2 * report.metadata_score
            + 0.2 * report.content_score
        )
        assert report.overall_score == pytest.approx(expected)


# ------------------------------------------------------------------
# 8. Report serialisation
# ------------------------------------------------------------------


class TestReportToDict:
    def test_report_to_dict(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse
    ) -> None:
        """to_dict should produce the expected keys and rounded values."""
        report = scorer.score(empty_brand)
        d = report.to_dict()
        assert set(d.keys()) == {
            "overall_score",
            "identity_score",
            "visual_score",
            "metadata_score",
            "content_score",
            "missing_fields",
            "recommendations",
        }
        # Values should be rounded to 4 decimal places
        for key in (
            "overall_score",
            "identity_score",
            "visual_score",
            "metadata_score",
            "content_score",
        ):
            assert isinstance(d[key], float)
        assert isinstance(d["missing_fields"], list)
        assert isinstance(d["recommendations"], list)


# ------------------------------------------------------------------
# 9. Score bounds
# ------------------------------------------------------------------


class TestScoreBounds:
    def test_score_bounds(
        self, scorer: BrandConsistencyScorer, empty_brand: BrandResponse, full_brand: BrandResponse
    ) -> None:
        """All scores should be between 0.0 and 1.0."""
        for brand in (empty_brand, full_brand):
            report = scorer.score(brand)
            assert 0.0 <= report.overall_score <= 1.0
            assert 0.0 <= report.identity_score <= 1.0
            assert 0.0 <= report.visual_score <= 1.0
            assert 0.0 <= report.metadata_score <= 1.0
            assert 0.0 <= report.content_score <= 1.0


# ------------------------------------------------------------------
# 10. Full brand recommendations
# ------------------------------------------------------------------


class TestFullBrandRecommendations:
    def test_full_brand_no_recommendations(
        self, scorer: BrandConsistencyScorer, full_brand: BrandResponse
    ) -> None:
        """A fully populated brand should have few or no recommendations
        outside of Phase B content fields."""
        report = scorer.score(full_brand)
        # Identity, visual, and metadata should all be perfect,
        # so recommendations should only come from content (Phase B).
        non_content_recs = [
            r
            for r in report.recommendations
            if not any(
                keyword in r.lower()
                for keyword in ("tagline", "social media profile", "brand voice", "tone")
            )
        ]
        assert len(non_content_recs) == 0
