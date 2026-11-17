"""Request/response Pydantic models for critique_service."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AuthInfo(BaseModel):
    """Authentication info the orchestrator forwards on behalf of the
    end-user so analysis_service can access an authenticated target site.

    Three modes are supported in this MVP:

      - ``type="none"`` (default)   no auth applied
      - ``type="basic"``            HTTP Basic with username + password
      - ``type="bearer"``           Authorization: Bearer <token>
      - ``type="cookies"``          replay a pre-collected cookie jar

    Notes / non-goals for this MVP:

      - Form-login (POST username/password to login_url, persist session
        cookies for subsequent requests) is intentionally NOT implemented
        here. Callers wanting form-login should perform the login
        themselves and pass the resulting session cookies via
        ``type="cookies"``.
      - MFA / OAuth / SAML flows are out of scope.
      - This schema is consumed today by ``_fetch_html`` in
        ``services/critique_service/helpers.py``. Per-scanner threading
        (security_scanner, network_checks, code_quality_scanner, axe-core
        Playwright context, ...) is tracked as a follow-up — they each
        spin their own ``httpx.AsyncClient`` and will see unauthenticated
        traffic until they're updated. For sites that gate ONLY the page
        HTML (the common case), the MVP is enough.
    """

    type: Literal["none", "basic", "bearer", "cookies"] = "none"

    # type="basic"
    username: str | None = None
    password: str | None = None

    # type="bearer"
    token: str | None = None

    # type="cookies" — list of {name, value, domain?} entries, or a raw
    # Cookie header string. Use the list form for clarity.
    cookies: list[dict[str, str]] | None = None
    cookie_header: str | None = None

    # Optional URL the user logs into before triggering the critique;
    # informational today (not auto-driven), useful for the future
    # form-login implementation.
    login_url: str | None = None

    @model_validator(mode="after")
    def _required_fields_present(self) -> AuthInfo:
        if self.type == "basic":
            if not self.username or self.password is None:
                raise ValueError("auth_info type=basic requires username and password")
        elif self.type == "bearer":
            if not self.token:
                raise ValueError("auth_info type=bearer requires token")
        elif self.type == "cookies" and not self.cookies and not self.cookie_header:
            raise ValueError("auth_info type=cookies requires cookies or cookie_header")
        return self


class LLMConfigOverride(BaseModel):
    """Optional overrides for the LLM provider used during critique."""

    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None


class CritiqueCategory(StrEnum):
    """Categories of critique findings."""

    seo = "seo"
    security = "security"
    accessibility = "accessibility"
    performance = "performance"
    ux = "ux"
    code_quality = "code_quality"
    cro = "cro"


class CritiqueSeverity(StrEnum):
    """Severity levels for critique findings."""

    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class FindingResponse(BaseModel):
    """A single critique finding."""

    finding_id: str
    category: str
    severity: str
    title: str
    description: str
    url: str = ""
    recommendation: str = ""
    selector: str | None = None
    check_id: str | None = None
    wcag_criterion: str | None = None
    evidence: str | None = None
    source_tool: str | None = None
    false_positive_risk: str | None = None
    remediation_effort: str | None = None


class WebhookConfig(BaseModel):
    """Webhook notification configuration for a job."""

    events: list[str] = Field(
        default_factory=lambda: ["completed"],
        description="Events to fire webhooks for",
    )
    severity_threshold: str | None = Field(
        default=None, description="Fire alert webhook for findings at or above this severity"
    )


class CritiqueRequest(BaseModel):
    """Request to start a website critique.

    Exactly one content source is required — one of ``url``, ``html``, or
    ``artifact_ref``:

      - ``url``           crawl a live, reachable page (the historical path;
                          runs every category incl. live-only checks).
      - ``html``          score a pre-rendered HTML string in place (no fetch).
      - ``artifact_ref``  read a stored object out of **analysis_service's own
                          MinIO bucket** (S2S) and score it in place. The key is
                          confined server-side to the calling tenant's prefix —
                          see ``CritiqueHelpersMixin._artifact_object_key``.

                          ⚠️ This source is **not usable today**: the read
                          resolves against this service's own bucket (whose only
                          writer is the brand-logo upload), NOT against
                          html_render_service's store, so a ref naming a
                          rendered artifact returns "not found". Use ``html``
                          instead until the follow-up described on
                          ``CritiqueHelpersMixin.ARTIFACT_BUCKET`` lands.

    For ``html``/``artifact_ref`` (no live origin) the live-only work is skipped
    — ``seo_live`` (robots/sitemap/llms.txt), the live-security checks
    (headers/TLS/CORS/exposed-files), and the external adapters (axe/lighthouse).
    The static scanners (seo base, accessibility, performance, ux, cro, and the
    HTML-only security subset) run as usual. Which categories were scored vs
    skipped is surfaced in the job's ``progress`` (``input_mode``,
    ``scored_categories``, ``skipped_categories``) — never silently.
    """

    url: str | None = Field(default=None, description="URL to critique (crawl a live page)")
    html: str | None = Field(
        default=None,
        description="Pre-rendered HTML to score in lieu of a URL (skips the fetch; max 10MB).",
    )
    artifact_ref: str | None = Field(
        default=None,
        description=(
            "Relative object key to read S2S and score in lieu of a URL, "
            "resolved strictly inside the calling tenant's own prefix. The "
            "bucket is fixed server-side and is analysis_service's own bucket, "
            "NOT html_render_service's — so refs naming a rendered artifact "
            "currently return 'not found' (see CritiqueRequest docstring). "
            "The key is used literally: percent-encoded refs, URI forms "
            "(minio://, s3://, file://, http://) and any key escaping the "
            "tenant prefix are rejected with 400."
        ),
    )
    categories: list[str] | None = Field(
        default=None,
        description="Categories to scan (default: all). Options: seo, security, accessibility, performance, ux, code_quality",
    )
    max_pages: int = Field(default=1, ge=1, le=50, description="Maximum pages to scan")
    instructions: str | None = Field(
        default=None, description="Additional instructions for the critique"
    )
    github_repo: str | None = Field(
        default=None, description="GitHub repository URL for code quality analysis"
    )
    force_rescan: bool = Field(default=False, description="Force rescan, bypassing cache")
    callback_url: str | None = Field(
        default=None, description="Webhook URL to notify on completion"
    )
    site_analysis: dict | None = Field(
        default=None,
        description="Pre-computed site analysis from the orchestrator (tech stack, site type)",
    )
    tags: list[str] | None = Field(default=None, description="Tags for this job")
    webhook_config: WebhookConfig | None = Field(
        default=None, description="Webhook notification config"
    )
    llm_config: LLMConfigOverride | None = None
    auth_info: AuthInfo | None = Field(
        default=None,
        description=(
            "Auth applied when fetching the target URL HTML (basic / bearer / "
            "cookies). See AuthInfo for the MVP scope + per-scanner gaps."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> CritiqueRequest:
        # Mirrors the cro/seo_audit contract: exactly one content source.
        sources = [
            bool(self.url and self.url.strip()),
            bool(self.html and self.html.strip()),
            bool(self.artifact_ref and self.artifact_ref.strip()),
        ]
        if sum(sources) != 1:
            raise ValueError("exactly one of 'url', 'html', or 'artifact_ref' is required")
        return self


class CritiqueJobResponse(BaseModel):
    """Response when a critique job is created."""

    job_id: str
    status: str = "pending"
    poll_url: str


class CritiqueResultResponse(BaseModel):
    """Full critique result."""

    job_id: str
    status: str
    url: str
    findings: list[FindingResponse] | None = None
    category_scores: dict[str, float] | None = None
    overall_score: float | None = None
    confidence_scores: dict[str, float] | None = None
    summary: str | None = None
    scan_duration: float | None = None
    pages_scanned: int | None = None
    created_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    progress: dict | None = None
    token_usage: dict | None = None


class CritiqueListResponse(BaseModel):
    """Paginated list of critique jobs."""

    items: list[CritiqueResultResponse]
    total: int
    page: int
    per_page: int


class CritiqueReportResponse(BaseModel):
    """Markdown report response."""

    job_id: str
    url: str
    report: str
    format: str = "markdown"


class FindingsListResponse(BaseModel):
    """Paginated list of findings for a job."""

    job_id: str
    findings: list[FindingResponse]
    total: int
    page: int
    per_page: int


class ComparisonResponse(BaseModel):
    """Comparison between two critique jobs."""

    job_id_1: str
    job_id_2: str
    url_1: str
    url_2: str
    score_deltas: dict[str, float] = Field(default_factory=dict)
    overall_delta: float | None = None
    new_findings: int = 0
    resolved_findings: int = 0
    persistent_findings: int = 0


class CategoryScoreDetail(BaseModel):
    """Category score with grade."""

    score: float
    grade: str


class CritiqueJSONReportResponse(BaseModel):
    """Structured JSON report response."""

    url: str
    domain: str | None = None
    #: ``None`` when no category has verified evidence ("Not scored"): no number, no grade.
    overall_score: float | None
    grade: str | None
    category_scores: dict[str, CategoryScoreDetail] = Field(default_factory=dict)
    top_issues: list[FindingResponse] = Field(default_factory=list)
    findings_by_category: dict[str, list[FindingResponse]] = Field(default_factory=dict)
    summary: str = ""
    metadata: dict = Field(default_factory=dict)


class BatchCritiqueRequest(BaseModel):
    """Request to start a batch critique."""

    urls: list[str] = Field(..., min_length=1, max_length=50, description="URLs to critique")
    categories: list[str] | None = Field(default=None, description="Categories to scan")
    max_pages: int = Field(default=1, ge=1, le=50, description="Max pages per URL")
    instructions: str | None = Field(default=None, description="Additional instructions")
    callback_url: str | None = Field(default=None, description="Webhook URL on batch completion")
    llm_config: LLMConfigOverride | None = None


class BatchCritiqueResponse(BaseModel):
    """Response when a batch critique is created."""

    batch_id: str
    job_ids: list[str]
    total: int
    status: str = "pending"


class BatchStatusResponse(BaseModel):
    """Batch critique status."""

    batch_id: str
    status: str
    total: int
    completed: int
    failed: int
    job_ids: list[str]


# ============================================================================
# Analytics schemas (WS-5)
# ============================================================================


class TrendDataPoint(BaseModel):
    """A single point on a score trend timeline."""

    job_id: str
    date: str | None = None
    overall_score: float | None = None
    category_scores: dict[str, float] = Field(default_factory=dict)


class DomainTrendResponse(BaseModel):
    """Score trends for a domain."""

    domain: str
    days: int
    data_points: list[TrendDataPoint] = Field(default_factory=list)


class TopFindingResponse(BaseModel):
    """A top recurring finding."""

    check_id: str | None = None
    title: str
    category: str
    severity: str
    count: int
    percentage: float = 0.0


class DomainAnalyticsResponse(BaseModel):
    """Aggregate analytics for a domain."""

    domain: str
    total_scans: int
    avg_score: float | None = None
    latest_score: float | None = None
    score_trend: str = "stable"
    top_recurring_findings: list[TopFindingResponse] = Field(default_factory=list)
    category_averages: dict[str, float] = Field(default_factory=dict)


class DomainSummaryItem(BaseModel):
    """Summary item for a single domain."""

    domain: str | None = None
    scan_count: int
    latest_score: float | None = None
    last_scanned: str | None = None


class TenantAnalyticsResponse(BaseModel):
    """Cross-domain overview for a tenant."""

    tenant_id: str
    total_scans: int
    total_domains: int
    avg_score: float | None = None
    domains: list[DomainSummaryItem] = Field(default_factory=list)


# ============================================================================
# Suppression schemas (WS-6)
# ============================================================================


class CreateSuppressionRequest(BaseModel):
    """Request to create a finding suppression rule."""

    domain: str | None = Field(default=None, description="Domain to suppress findings for")
    check_id: str | None = Field(default=None, description="Check ID to suppress")
    category: str | None = Field(default=None, description="Category to suppress")
    reason: str | None = Field(default=None, description="Reason for suppression")
    status: str = Field(default="suppressed", description="suppressed or acknowledged")
    expires_at: str | None = Field(
        default=None, description="ISO datetime when suppression expires"
    )


class SuppressionResponse(BaseModel):
    """Suppression rule response."""

    id: str
    tenant_id: str
    domain: str | None = None
    check_id: str | None = None
    category: str | None = None
    reason: str | None = None
    status: str
    expires_at: str | None = None
    created_at: str | None = None


class SuppressionListResponse(BaseModel):
    """List of suppression rules."""

    items: list[SuppressionResponse]
    total: int


class AcknowledgeFindingRequest(BaseModel):
    """Request to acknowledge a specific finding."""

    reason: str | None = Field(default=None, description="Reason for acknowledgement")


class TagsUpdateRequest(BaseModel):
    """Request to update job tags."""

    tags: list[str] = Field(..., description="Tags to set on the job")


# ============================================================================
# Schedule schemas (WS-8)
# ============================================================================


class CreateScheduleRequest(BaseModel):
    """Request to create a scheduled scan."""

    url: str = Field(..., description="URL to scan on schedule")
    categories: list[str] | None = Field(default=None, description="Categories to scan")
    cron_expression: str = Field(
        default="@daily",
        description="Schedule: @hourly, @daily, @weekly, @every_6h, @every_12h",
    )


class UpdateScheduleRequest(BaseModel):
    """Request to update a scheduled scan."""

    url: str | None = None
    categories: list[str] | None = None
    cron_expression: str | None = None
    enabled: bool | None = None


class ScheduleResponse(BaseModel):
    """Scheduled scan response."""

    schedule_id: str
    tenant_id: str
    url: str
    categories: list[str] | None = None
    cron_expression: str
    enabled: bool
    last_run_at: str | None = None
    next_run_at: str | None = None
    last_job_id: str | None = None
    run_count: int = 0
    created_at: str | None = None


class ScheduleListResponse(BaseModel):
    """List of scheduled scans."""

    items: list[ScheduleResponse]
    total: int


# ============================================================================
# Scoring / Config schemas (WS-9/10)
# ============================================================================


class ScoringWeightsResponse(BaseModel):
    """Current scoring weights."""

    weights: dict[str, float] = Field(default_factory=dict)


class ScoringWeightsUpdateRequest(BaseModel):
    """Request to update scoring weights at runtime."""

    weights: dict[str, float] = Field(..., description="Weights to update")


class ConfigOverridesResponse(BaseModel):
    """Runtime config overrides."""

    overrides: dict[str, dict[str, str]] = Field(default_factory=dict)


# ============================================================================
# Historical comparison (WS-10)
# ============================================================================


class FindingChangeEvent(BaseModel):
    """A finding that changed between scans."""

    check_id: str | None = None
    title: str
    category: str
    severity: str
    change: str  # "new", "resolved", "recurred"


class HistoricalComparisonResponse(BaseModel):
    """Historical comparison across jobs for a domain."""

    domain: str
    jobs_compared: int
    new_findings: list[FindingChangeEvent] = Field(default_factory=list)
    resolved_findings: list[FindingChangeEvent] = Field(default_factory=list)
    recurred_findings: list[FindingChangeEvent] = Field(default_factory=list)


# ============================================================================
# Rerun schema (WS-10)
# ============================================================================


class RerunRequest(BaseModel):
    """Request to partially re-run a job with limited categories."""

    categories: list[str] | None = Field(
        default=None, description="Categories to re-run (default: same as original)"
    )
