"""Request/response Pydantic models for review domain."""

from __future__ import annotations

from pydantic import BaseModel, Field

REVIEW_TYPES = [
    "design_review",
    "usability_review",
    "feature_review",
    "content_quality_review",
    "feature_suggestions",
]


class SiteAnalysisInput(BaseModel):
    """Site-level metadata including description, audience, and visual identity."""

    description: str = ""
    target_audience: list[str] = []
    use_cases: list[str] = []
    selling_points: list[str] = []
    narrative_hooks: list[str] = []
    tone: str = ""
    visual_identity: dict = {}


class PageInput(BaseModel):
    """Single page data including URL, content metrics, and interactive elements."""

    url: str
    title: str = ""
    page_type: str = "unknown"
    word_count: int = 0
    interactive_elements: list[dict] = []
    content_sections: list[dict] = []
    headings: list[str] = []
    has_forms: bool = False
    has_media: bool = False
    html: str | None = None
    text: str | None = None
    css_colors: list[str] = []
    media_queries: list[str] = []
    font_families: list[str] = []
    heading_structure: list[dict] = []


class NarrativeInput(BaseModel):
    """Narrative context extracted from site content for review enrichment."""

    hooks: list[str] = []
    key_quotes: list[str] = []
    feature_highlights: list[str] = []
    total_pages: int = 0
    pages_with_forms: int = 0
    pages_with_media: int = 0


class AnalysisInput(BaseModel):
    """Combined analysis input with pages, site metadata, and narrative data."""

    pages: list[PageInput] = []
    site: SiteAnalysisInput = Field(default_factory=SiteAnalysisInput)
    narrative: NarrativeInput | None = None


# --- Phase 2: User Context ---


class UserContext(BaseModel):
    """User-provided context that shapes review behavior."""

    critique_focus: list[str] = Field(default_factory=list, description="Review types to focus on")
    target_audience: str = Field(default="", description="Target audience description")
    focus_areas: list[str] = Field(default_factory=list, description="Specific areas to focus on")
    tone: str = Field(
        default="professional",
        description="Review tone: professional, constructive, critical, encouraging",
    )
    goals: list[str] = Field(default_factory=list, description="Business goals to evaluate against")


class LLMConfigOverride(BaseModel):
    """Optional per-request LLM configuration override."""

    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


class ReviewRequest(BaseModel):
    """Request body for starting a new review job."""

    analysis: AnalysisInput
    review_types: list[str] = Field(default_factory=lambda: REVIEW_TYPES)
    message: str = ""
    callback_url: str | None = None
    user_context: UserContext | None = None
    llm_config: LLMConfigOverride | None = None


class ReviewJobResponse(BaseModel):
    """Response returned when a review job is created or listed."""

    job_id: str
    status: str
    review_types: list[str] = []
    created_at: str | None = None


class ReviewResultResponse(BaseModel):
    """Response containing review job status and results."""

    job_id: str
    status: str
    results: dict[str, dict] = {}
    error: str | None = None
    token_usage: dict | None = None


class ReviewListResponse(BaseModel):
    """Paginated list of review jobs."""

    jobs: list[ReviewJobResponse]
    total: int


class ReviewSummary(BaseModel):
    """Executive summary with aggregate scoring across review types."""

    job_id: str
    aggregate_score: float | None = None
    grade: str | None = None
    per_review_scores: dict[str, float | None] = {}
    # Share of the configured aggregate weight actually measured. ``partial``
    # is True whenever any review type contributed no score; below
    # ``MIN_AGGREGATE_COVERAGE`` the aggregate itself is None.
    coverage: float = 0.0
    scored_review_types: list[str] = []
    unscored_review_types: list[str] = []
    partial: bool = False
    top_strengths: list[str] = []
    critical_issues: list[str] = []
    top_recommendations: list[str] = []
    score_100: float | None = None
    penalty_score: float | None = None


# --- Phase 3: Enhanced Output ---


class ReviewFinding(BaseModel):
    """Single structured finding extracted from review results."""

    review_type: str
    category: str = ""
    severity: str = ""
    description: str = ""
    score: float | None = None
    finding_id: str = ""
    evidence: list[str] = []
    remediation_effort: str = "unknown"
    source_data: dict = {}


class FindingsListResponse(BaseModel):
    """Paginated list of structured findings from a completed review."""

    job_id: str
    findings: list[ReviewFinding]
    total: int
    page: int
    per_page: int


class ReviewComparisonResponse(BaseModel):
    """Score deltas and qualitative diffs between two review jobs."""

    job_id_1: str
    job_id_2: str
    score_deltas: dict[str, float | None] = {}
    overall_delta: float | None = None
    grade_change: str | None = None
    new_strengths: list[str] = []
    resolved_weaknesses: list[str] = []
    persistent_issues: int = 0


class ReviewReportResponse(BaseModel):
    """Rendered review report in the requested format."""

    job_id: str
    format: str
    report: str


class ReviewVideoResponse(BaseModel):
    """Response returned when video generation is triggered for a review."""

    review_job_id: str
    video_job_id: str
    video_poll_url: str
    findings_count: int


# --- Phase 4: Operational Excellence ---


class UsageResponse(BaseModel):
    """Daily usage statistics for a single tenant."""

    tenant_id: str
    date: str
    review_count: int
    error_count: int


class UsageListResponse(BaseModel):
    """Paginated list of daily usage records."""

    usage: list[UsageResponse]
    total: int


class DomainHistoryEntry(BaseModel):
    """Single entry in a domain's review history timeline."""

    job_id: str
    aggregate_score: float | None = None
    grade: str | None = None
    review_types: list[str] = []
    created_at: str | None = None


class DomainHistoryResponse(BaseModel):
    """Review history for a domain with score trend analysis."""

    domain: str
    reviews: list[DomainHistoryEntry]
    total: int
    trend: str | None = None


class BatchReviewRequest(BaseModel):
    """Request body for starting multiple reviews in a single batch."""

    reviews: list[ReviewRequest]
    callback_url: str | None = None


class BatchReviewResponse(BaseModel):
    """Response returned when a batch review is created."""

    batch_id: str
    status: str
    total: int
    job_ids: list[str] = []


class BatchStatusResponse(BaseModel):
    """Current status of a batch review including per-job completion counts."""

    batch_id: str
    status: str
    total: int
    completed: int
    failed: int
    job_ids: list[str] = []
