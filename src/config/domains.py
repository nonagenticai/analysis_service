"""Per-domain settings (one section per merged former service).

analyze / review — the only two domains (of the four consolidated into
analysis_service) that own a settings section.

Neither critique nor brand does. Critique's knobs live on the shared
``scanners`` / ``scoring`` / ``external_tools`` / ``features`` sections. Brand's
one live knob is ``playwright`` (read by ``src/domains/brand/stages/fetch.py``);
its own ``BrandSettings`` section declared ``max_pages`` / ``extraction_timeout``
and was read by nothing — brand extraction runs a single-URL pipeline and has no
page limit to configure. It is deleted; ``BRAND_*`` now fails startup.

These live in the config package (not under ``src/domains/<domain>/``) to avoid
a circular import: domain code imports ``get_settings`` from ``src.config``,
which composes these classes.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "AnalyzeSettings",
    "ReviewSettings",
]


class AnalyzeSettings(BaseSettings):
    """Analyzer domain settings (page classification, narrative extraction)."""

    parallel_workers: int = Field(
        default=4, description="Default parallel worker count for page analysis"
    )
    page_analysis_timeout: int = Field(
        default=30, description="Per-page analysis timeout in seconds"
    )
    max_aggregate_timeout: int = Field(
        default=300, description="Maximum total timeout in seconds for all page analyses combined"
    )

    model_config = SettingsConfigDict(
        env_prefix="ANALYZE_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class ReviewSettings(BaseSettings):
    """Review domain settings (design, usability, features, content quality)."""

    timeout_seconds: int = Field(
        default=600,
        description=(
            "Per-call cap ReviewService applies around WMClient.message(). Must not "
            "sit below WM_TIMEOUT_SECONDS (600s) or the client's own timeout becomes "
            "dead code and every slow review dies early with an unattributed "
            "TimeoutError. Was 180 and read by nothing; see "
            "tests/unit/domains/review/test_review_wm_timeout_budget.py."
        ),
    )
    job_retention_hours: int = Field(
        default=72, description="Hours to retain completed/failed jobs before cleanup"
    )
    cleanup_interval_minutes: int = Field(default=60, description="Minutes between cleanup runs")
    max_pages_per_request: int = Field(
        default=50, description="Maximum number of pages per review request"
    )
    max_request_size_kb: int = Field(default=512, description="Maximum request payload size in KB")

    # Scoring weights
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

    # Video compiler integration (URL resolved via w2v_common.discovery.service_url)
    video_compiler_enabled: bool = Field(default=True, description="Enable video generation")
    video_compiler_timeout: int = Field(
        default=300, description="Video compilation timeout in seconds"
    )

    # Feature flags
    sse_progress: bool = Field(default=True, description="Enable SSE real-time progress")
    batch_reviews: bool = Field(default=True, description="Enable batch review support")

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_", env_file=".env", case_sensitive=False, extra="ignore"
    )

    def get_category_weights(self, review_type: str) -> dict[str, float]:
        """Get category weights for a specific review type."""
        mapping = {
            "design_review": self.design_weights,
            "usability_review": self.usability_weights,
            "content_quality_review": self.content_weights,
        }
        return mapping.get(review_type, {})
