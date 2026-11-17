"""
Unified configuration management for analysis_service using Pydantic Settings.

analysis_service is a single service (port 6303) that consolidated the former
standalone analyzer/critique/review/brand-extractor services — those no longer
exist as separate repos. Only two of the four domains own a settings section:
- analyze  — page classification, narrative extraction, site analysis
- review   — design, usability, features, content-quality qualitative review

Critique's knobs live on the shared ``scanners`` / ``scoring`` /
``external_tools`` / ``features`` sections, which the critique code actually
reads. Brand's live knob is ``playwright``, read by its fetch stage.

A section that nothing reads is not a harmless leftover — it is a control
panel wired to nothing, and an operator who sets its env vars gets silence.
Six such sections have been deleted (``critique``, ``screenshot``, ``dalle``,
``content_generation``, ``flux``, ``brand``) plus one duplicate alias
(``scanner``, singular). Their env prefixes now FAIL settings construction and
name the replacement — see `_RETIRED_ENV_PREFIXES` below. The redesign domain
they mostly belonged to was lifted out into html_render_service (migration 0004
drops its tables).

Shared infrastructure settings live at the top level; domain-specific
settings are nested under their respective section names.

This module was split into a ``config/`` package (``infra`` / ``scoring`` /
``media`` / ``domains``) for readability; every public symbol is re-exported
here so ``from src.config import <name>`` keeps working unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domains import AnalyzeSettings, ReviewSettings
from .infra import (
    AnalyzeReaperSettings,
    APISettings,
    AuthSettings,
    AuthzSettings,
    CacheSettings,
    DatabaseSettings,
    FeatureFlags,
    LoggingSettings,
    RateLimitSettings,
    ReaperSettings,
    RedisSettings,
    RetentionSettings,
    ReviewReaperSettings,
    StorageSettings,
    TracingSettings,
    WebhookSettings,
    WorkerManagementSettings,
)
from .media import (
    CGHSettings,
    ExternalToolSettings,
    PlaywrightSettings,
    ScannerSettings,
    TemplateSettings,
    VideoCompilerSettings,
)
from .scoring import ScoringSettings

__all__ = [
    "APISettings",
    "AnalyzeReaperSettings",
    "AnalyzeSettings",
    "AuthSettings",
    "AuthzSettings",
    "CGHSettings",
    "CacheSettings",
    "DatabaseSettings",
    "ExternalToolSettings",
    "FeatureFlags",
    "LoggingSettings",
    "PlaywrightSettings",
    "RateLimitSettings",
    "ReaperSettings",
    "RedisSettings",
    "RetentionSettings",
    "ReviewReaperSettings",
    "ReviewSettings",
    "ScannerSettings",
    "ScoringSettings",
    "Settings",
    "StorageSettings",
    "TemplateSettings",
    "TracingSettings",
    "VideoCompilerSettings",
    "WebhookSettings",
    "WorkerManagementSettings",
    "clear_settings_cache",
    "get_settings",
]


# ---------------------------------------------------------------------------
# Retired env prefixes
# ---------------------------------------------------------------------------
# `CritiqueSettings` (env_prefix "CRITIQUE_") used to sit on `Settings.critique`.
# Every field it declared IS live — but on a DIFFERENT section, under a different
# env prefix. Nothing anywhere read `settings.critique`: not src/, tests/,
# scripts/, this repo's k8s manifests, the fleet deployment configuration, nor the running
# deployment (checked 2026-08-02 — zero `CRITIQUE_`-prefixed keys in the w2v
# namespace). The two surfaces had already diverged: the live `ScannerSettings`
# and `ScoringSettings` grew `cro_timeout` / `cro_weight`, which the frozen copy
# never got.
#
# So the class was a decoy that read like a control panel. Setting
# `CRITIQUE_LIGHTHOUSE_ENABLED=true` bound a field no code consults, while
# `lighthouse_adapter.py` kept reading `settings.external_tools.lighthouse_enabled`.
# The dangerous direction is the disable: `CRITIQUE_AXE_ENABLED=false` looks like
# it turns axe off and does not — the scanner still runs (verified at runtime
# before deletion).
#
# ⚠️ Deleting the class alone does NOT close the trap: an unclaimed `CRITIQUE_*`
# var is simply IGNORED, which is the same silence with fewer moving parts. The
# operator's mental model is the bug, so the fix has to answer them. Any
# `CRITIQUE_*` var now fails settings construction and names its replacement.
# Blast radius today is zero (nothing sets one anywhere), and a var that does
# nothing is exactly the thing worth refusing to boot on.
#
# ---------------------------------------------------------------------------
# The same audit, run again (2026-08-02), found five MORE sections that nothing
# read. They are deleted too, and their prefixes join the same trap-closing
# mechanism below. What that pass also found is the reason this comment is long:
#
#   * `playwright` was on the "unread" allowlist and is NOT unread. Every field
#     is read by `src/domains/brand/stages/fetch.py`, which does
#     `pw = get_settings().playwright` and then reads `pw.headless` etc. The
#     detector only understood `settings.<section>.<field>` in one expression,
#     so binding the section to a local hid it. Deleting on the strength of that
#     allowlist would have broken live brand extraction. The allowlist entry, and
#     the CLAUDE.md line derived from it, were both simply wrong.
#     ⚠️ This is the SECOND vacuity in the same detector (the first was a text
#     search for ".critique." matching the import path `src.domains.critique`).
#     A guard's own blind spots become "documented facts" fast.
#
#   * `scanner` (singular) was a second attribute bound to the SAME
#     `ScannerSettings` class as the live `scanners`. Deleting the attribute is
#     safe precisely because both shared the `SCANNER_` prefix: every
#     `SCANNER_*` var still binds `settings.scanners`. So `SCANNER_` is NOT
#     retired, and adding it here would break the live per-scanner timeouts.
#
# The deleted sections and what an operator setting them actually wanted:
#   SCREENSHOT_ / DALLE_ / FLUX_ — the redesign domain (screenshots + AI image
#       generation) was lifted out into `html_render_service` by migration 0004.
#       These knobs configured a capability this service no longer has.
#   CONTENT_GENERATION_ — analysis_service has NO ai_content_generation client
#       at all (zero call sites), yet the live ConfigMap set
#       CONTENT_GENERATION_ENABLED / _BASE_URL / _DEFAULT_PROVIDER. _BASE_URL
#       was not even a field on the class, so it was doubly ignored.
#   BRAND_ — `BrandSettings.max_pages` / `.extraction_timeout`. Brand extraction
#       is a single-URL pipeline; there is no page count to cap.
# ---------------------------------------------------------------------------

_RETIRED_CRITIQUE_ENV: dict[str, str] = {
    # timeouts + retries moved to ScannerSettings (env_prefix "SCANNER_")
    **{
        f"CRITIQUE_{f.upper()}": f"SCANNER_{f.upper()}"
        for f in (
            "seo_timeout",
            "security_timeout",
            "a11y_timeout",
            "performance_timeout",
            "ux_timeout",
            "code_quality_timeout",
            "overall_timeout",
            "max_retries",
        )
    },
    # scoring weights moved to ScoringSettings (env_prefix "SCORING_")
    **{
        f"CRITIQUE_{f.upper()}": f"SCORING_{f.upper()}"
        for f in (
            "critical_weight",
            "high_weight",
            "medium_weight",
            "low_weight",
            "info_weight",
            "decay_k",
            "a11y_weight",
            "security_weight",
            "performance_weight",
            "seo_weight",
            "ux_weight",
            "score_floor",
            "perf_finding_weight",
            "perf_lighthouse_weight",
        )
    },
    # tool flags moved to ExternalToolSettings (env_prefix "EXTERNAL_TOOL_")
    "CRITIQUE_EXTERNAL_TOOLS_ENABLED": "EXTERNAL_TOOL_ENABLED",
    **{
        f"CRITIQUE_{f.upper()}": f"EXTERNAL_TOOL_{f.upper()}"
        for f in ("axe_enabled", "axe_timeout", "lighthouse_enabled", "lighthouse_timeout")
    },
    # report/API toggles moved to FeatureFlags (env_prefix "FEATURE_")
    **{
        f"CRITIQUE_{f.upper()}": f"FEATURE_{f.upper()}"
        for f in ("batch_scan", "sse_progress", "pdf_reports", "html_reports")
    },
}

#: Every retired prefix, mapped to what an operator who set one should do
#: instead. A var under one of these prefixes fails settings construction.
#:
#: The whole prefix is refused, not just the field names that used to exist:
#: `DALLE_MODEL` and `DALLE_MDOEL` are equally inert, and only rejecting the
#: former would leave the typo silently ignored — which is the defect this
#: mechanism exists to remove, reintroduced one level down.
#:
#: ⚠️ `SCANNER_` is deliberately NOT here. It is live (`settings.scanners`).
_RETIRED_ENV_PREFIXES: dict[str, str] = {
    "CRITIQUE_": (
        "the critique domain's knobs are live under SCANNER_ / SCORING_ / EXTERNAL_TOOL_ / FEATURE_"
    ),
    "SCREENSHOT_": (
        "screenshot rendering moved to html_render_service (migration 0004 "
        "dropped the redesign tables); analysis_service renders only inside the "
        "brand pipeline, which is configured via PLAYWRIGHT_*"
    ),
    "DALLE_": (
        "AI image generation is not part of analysis_service; it lives in "
        "ai_content_generation_service"
    ),
    "FLUX_": (
        "local FLUX image generation is not part of analysis_service; it lives "
        "in ai_content_generation_service / image_gen_backend"
    ),
    "CONTENT_GENERATION_": (
        "analysis_service has no ai_content_generation client — there is no "
        "call site to enable, time out, or point at a provider. Remove the key"
    ),
    "JOB_QUEUE_": (
        "the Redis job queue and its worker are DELETED. They had no producer: "
        "nothing ever called JobQueue.enqueue, so run_worker polled a list that "
        "was never written and the DLQ was permanently empty — an alert on it "
        "would have looked armed while being blind to every failure. Critique "
        "jobs run inline via _process_job_background; there is no retry knob to "
        "set, and MAX_RETRIES never applied to the live path in the first place"
    ),
    "BRAND_": (
        "the brand domain has no settings section: extraction is a single-URL "
        "pipeline with no page cap, and its browser knobs are PLAYWRIGHT_*"
    ),
}


def _dotenv_keys(path: str = ".env") -> dict[str, str]:
    """Keys declared in the dotenv file that pydantic-settings also reads.

    Deliberately a minimal `KEY=` scan rather than a dotenv parser: the only
    question asked of it is which NAMES are set, and a name is unambiguous even
    where a value would need quoting/escaping rules to read correctly. Missing
    or unreadable file -> no keys, never an exception; refusing to boot because
    a dotenv file had odd permissions would be a worse failure than the one this
    guards against.
    """
    try:
        text = Path(path).read_text(errors="ignore")
    except OSError:
        return {}
    keys: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, _ = line.partition("=")
        if sep:
            keys[name.strip().removeprefix("export ").strip()] = ""
    return keys


def _retired_env_offenders(environ: Mapping[str, str]) -> list[str]:
    """Return the sorted names in ``environ`` that fall under a retired prefix.

    Split out of the validator so the scan itself is testable without mutating
    the real process environment.
    """
    return sorted(
        key
        for key in environ
        if any(key.upper().startswith(prefix) for prefix in _RETIRED_ENV_PREFIXES)
    )


def _retired_env_message(offenders: list[str]) -> str:
    """Build the operator-facing error naming each replacement."""
    lines = []
    for key in offenders:
        upper = key.upper()
        replacement = _RETIRED_CRITIQUE_ENV.get(upper)
        if replacement:
            lines.append(f"  {key} -> use {replacement} instead")
            continue
        prefix = next(p for p in _RETIRED_ENV_PREFIXES if upper.startswith(p))
        lines.append(f"  {key} -> {_RETIRED_ENV_PREFIXES[prefix]}")
    return (
        "These settings prefixes are retired and configure NOTHING:\n"
        + "\n".join(lines)
        + "\nThey were silently ignored, so a capability you believed you had "
        "enabled or disabled behaved exactly as before."
    )


# ---------------------------------------------------------------------------
# Main settings class
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Main application settings combining all configuration sections."""

    app_name: str = Field(default="Analysis Service", description="Application name")
    app_version: str = Field(default="0.1.0", description="Application version")
    environment: str = Field(
        default="development", description="Environment (development, staging, production)"
    )

    # Shared infrastructure
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    authz: AuthzSettings = Field(default_factory=AuthzSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    api: APISettings = Field(default_factory=APISettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    wm: WorkerManagementSettings = Field(default_factory=WorkerManagementSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    reaper: ReaperSettings = Field(default_factory=ReaperSettings)
    # Sibling of `reaper` (critique). Separate group on purpose -- different
    # table, different status literal, different staleness budget. See
    # ReviewReaperSettings.
    review_reaper: ReviewReaperSettings = Field(default_factory=ReviewReaperSettings)
    # Third sibling of `reaper`/`review_reaper` — `analysis_jobs` stranded in
    # `processing`. Separate group for the same reason the other two are separate:
    # different table, different status literal, own staleness budget. Migration
    # 0015. See AnalyzeReaperSettings.
    analyze_reaper: AnalyzeReaperSettings = Field(default_factory=AnalyzeReaperSettings)

    # Legacy attributes (kept for backward compatibility with domain tests)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    feature_flags: FeatureFlags = Field(default_factory=FeatureFlags)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)

    # Domain-level settings exposed at top level (domain code accesses settings.X directly)
    # `playwright` is LIVE: src/domains/brand/stages/fetch.py reads all five fields.
    playwright: PlaywrightSettings = Field(default_factory=PlaywrightSettings)
    # `scanners` is the ONLY name for ScannerSettings. A `scanner` singular alias
    # of the same class also existed and was read by nothing; both shared the
    # SCANNER_ prefix, so dropping it changed no binding.
    scanners: ScannerSettings = Field(default_factory=ScannerSettings)
    video_compiler: VideoCompilerSettings = Field(default_factory=VideoCompilerSettings)
    external_tools: ExternalToolSettings = Field(default_factory=ExternalToolSettings)

    # Domain-specific
    analyze: AnalyzeSettings = Field(default_factory=AnalyzeSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @model_validator(mode="after")
    def _reject_retired_env(self) -> Settings:
        """Fail loudly on any var under a retired prefix — they configure nothing.

        See the `_RETIRED_ENV_PREFIXES` note above. Silence is the defect: an
        operator who sets one of these believes they changed the service's
        behaviour and has not. Deleting the dead class alone would not fix that
        — an unclaimed var is simply ignored, which is the same silence with
        fewer moving parts.

        Both binding surfaces are scanned. `model_config` declares
        ``env_file=".env"``, so a retired key in a dotenv file was bound (and
        ignored) exactly as an exported one was; checking only `os.environ`
        would leave half the trap open.
        """
        offenders = _retired_env_offenders({**_dotenv_keys(), **os.environ})
        if not offenders:
            return self
        raise ValueError(_retired_env_message(offenders))

    def get_database_url(self) -> str:
        """Return the configured database connection URL."""
        return self.database.url

    def get_redis_url(self) -> str:
        """Return the configured Redis connection URL."""
        return self.redis.url

    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment.lower() == "production"

    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


def clear_settings_cache() -> None:
    """Clear the cached settings instance (useful for testing)."""
    get_settings.cache_clear()
