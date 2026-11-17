"""Shared infrastructure settings for analysis_service.

Database / Redis / API / Storage / Cache subclass the ``w2v_common`` bases
(with service-specific overrides); the remaining classes are cross-cutting
infrastructure sections (worker-management gateway, tracing, rate limiting,
webhooks, retention, stale-job reaper, feature flags). ``AuthSettings`` and
``LoggingSettings`` are re-exported unchanged from ``w2v_common``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from w2v_common.config import APISettings as BaseAPISettings
from w2v_common.config import AuthSettings as AuthSettings
from w2v_common.config import CacheSettings as BaseCacheSettings
from w2v_common.config import DatabaseSettings as BaseDatabaseSettings
from w2v_common.config import LoggingSettings as LoggingSettings
from w2v_common.config import RedisSettings as BaseRedisSettings
from w2v_common.config import StorageSettings as BaseStorageSettings

__all__ = [
    "APISettings",
    "AuthSettings",
    "AuthzSettings",
    "CacheSettings",
    "DatabaseSettings",
    "FeatureFlags",
    "LoggingSettings",
    "RateLimitSettings",
    "RedisSettings",
    "RetentionSettings",
    "StorageSettings",
    "TracingSettings",
    "WebhookSettings",
    "WorkerManagementSettings",
]


# ---------------------------------------------------------------------------
# Shared infrastructure settings (imported from w2v_common, with overrides)
# ---------------------------------------------------------------------------


class DatabaseSettings(BaseDatabaseSettings):
    """Database settings — DATABASE_URL required env (no localhost fallback)."""


class RedisSettings(BaseRedisSettings):
    """Redis settings — REDIS_URL required env (no localhost fallback)."""


class APISettings(BaseAPISettings):
    """API settings with analysis_service default port."""

    port: int = Field(default=6303, description="API server port")


class StorageSettings(BaseStorageSettings):
    """Storage settings with analysis_service defaults."""

    minio_bucket: str = Field(default="analysis-service", description="MinIO bucket name")


# ---------------------------------------------------------------------------
# Extended w2v_common config classes with service-specific fields
# ---------------------------------------------------------------------------


class WorkerManagementSettings(BaseSettings):
    """Worker-management gateway configuration.

    All LLM calls in this service route through worker_management. The
    URL is also picked up from the ``WORKER_MANAGEMENT_URL`` env var
    directly by :class:`w2v_common.clients.WMClient`; this section
    exposes the per-call retry / temperature settings that previously
    lived under ``LLMSettings``.
    """

    url: str | None = Field(
        default=None,
        description=(
            "Override for the worker_management base URL. "
            "Defaults to the in-cluster service when unset."
        ),
    )
    timeout_seconds: float = Field(
        default=600.0,
        description=(
            "Per-message HTTP timeout against worker_management. Must cover the "
            "slowest worker — a full-page redesign-html generation (~16k tokens) "
            "runs well past the old 120s. Matches the provider's 600s budget."
        ),
    )
    max_tokens: int = Field(
        default=8000,
        description=(
            "Declared but NOT passed: nothing in this service reads this field, "
            "and no environment sets WM_MAX_TOKENS. It documents the intended "
            "output budget; the library floor is what actually takes effect. "
            "The previous description here was wrong in every clause, and wrong "
            "in the dangerous direction -- it claimed WM's MessageRequest "
            "forbids max_tokens and that the cap could only be set on the "
            "registered worker, which was twice used to argue the output budget "
            "DOWN. In fact WMClient.message() accepts a per-call max_tokens and "
            "puts it in the request body, and worker_management honours it per "
            "call, overriding the registered worker's cap. All 16 "
            "wm_client.message() call sites here omit it, so the effective "
            "budget comes from the client: WMClient.message() always sends a "
            "budget and floors an omitted one at w2v_common's exported "
            "WM_MIN_OUTPUT_TOKENS; before that floor existed, an omitted budget "
            "fell through to WM's own 6000 default. That matters because the "
            "model is a reasoning model whose thinking is charged against "
            "max_tokens: 4000 (the old default here) and 6000 are both RECORDED "
            "PRODUCTION FAILURE VALUES, yielding a terminal post-generation 422 "
            "'Model token limit exceeded before any response was generated'. "
            "8000 is the smallest value with no recorded failure, and is chosen "
            "to match WM_MIN_OUTPUT_TOKENS rather than contradict it. Do not "
            "lower it. Wiring it through to the call sites is separate work, "
            "deliberately out of scope."
        ),
    )
    temperature: float = Field(default=0.2, description="Sampling temperature hint")
    max_retries: int = Field(default=1, description="Retry attempts for transient WM failures")
    retry_delay: float = Field(default=2.0, description="Delay in seconds between WM retries")

    model_config = SettingsConfigDict(
        env_prefix="WM_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class CacheSettings(BaseCacheSettings):
    """Extended cache configuration for analysis_service domains."""

    review_ttl: int = Field(default=3600, description="Review job cache TTL in seconds")
    review_maxsize: int = Field(default=500, description="Review job cache max entries")
    ttl: int = Field(default=3600, description="Default cache TTL in seconds")
    scanner_ttl: int = Field(default=3600, description="Per-scanner cache TTL in seconds")
    domain_invalidation_enabled: bool = Field(
        default=True, description="Enable domain-level cache invalidation tracking"
    )
    brand_ttl: int = Field(default=3600, description="Brand extraction cache TTL in seconds")
    icp_ttl: int = Field(default=3600, description="ICP extraction cache TTL in seconds")


class TracingSettings(BaseSettings):
    """OpenTelemetry tracing configuration."""

    enabled: bool = Field(default=False, description="Enable OpenTelemetry tracing")
    # Empty default — tracing is opt-in (TRACING_ENDPOINT). Exporter never
    # fires when blank; production sets TRACING_ENDPOINT via env.
    endpoint: str = Field(default="", description="OTLP exporter endpoint")
    service_name: str = Field(default="analysis-service", description="Service name for tracing")
    sample_rate: float = Field(default=1.0, description="Trace sampling rate (0.0-1.0)")

    model_config = SettingsConfigDict(
        env_prefix="TRACING_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class RateLimitSettings(BaseSettings):
    """Rate limiting configuration."""

    enabled: bool = Field(default=False, description="Enable rate limiting")
    hourly_limit: int = Field(default=60, description="Max requests per hour per tenant")
    daily_limit: int = Field(default=500, description="Max requests per day per tenant")

    model_config = SettingsConfigDict(
        env_prefix="RATE_LIMIT_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class WebhookSettings(BaseSettings):
    """Webhook notification configuration."""

    enabled: bool = Field(default=True, description="Enable webhook notifications")
    max_retries: int = Field(default=3, description="Maximum webhook delivery retries")
    timeout: int = Field(default=10, description="Webhook delivery timeout in seconds")

    model_config = SettingsConfigDict(
        env_prefix="WEBHOOK_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class RetentionSettings(BaseSettings):
    """Data retention configuration."""

    enabled: bool = Field(default=False, description="Enable data retention cleanup")
    job_retention_days: int = Field(default=90, description="Days to keep completed/failed jobs")
    audit_retention_days: int = Field(default=365, description="Days to keep audit logs")
    cleanup_interval_hours: int = Field(default=24, description="Hours between cleanup runs")

    model_config = SettingsConfigDict(
        env_prefix="RETENTION_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class ReaperSettings(BaseSettings):
    """Stale-job reaper for critique jobs stranded in ``running``.

    Env prefix is ``REAPER_`` and NOT ``CRITIQUE_REAPER_``: ``CRITIQUE_`` is a
    RETIRED prefix (see ``_RETIRED_ENV_PREFIXES``) and any var under it makes
    ``Settings()`` raise at validation time, i.e. the pod would refuse to boot.

    ``enabled`` defaults to True on purpose. This is a crash/rollout backstop,
    not a feature: the failure it covers (a job left ``running`` forever while
    the orchestrator polls it forever) is invisible, so an opt-in default would
    mean the guard is armed nowhere until someone edits a ConfigMap.
    """

    enabled: bool = Field(default=True, description="Enable the stale critique-job reaper")
    stale_after_minutes: int = Field(
        default=60, description="Minutes a job may sit in 'running' before it is reaped"
    )
    #: A SECOND, deliberately much longer cutoff for rows stranded at ``'pending'``
    #: (committed by ``create_job`` before the route's ``asyncio.create_task``, and
    #: matched by neither 0010's reaper nor 0005's prune). It is separate from
    #: ``stale_after_minutes`` and NOT derived from it because ``'pending'`` is also
    #: the LEGITIMATE queued state: collapsing the two onto the 60-minute ``running``
    #: budget would reap live work. 6h is the shape proven on the sibling services.
    pending_stale_after_minutes: int = Field(
        default=360,
        description="Minutes a job may sit in 'pending' (never started) before it is reaped",
    )
    interval_minutes: int = Field(default=10, description="Minutes between reaper sweeps")

    model_config = SettingsConfigDict(
        env_prefix="REAPER_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class AnalyzeReaperSettings(BaseSettings):
    """Stale-job reaper for ``analysis_jobs`` stranded in ``processing``.

    A THIRD sibling of :class:`ReaperSettings` / :class:`ReviewReaperSettings`, for
    the same reason they are siblings of each other: a different table, a different
    status literal, and a different staleness budget that must be tunable — and
    disable-able — on its own. ``analysis_jobs`` had no reaper at all before
    migration 0015; a job whose process died mid-flight sat ``processing`` forever
    and the caller polled it forever.

    Env prefix ``ANALYZE_REAPER_``. ``ANALYZE_`` is live (``AnalyzeSettings`` in
    ``config/domains.py``) and ``REAPER_`` is live (``settings.reaper``); pydantic
    resolves the longest matching prefix per model, so the three do not collide.
    ``ANALYZE_`` is not in ``_RETIRED_ENV_PREFIXES``.

    ``stale_after_minutes`` is 60 against a job whose own in-band budget is capped
    at ``timeout_seconds <= 600`` by ``AnalysisConfig`` — six times the longest run
    the API will accept. Reaping live work is worse than reaping late.

    ``enabled`` defaults to True for the same reason both siblings do: this is a
    crash/rollout backstop, not a feature, and the failure it covers is invisible,
    so an opt-in default would arm the guard nowhere until someone edits a ConfigMap.
    """

    enabled: bool = Field(default=True, description="Enable the stale analysis-job reaper")
    stale_after_minutes: int = Field(
        default=60,
        description="Minutes an analysis job may sit in 'processing' before it is reaped",
    )
    interval_minutes: int = Field(default=10, description="Minutes between analysis reaper sweeps")

    model_config = SettingsConfigDict(
        env_prefix="ANALYZE_REAPER_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class ReviewReaperSettings(BaseSettings):
    """Stale-job reaper for review jobs stranded in ``processing``.

    A SIBLING of :class:`ReaperSettings`, not a reuse of it. That one is the
    critique reaper and its knobs are read by a loop whose predicate is
    ``status='running'`` on ``critique_jobs``; this one drives
    ``review.services.reaper``, whose predicate is ``status='processing'`` on
    ``review_jobs`` + ``analyze_review_jobs``. Sharing one settings group would
    make the two impossible to tune — or disable — independently, and the review
    sweep spans two tables whose jobs are LLM-bound and much slower than a
    critique scan, so its staleness threshold is deliberately its own number.

    Env prefix ``REVIEW_REAPER_``. ``REVIEW_`` is live (``settings.review``) and
    ``REAPER_`` is live (``settings.reaper``); neither is retired, and pydantic
    resolves the longest matching prefix per model, so the three do not collide.

    ``enabled`` defaults to True on purpose, for the same reason the critique
    reaper does: this is a crash/rollout backstop, not a feature. The failure it
    covers (a job left ``processing`` forever while the caller polls it forever)
    is invisible, so an opt-in default would mean the guard is armed nowhere
    until someone edits a ConfigMap.

    ``stale_after_minutes`` is 120, double the critique reaper's 60: a review job
    fans out across several review types, each a worker_management LLM call
    capped at ``REVIEW_TIMEOUT_SECONDS`` (600s), so a live job can legitimately
    sit in ``processing`` far longer than a critique scan. Reaping live work is
    worse than reaping late.
    """

    enabled: bool = Field(default=True, description="Enable the stale review-job reaper")
    stale_after_minutes: int = Field(
        default=120,
        description="Minutes a review job may sit in 'processing' before it is reaped",
    )
    interval_minutes: int = Field(default=10, description="Minutes between review reaper sweeps")

    model_config = SettingsConfigDict(
        env_prefix="REVIEW_REAPER_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class AuthzSettings(BaseSettings):
    """Route-level authorization for operator-only endpoints.

    Some routes (critique scoring weights + runtime config overrides) read/write
    process-GLOBAL runtime config shared across all tenants, so they must not be
    reachable by an ordinary tenant token. A caller is admitted only if it presents
    a Keycloak JWT whose realm roles intersect ``operator_roles``; license-token and
    in-cluster identities carry no roles (``request.state.roles == []``) and are denied.
    """

    operator_roles: list[str] = Field(
        default_factory=lambda: ["operator", "analysis-admin"],
        description=(
            "Keycloak realm roles permitted to read/write global runtime config "
            "(scoring weights, config overrides). Env AUTHZ_OPERATOR_ROLES (JSON list). "
            "Set to the real operator role(s) in GitOps; a caller needs a JWT carrying one."
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="AUTHZ_", env_file=".env", case_sensitive=False, extra="ignore"
    )


class FeatureFlags(BaseSettings):
    """Feature flags for gating new functionality."""

    batch_scan: bool = Field(default=True, description="Enable batch scan API")
    sse_progress: bool = Field(default=True, description="Enable SSE progress endpoint")
    pdf_reports: bool = Field(default=True, description="Enable PDF report generation")
    html_reports: bool = Field(default=True, description="Enable HTML report generation")
    analytics: bool = Field(default=True, description="Enable trend analytics endpoints")
    export_csv: bool = Field(default=True, description="Enable CSV export")
    export_sarif: bool = Field(default=True, description="Enable SARIF export")
    scheduled_scans: bool = Field(default=True, description="Enable scheduled recurring scans")
    batch_reviews: bool = Field(default=True, description="Enable batch review support")

    model_config = SettingsConfigDict(
        env_prefix="FEATURE_", env_file=".env", case_sensitive=False, extra="ignore"
    )
