"""Configurable extraction parameters for brand extraction."""

from __future__ import annotations

from pydantic import BaseModel, Field

ALL_METHODS = [
    "html_meta",
    "css_parser",
    "schema_org",
    "manifest",
    "social_extractor",
    "content_analyzer",
    "industry_detector",
    "tone_analyzer",
    "logo_intelligence",
    "llm_analysis",
    "visual_colors",
    # Pipeline stage names
    "fetch",
    "name",
    "logo",
    "colors",
    "fonts",
    "metadata",
    "content",
    "social",
    "industry",
    "palette",
    "logo_download",
    "quality",
]


class BrandExtractConfig(BaseModel):
    """Configuration for controlling brand extraction behavior.

    All fields have sensible defaults - omitting fields uses defaults.
    """

    methods: list[str] = Field(
        default_factory=lambda: list(ALL_METHODS),
        description="Which extraction methods to run (default: all)",
    )
    confidence_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum confidence to accept extraction results",
    )
    timeout: float = Field(
        default=15.0,
        gt=0.0,
        le=120.0,
        description="HTTP fetch timeout in seconds",
    )
    max_css_stylesheets: int = Field(
        default=10,
        ge=0,
        le=50,
        description="Maximum CSS stylesheets to fetch and parse",
    )
    extract_social: bool = Field(
        default=True,
        description="Extract social media profiles",
    )
    extract_tagline: bool = Field(
        default=True,
        description="Extract tagline and content analysis",
    )
    extract_fonts: bool = Field(
        default=True,
        description="Extract font/typography information",
    )
    extract_colors: bool = Field(
        default=True,
        description="Extract color palette",
    )
    download_logo: bool = Field(
        default=False,
        description="Download and store logo to storage backend",
    )

    def should_run(self, method: str) -> bool:
        """Check if a specific extraction method should run."""
        return method in self.methods

    def is_feature_enabled(self, feature: str) -> bool:
        """Check if a feature toggle is enabled."""
        feature_map = {
            "social": self.extract_social,
            "tagline": self.extract_tagline,
            "fonts": self.extract_fonts,
            "colors": self.extract_colors,
            "logo": self.download_logo,
        }
        return feature_map.get(feature, True)


def default_config() -> BrandExtractConfig:
    """Return default extraction config with all features enabled."""
    return BrandExtractConfig()


def minimal_config() -> BrandExtractConfig:
    """Return minimal/fast config - only essential extractors."""
    return BrandExtractConfig(
        methods=["html_meta"],
        extract_social=False,
        extract_tagline=False,
        extract_fonts=False,
        extract_colors=True,
        download_logo=False,
        max_css_stylesheets=0,
        timeout=5.0,
    )
