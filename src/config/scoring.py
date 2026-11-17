"""Scoring-weights configuration (review / critique domain legacy)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ScoringSettings"]


class ScoringSettings(BaseSettings):
    """Scoring weights configuration (review/critique domain legacy)."""

    critical_weight: int = Field(default=30, description="Penalty weight for critical findings")
    high_weight: int = Field(default=15, description="Penalty weight for high findings")
    medium_weight: int = Field(default=7, description="Penalty weight for medium findings")
    low_weight: int = Field(default=3, description="Penalty weight for low findings")
    info_weight: int = Field(default=0, description="Penalty weight for info findings")
    decay_k: int = Field(default=25, description="Exponential decay constant")

    # Category weights for overall score (must sum to 1.0)
    a11y_weight: float = Field(default=0.25, description="Accessibility category weight")
    security_weight: float = Field(default=0.25, description="Security category weight")
    performance_weight: float = Field(default=0.20, description="Performance category weight")
    seo_weight: float = Field(default=0.15, description="SEO category weight")
    ux_weight: float = Field(default=0.15, description="UX category weight")
    cro_weight: float = Field(default=0.15, description="CRO category weight")
    score_floor: float = Field(default=50.0, description="Minimum floor for category scores")

    # Lighthouse-blended performance scoring
    perf_finding_weight: float = Field(
        default=0.4, description="Weight for finding-based performance score"
    )
    perf_lighthouse_weight: float = Field(
        default=0.6, description="Weight for Lighthouse-based performance score"
    )

    # Per-review-type category weights
    design_weights: dict[str, float] = Field(
        default={
            "layout_consistency": 0.15,
            "whitespace_usage": 0.10,
            "typography_hierarchy": 0.15,
            "color_palette_harmony": 0.10,
            "contrast_ratios": 0.15,
            "responsive_design": 0.10,
            "visual_hierarchy": 0.15,
            "brand_consistency": 0.10,
        },
        description="Category weights for design review scoring",
    )
    usability_weights: dict[str, float] = Field(
        default={
            "navigation_clarity": 0.15,
            "form_usability": 0.10,
            "cta_visibility": 0.15,
            "content_readability": 0.15,
            "accessibility": 0.15,
            "mobile_friendliness": 0.10,
            "error_prevention": 0.10,
            "loading_performance": 0.10,
        },
        description="Category weights for usability review scoring",
    )
    content_weights: dict[str, float] = Field(
        default={
            "writing_clarity": 0.15,
            "meta_descriptions": 0.10,
            "heading_hierarchy": 0.10,
            "content_completeness": 0.15,
            "keyword_relevance": 0.15,
            "internal_linking": 0.10,
            "content_freshness": 0.10,
            "cta_effectiveness": 0.15,
        },
        description="Category weights for content quality review scoring",
    )
    aggregate_weights: dict[str, float] = Field(
        default={
            "design_review": 0.20,
            "usability_review": 0.25,
            "content_quality_review": 0.20,
            "feature_review": 0.20,
            "feature_suggestions": 0.15,
        },
        description="Aggregate weights across review types",
    )

    model_config = SettingsConfigDict(
        env_prefix="SCORING_", env_file=".env", case_sensitive=False, extra="ignore"
    )

    def get_category_weights(self, review_type: str) -> dict[str, float]:
        """Get category weights for a specific review type."""
        mapping = {
            "design_review": self.design_weights,
            "usability_review": self.usability_weights,
            "content_quality_review": self.content_weights,
        }
        return mapping.get(review_type, {})
