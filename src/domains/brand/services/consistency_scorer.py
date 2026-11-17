"""Brand data quality and consistency scoring."""

from __future__ import annotations

from dataclasses import dataclass, field

from src.domains.brand.schemas import BrandResponse


@dataclass
class BrandConsistencyReport:
    """Detailed brand quality scoring report."""

    overall_score: float = 0.0  # 0.0-1.0
    identity_score: float = 0.0  # name, domain, logo
    visual_score: float = 0.0  # colors, palette, fonts
    metadata_score: float = 0.0  # schema.org, social images, favicons
    content_score: float = 0.0  # tagline, social profiles, tone
    missing_fields: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "overall_score": round(self.overall_score, 4),
            "identity_score": round(self.identity_score, 4),
            "visual_score": round(self.visual_score, 4),
            "metadata_score": round(self.metadata_score, 4),
            "content_score": round(self.content_score, 4),
            "missing_fields": self.missing_fields,
            "recommendations": self.recommendations,
        }


class BrandConsistencyScorer:
    """Scores brand data quality and completeness across multiple dimensions."""

    # Weight each dimension contributes to the overall score.
    WEIGHT_IDENTITY = 0.3
    WEIGHT_VISUAL = 0.3
    WEIGHT_METADATA = 0.2
    WEIGHT_CONTENT = 0.2

    def score(self, brand: BrandResponse) -> BrandConsistencyReport:
        """Score a brand across all dimensions and return a full report.

        Args:
            brand: Brand response to evaluate.

        Returns:
            BrandConsistencyReport with per-dimension scores and recommendations.
        """
        identity_score, identity_missing, identity_recs = self._score_identity(brand)
        visual_score, visual_missing, visual_recs = self._score_visual(brand)
        metadata_score, metadata_missing, metadata_recs = self._score_metadata(brand)
        content_score, content_missing, content_recs = self._score_content(brand)

        overall = min(
            self.WEIGHT_IDENTITY * identity_score
            + self.WEIGHT_VISUAL * visual_score
            + self.WEIGHT_METADATA * metadata_score
            + self.WEIGHT_CONTENT * content_score,
            1.0,
        )

        missing_fields = identity_missing + visual_missing + metadata_missing + content_missing
        recommendations = identity_recs + visual_recs + metadata_recs + content_recs

        return BrandConsistencyReport(
            overall_score=overall,
            identity_score=identity_score,
            visual_score=visual_score,
            metadata_score=metadata_score,
            content_score=content_score,
            missing_fields=missing_fields,
            recommendations=recommendations,
        )

    # ------------------------------------------------------------------
    # Sub-scorers
    # ------------------------------------------------------------------

    def _score_identity(self, brand: BrandResponse) -> tuple[float, list[str], list[str]]:
        """Score identity fields: name, domain, logo."""
        score = 0.0
        missing: list[str] = []
        recommendations: list[str] = []

        # Name present and different from domain: +0.4
        if brand.name and brand.name != brand.domain:
            score += 0.4
        else:
            if not brand.name:
                missing.append("name")
            recommendations.append("Set a descriptive brand name")

        # Domain present: +0.2
        if brand.domain:
            score += 0.2

        # Has logo: +0.4
        if brand.has_logo:
            score += 0.4
        else:
            missing.append("logo")
            recommendations.append("Add a logo for stronger brand identity")

        return min(score, 1.0), missing, recommendations

    def _score_visual(self, brand: BrandResponse) -> tuple[float, list[str], list[str]]:
        """Score visual fields: colors, palette, fonts."""
        score = 0.0
        missing: list[str] = []
        recommendations: list[str] = []

        # Primary color not default: +0.3
        has_custom_primary = brand.primary_color != "#2563EB"
        # Secondary color not default: +0.1
        has_custom_secondary = brand.secondary_color != "#1E40AF"

        if has_custom_primary:
            score += 0.3
        if has_custom_secondary:
            score += 0.1

        if not has_custom_primary and not has_custom_secondary:
            missing.append("custom_colors")
            recommendations.append("Define custom brand colors instead of using defaults")

        # Color palette present: +0.2
        if brand.color_palette is not None:
            score += 0.2
        else:
            recommendations.append("Generate a full color palette for design consistency")

        # Fonts non-empty: +0.3
        if brand.fonts:
            score += 0.3

            # Bonus: has both heading and body roles: +0.1
            roles = {f.role for f in brand.fonts}
            if "heading" in roles and "body" in roles:
                score += 0.1
        else:
            missing.append("fonts")
            recommendations.append("Extract typography information for brand consistency")

        return min(score, 1.0), missing, recommendations

    def _score_metadata(self, brand: BrandResponse) -> tuple[float, list[str], list[str]]:
        """Score metadata fields: schema.org, social images, favicons, manifest."""
        score = 0.0
        missing: list[str] = []
        recommendations: list[str] = []

        # Schema.org present: +0.35
        if brand.schema_org is not None:
            score += 0.35
        else:
            missing.append("schema_org")
            recommendations.append("Add Schema.org structured data for better SEO")

        # Social images with at least og_image: +0.25
        if brand.social_images is not None and brand.social_images.og_image:
            score += 0.25
        else:
            missing.append("social_images")
            recommendations.append("Add Open Graph social images for link previews")

        # Favicon URLs non-empty: +0.2
        if brand.favicon_urls:
            score += 0.2
        else:
            missing.append("favicon")
            recommendations.append("Add favicon URLs for browser tab branding")

        # Manifest theme color non-empty: +0.2
        if brand.manifest_theme_color:
            score += 0.2
        else:
            missing.append("manifest")
            recommendations.append("Set a web manifest theme color")

        return min(score, 1.0), missing, recommendations

    def _score_content(self, brand: BrandResponse) -> tuple[float, list[str], list[str]]:
        """Score content fields (Phase B): tagline, social_profiles, brand_voice, tone_result."""
        score = 0.0
        missing: list[str] = []
        recommendations: list[str] = []

        # Tagline: +0.3
        tagline = getattr(brand, "tagline", None)
        if tagline:
            score += 0.3
        else:
            missing.append("tagline")
            recommendations.append("Add a brand tagline or slogan")

        # Social profiles: +0.3
        social_profiles = getattr(brand, "social_profiles", None)
        if social_profiles:
            score += 0.3
        else:
            missing.append("social_profiles")
            recommendations.append("Add social media profile links")

        # Brand voice: +0.2
        brand_voice = getattr(brand, "brand_voice", None)
        if brand_voice:
            score += 0.2
        else:
            missing.append("brand_voice")
            recommendations.append("Define the brand voice and messaging style")

        # Tone result: +0.2
        tone_result = getattr(brand, "tone_result", None)
        if tone_result:
            score += 0.2
        else:
            missing.append("tone_result")
            recommendations.append("Analyze content tone for brand consistency")

        return min(score, 1.0), missing, recommendations
