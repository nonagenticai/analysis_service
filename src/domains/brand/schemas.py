"""Request/response Pydantic models for brand extraction."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LLMConfigOverride(BaseModel):
    """Optional overrides for the LLM provider used during extraction."""

    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


class PageData(BaseModel):
    """Minimal page data for brand extraction."""

    url: str = ""
    title: str = ""
    metadata: dict | None = None


class SiteData(BaseModel):
    """Minimal site data for brand extraction."""

    description: str = ""
    logo_url: str = ""
    favicon: str = ""
    metadata: dict | None = None
    visual_identity: dict | None = None


class AnalysisData(BaseModel):
    """Analysis data structure for brand extraction."""

    pages: list[PageData] = Field(default_factory=list)
    site: SiteData | None = None


class BrandExtractRequest(BaseModel):
    """Request to extract brand from analysis data."""

    analysis: AnalysisData = Field(..., description="Analysis result data")
    llm_config: LLMConfigOverride | None = None


class FontInfo(BaseModel):
    """Typography information."""

    family: str = ""
    category: str = ""  # serif, sans-serif, monospace, display, handwriting
    role: str = ""  # heading, body, accent
    weights: list[str] = Field(default_factory=list)
    is_google_font: bool = False
    source_url: str = ""


class ColorInfo(BaseModel):
    """Color with WCAG accessibility info."""

    hex: str = ""
    role: str = ""  # primary, secondary, accent, complementary, analogous, light, dark
    contrast_ratio_white: float = 0.0
    contrast_ratio_black: float = 0.0
    wcag_aa_text: bool = False
    wcag_aaa_text: bool = False


class ColorPalette(BaseModel):
    """Full color palette derived from primary brand color."""

    primary: ColorInfo | None = None
    secondary: ColorInfo | None = None
    accent: ColorInfo | None = None
    complementary: ColorInfo | None = None
    analogous: list[ColorInfo] = Field(default_factory=list)
    light_variant: ColorInfo | None = None
    dark_variant: ColorInfo | None = None
    recommended_text_color: str = "#000000"
    colors: list[ColorInfo] = Field(default_factory=list)


class LogoAsset(BaseModel):
    """Stored logo asset reference."""

    asset_id: str = ""
    original_url: str = ""
    stored_path: str = ""
    content_type: str = ""
    size_bytes: int = 0
    width: int | None = None
    height: int | None = None
    source: str = ""  # og:image, favicon, logo-tag, manifest


class SocialImages(BaseModel):
    """Social media image URLs."""

    og_image: str = ""
    twitter_image: str = ""
    apple_touch_icon: str = ""


class SchemaOrgData(BaseModel):
    """Schema.org structured data."""

    type: str = ""  # Organization, LocalBusiness, WebSite
    name: str = ""
    url: str = ""
    logo: str = ""
    description: str = ""
    same_as: list[str] = Field(default_factory=list)


class StageIssueSchema(BaseModel):
    """A warning or error from a pipeline stage."""

    stage: str = ""
    severity: str = ""
    message: str = ""


class ExtractionReport(BaseModel):
    """Structured report of pipeline stage execution."""

    stages_run: list[str] = Field(default_factory=list)
    stages_succeeded: list[str] = Field(default_factory=list)
    stages_failed: list[str] = Field(default_factory=list)
    warnings: list[StageIssueSchema] = Field(default_factory=list)
    errors: list[StageIssueSchema] = Field(default_factory=list)
    total_duration_ms: int = 0


class ExtractionMetadata(BaseModel):
    """Metadata about the extraction process."""

    extracted_at: datetime | None = None
    source: str = ""  # "analysis" or "url"
    confidence: float = 0.0
    extraction_duration_ms: int = 0
    extractor_version: str = "1.0.0"


class BrandFromUrlRequest(BaseModel):
    """Request to extract brand from a URL."""

    url: str = Field(..., description="URL to extract brand from")
    download_logo: bool = Field(default=False, description="Download and store logo")
    include_fonts: bool = Field(default=True, description="Include font extraction")
    include_colors: bool = Field(default=True, description="Include color palette")
    include_metadata: bool = Field(default=True, description="Include extraction metadata")
    config: dict[str, Any] | None = Field(default=None, description="Optional extraction config")
    llm_config: LLMConfigOverride | None = None


class BrandResponse(BaseModel):
    """Extracted brand information."""

    name: str = ""
    domain: str = ""
    logo_url: str = ""
    primary_color: str = "#2563EB"
    secondary_color: str = "#1E40AF"
    has_logo: bool = False
    logo_asset: LogoAsset | None = None
    logo_serve_url: str = ""
    color_palette: ColorPalette | None = None
    fonts: list[FontInfo] = Field(default_factory=list)
    social_images: SocialImages | None = None
    schema_org: SchemaOrgData | None = None
    manifest_theme_color: str = ""
    favicon_urls: list[str] = Field(default_factory=list)
    metadata: ExtractionMetadata | None = None
    # Round 2 fields (all with defaults for backward compat)
    tagline: str = ""
    brand_voice: str = ""
    # Round 3 fields
    visual_dominant_colors: list[str] = Field(default_factory=list)
    brand_personality: list[str] = Field(default_factory=list)
    tone_result: dict[str, Any] | None = None
    content_analysis: dict[str, Any] | None = None
    social_profiles: dict[str, Any] | None = None
    industry: dict[str, Any] | None = None
    logo_intelligence: dict[str, Any] | None = None
    quality_report: dict[str, Any] | None = None
    extraction_methods: list[str] = Field(default_factory=list)
    extraction_report: ExtractionReport | None = None
    font_family_primary: str = ""
    font_family_secondary: str = ""
    is_user_override: bool = False
    token_usage: dict[str, Any] | None = None


class LogoDownloadRequest(BaseModel):
    """Request to download and store a logo.

    Carries no tenant field. It used to have one (``tenant_id``, a body-supplied
    "override"), and that made the request the authority on which tenant's blob
    prefix got written: ``LogoService.download_and_store`` calls
    ``storage.store(tenant_id, ...)`` *before* the route inserts the asset row,
    so RLS's ``WITH CHECK`` refused the row while the image had already landed
    in the victim tenant's object storage. The tenant now comes only from the
    authenticated identity on the request.

    ``extra="forbid"`` is the other half of that removal. Pydantic's default is
    ``extra="ignore"``, which means a client still sending the old ``tenant_id``
    gets a **200 against a different tenant than it asked for** and no
    indication that the field it supplied was dropped. Silently doing the right
    thing while the caller believes it asked for something else is the worst of
    the available behaviours for a field that was removed on tenant-isolation
    grounds: it is indistinguishable, from the caller's side, from the
    vulnerable version still working. A 422 naming the unexpected key says
    exactly what changed.

    Verified safe before enabling: no service in the monorepo POSTs to
    ``/api/brand/logo/download`` at all. The orchestrator's
    ``brand_extractor_client`` posts ``{"url", "download_logo"}`` to the
    *different* ``/api/brand/from-url`` route, which is unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., description="Logo URL to download")


