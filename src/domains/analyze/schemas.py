"""Request/response Pydantic models for analyzer_service API."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PageType(StrEnum):
    """Enumeration of recognized page types for classification."""

    HOMEPAGE = "homepage"
    PRICING = "pricing"
    BLOG_INDEX = "blog_index"
    BLOG_POST = "blog_post"
    PRODUCT_LISTING = "product_listing"
    PRODUCT = "product"
    DOCUMENTATION = "documentation"
    ABOUT = "about"
    CONTACT = "contact"
    FAQ = "faq"
    GALLERY = "gallery"
    DASHBOARD = "dashboard"
    LOGIN = "login"
    FORM = "form"
    LANDING = "landing"
    UNKNOWN = "unknown"


# --- Input models ---


class CrawledPageInput(BaseModel):
    """Input model for a single crawled page."""

    page_id: str = Field(default="", max_length=100)
    url: str = Field(..., max_length=2048)
    title: str = Field(default="", max_length=500)
    html: str = Field(default="", max_length=5_000_000)
    text: str = Field(default="", max_length=1_000_000)
    word_count: int | None = None


class LLMConfigOverride(BaseModel):
    """Optional per-request LLM configuration override."""

    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


class AnalysisConfig(BaseModel):
    """Configuration for analysis behavior."""

    enable_language_dedup: bool = True
    enable_spa_detection: bool = True
    enable_context_enrichment: bool = True
    llm_model: str | None = None
    max_pages: int = 100
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    timeout_seconds: int = Field(default=300, ge=10, le=600)
    parallel_workers: int = Field(default=4, ge=1, le=16)
    include_narrative: bool = True
    custom_prompts: dict[str, str] | None = None


class IntelligenceContext(BaseModel):
    """Typed user-provided context for analysis."""

    description: str | None = None
    site_type: str | None = None
    target_audience: str | None = None
    goals: list[str] = []
    focus_pages: list[str] = []
    exclude_pages: list[str] = []


class AnalyzeRequest(BaseModel):
    """Request body for full site analysis."""

    pages: list[CrawledPageInput] = Field(..., min_length=1, max_length=100)
    user_context: IntelligenceContext | dict | None = None
    keep_lang: str = "en"
    auto_enrich_context: bool = False
    config: AnalysisConfig = Field(default_factory=AnalysisConfig)
    llm_config: LLMConfigOverride | None = None


class ClassifyRequest(BaseModel):
    """Request body for page classification only."""

    pages: list[CrawledPageInput] = Field(..., min_length=1)


class NarrativeRequest(BaseModel):
    """Request body for narrative extraction."""

    pages: list[dict] = Field(..., min_length=1)


# --- Output models ---


class InteractiveElementResponse(BaseModel):
    """An interactive HTML element discovered on a page."""

    tag: str
    selector: str
    text: str = ""
    element_type: str = ""
    name: str = ""
    aria_label: str = ""


class ContentSectionResponse(BaseModel):
    """A content section identified within a page."""

    section_type: str
    heading: str = ""
    text_preview: str = ""
    selector: str = ""
    word_count: int = 0


class NavigationElementResponse(BaseModel):
    """A navigation element (menu, breadcrumb) found on a page."""

    nav_type: str
    items: list[str] = []
    selector: str = ""


class PageMetadata(BaseModel):
    """Rich metadata extracted from a page (OG tags, JSON-LD, canonical, etc.)."""

    og_tags: dict = Field(default_factory=dict)
    json_ld: list[dict] = Field(default_factory=list)
    canonical_url: str = ""
    language: str = ""
    robots: dict = Field(default_factory=dict)


class PageAnalysisResponse(BaseModel):
    """Full analysis result for a single page."""

    page_id: str = ""
    url: str
    title: str = ""
    page_type: PageType = PageType.UNKNOWN
    interactive_elements: list[InteractiveElementResponse] = []
    content_sections: list[ContentSectionResponse] = []
    navigation: list[NavigationElementResponse] = []
    headings: list[str] = []
    word_count: int = 0
    has_forms: bool = False
    has_media: bool = False
    is_spa_section: bool = False
    meta_description: str = ""
    metadata: PageMetadata = Field(default_factory=PageMetadata)
    logo_candidates: list[str] = []


class SiteAnalysisResponse(BaseModel):
    """Site-level analysis produced by LLM."""

    description: str = ""
    target_audience: list[str] = []
    use_cases: list[str] = []
    selling_points: list[str] = []
    narrative_hooks: list[str] = []
    tone: str = ""
    visual_identity: dict = {}


class NarrativeExtractionResponse(BaseModel):
    """Narrative material extracted from page content."""

    hooks: list[str] = []
    key_quotes: list[str] = []
    feature_highlights: list[str] = []
    total_pages: int = 0
    pages_with_forms: int = 0
    pages_with_media: int = 0


class ReviewType(StrEnum):
    """Available review assessment types."""

    DESIGN = "design"
    USABILITY = "usability"
    FEATURE = "feature"
    CONTENT_QUALITY = "content_quality"


class ReviewRequest(BaseModel):
    """Request body for generating reviews from a completed analysis."""

    analysis_job_id: str = Field(..., description="Job ID of completed analysis to review")
    review_types: list[ReviewType] = Field(default_factory=lambda: [ReviewType.DESIGN])
    user_context: dict | None = None
    llm_config: LLMConfigOverride | None = None


class ReviewCategoryResult(BaseModel):
    """Scored result for a single review category."""

    score: float = 0.0
    observations: list[str] = []
    issues: list[str] = []
    recommendations: list[str] = []


class ReviewResult(BaseModel):
    """Complete review result with scores and recommendations."""

    review_type: str
    overall_score: float = 0.0
    categories: dict[str, ReviewCategoryResult] = {}
    strengths: list[str] = []
    weaknesses: list[str] = []
    issues: list[str] = []
    recommendations: list[str] = []
    raw_result: dict = Field(default_factory=dict)


class ReviewJobResponse(BaseModel):
    """Response when a review job is created."""

    review_job_id: str
    status: str
    review_types: list[str] = []


class ReviewResultResponse(BaseModel):
    """Response containing review job status and results."""

    review_job_id: str
    status: str
    reviews: dict[str, ReviewResult] = {}
    error: str | None = None
    processing_time_seconds: float | None = None
    llm_calls: int = 0


class AnalysisJobResponse(BaseModel):
    """Response when an analysis job is created."""

    job_id: str
    status: str
    total_pages: int | None = None
    created_at: str | None = None


class AnalysisResultResponse(BaseModel):
    """Full analysis result including pages, site analysis, and narrative."""

    job_id: str
    status: str
    pages: list[PageAnalysisResponse] = []
    site_analysis: SiteAnalysisResponse | None = None
    narrative_extraction: NarrativeExtractionResponse | None = None
    enriched_context: dict | None = None
    brand: BrandInfo | None = None
    language_duplicates_removed: int = 0
    error: str | None = None
    processing_time_seconds: float | None = None
    llm_calls: int = 0
    schema_version: str = "1.0"
    pages_analyzed_count: int = 0
    token_usage: dict | None = None


class ClassifyResponse(BaseModel):
    """Response containing page classification results."""

    pages: list[PageAnalysisResponse]


class NarrativeResponse(BaseModel):
    """Response containing narrative extraction results."""

    extraction: NarrativeExtractionResponse


class CancelJobResponse(BaseModel):
    """Response confirming job cancellation."""

    job_id: str
    status: str
    message: str


class AnalysisJobListItem(BaseModel):
    """Summary item for job listing responses."""

    job_id: str
    status: str
    total_pages: int | None = None
    created_at: str | None = None


class AnalysisJobListResponse(BaseModel):
    """Paginated list of analysis jobs."""

    jobs: list[AnalysisJobListItem] = []
    total: int = 0
    limit: int = 20
    offset: int = 0


class LLMArtifact(BaseModel):
    """Details of a single LLM API call for debugging."""

    type: str = "llm_call"
    model: str = ""
    prompt_tokens: int = 0
    response_tokens: int = 0
    duration_ms: float = 0.0
    status: str = ""
    prompt_preview: str = ""
    response_preview: str = ""
    error: str = ""


class ArtifactsResponse(BaseModel):
    """Response containing LLM artifacts for a job."""

    job_id: str
    artifacts: list[LLMArtifact] = []


class BrandInfo(BaseModel):
    """Extracted brand identity information."""

    name: str = ""
    domain: str = ""
    logo_url: str = ""
    primary_color: str = ""
    secondary_color: str = ""


class ContextProfileCreate(BaseModel):
    """Request body for creating a user context profile."""

    name: str = Field(..., min_length=1, max_length=255)
    context_data: dict
    is_default: bool = False


class ContextProfileResponse(BaseModel):
    """Response model for a saved user context profile."""

    id: int
    tenant_id: str
    profile_name: str
    context_data: dict
    is_default: bool = False
    created_at: str | None = None
    updated_at: str | None = None