class LogoAssetResponse(BaseModel):
    """Response after logo download/storage."""

    asset_id: str = ""
    stored_path: str = ""
    content_type: str = ""
    size_bytes: int = 0
    serve_url: str = ""


class BrandListResponse(BaseModel):
    """Paginated list of brands."""

    items: list[BrandResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
    has_next: bool = False


class BrandHistoryEntry(BaseModel):
    """Single version in brand history."""

    version: int = 1
    data: dict = Field(default_factory=dict)
    changed_fields: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class BrandHistoryResponse(BaseModel):
    """Brand version history."""

    domain: str = ""
    current_version: int = 1
    history: list[BrandHistoryEntry] = Field(default_factory=list)


class BatchExtractRequest(BaseModel):
    """Request to batch extract brands from multiple URLs."""

    urls: list[str] = Field(
        ..., min_length=1, max_length=50, description="URLs to extract brands from"
    )
    llm_config: LLMConfigOverride | None = None


class BatchUpstreamError(BaseModel):
    """A batch-wide upstream condition the CALLER has to fix.

    Distinct from ``BatchExtractResponse.errors`` on purpose. ``errors`` is
    per-URL and opaque (a string); this is the ONE tenant-level fact that
    applies to every URL in the batch — today ``byo_llm_not_configured`` from
    ``worker_management``. Repeating it 50 times as 50 opaque strings told the
    SPA nothing it could branch on, and the batch still answered 200. Carrying
    the same ``code`` the ``/api/brand/from-url`` 402 envelope carries lets one
    frontend branch render the same "Open Settings" call-to-action.
    """

    code: str = Field(default="", description="Machine-readable upstream code")
    message: str = Field(default="", description="Human-readable upstream message")
    status: int = Field(default=0, description="Upstream HTTP status, e.g. 402")
    upstream: str = Field(default="", description="Downstream service that reported it")


class BatchExtractResponse(BaseModel):
    """Response for batch extraction."""

    results: dict[str, BrandResponse] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    #: Set when a caller-fixable upstream condition affected the whole batch.
    #: ``results`` still holds every brand that could be built without it.
    upstream_error: BatchUpstreamError | None = None


class CacheInvalidationResponse(BaseModel):
    """Response after cache invalidation."""

    domain: str = ""
    invalidated: bool = False
    message: str = ""


# --- Round 2: New request/response schemas ---


class BrandOverrideRequest(BaseModel):
    """Request to apply manual brand overrides."""

    domain: str = Field(..., description="Domain to override")
    overrides: dict = Field(..., description="Override fields (name, primary_color, etc.)")


class BrandOverrideResponse(BaseModel):
    """Response after applying brand overrides."""

    domain: str = ""
    overridden_fields: list[str] = Field(default_factory=list)
    new_version: int = 0
    brand: BrandResponse | None = None


class BrandComparisonRequest(BaseModel):
    """Request to compare two brand identities."""

    domain_a: str = Field(..., description="First domain to compare")
    domain_b: str = Field(..., description="Second domain to compare")
    extract_if_missing: bool = Field(default=False, description="Extract brands if not cached")


class FieldComparison(BaseModel):
    """Comparison of a single field between two brands."""

    field: str = ""
    value_a: str = ""
    value_b: str = ""
    match: bool = False


class BrandComparisonResponse(BaseModel):
    """Response from comparing two brand identities."""

    domain_a: str = ""
    domain_b: str = ""
    brand_a: BrandResponse | None = None
    brand_b: BrandResponse | None = None
    overall_similarity: float = 0.0
    field_comparisons: list[FieldComparison] = Field(default_factory=list)
    color_distance: float = 0.0


class BrandExportResponse(BaseModel):
    """Pipeline-optimized flat export format."""

    name: str = ""
    domain: str = ""
    logo_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""
    heading_font: str = ""
    body_font: str = ""
    tagline: str = ""
    tone: str = ""
    css_variables: dict[str, str] = Field(default_factory=dict)
    google_fonts_url: str = ""
    social_profiles: dict = Field(default_factory=dict)
    has_logo: bool = False
    favicon_url: str = ""
