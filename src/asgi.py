"""
ASGI application for analysis_service.

Unified FastAPI app merging five microservices:
- analyze (page classification, narrative extraction, site analysis)
- critique (SEO, security, a11y, performance, UX)
- review (design, usability, features, content quality)
- brand (colors, typography, logos, identity)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from redis.exceptions import RedisError
from w2v_common.asgi import create_app
from w2v_common.observability import database_endpoint, redis_endpoint
from w2v_common.observability.request_context import RequestIdLogFilter

from src.config import get_settings
from src.logging_config import configure_logging

load_dotenv()

settings = get_settings()

configure_logging(json_logs=settings.logging.json_logs, level=settings.logging.level)
logger = logging.getLogger(__name__)


# ============================================================================
# Health Check Logging Filter
# ============================================================================


class HealthCheckLoggingFilter(logging.Filter):
    """Suppress access log noise from health check endpoints."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(
            path in message
            for path in ["/health", "/health/ready", "/health/live", "/healthz", "/readyz"]
        )


uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.addFilter(HealthCheckLoggingFilter())


# ============================================================================
# Lifespan
# ============================================================================


async def _init_tracing() -> None:
    """Initialize OpenTelemetry tracing (best-effort)."""
    if not settings.tracing.enabled:
        return
    try:
        from src.shared.observability.tracing import setup_tracing

        setup_tracing(
            service_name=settings.tracing.service_name,
            environment=settings.environment,
        )
        logger.info("OpenTelemetry tracing initialized")
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to initialize tracing: %s", exc, exc_info=True)


async def _init_database(app: FastAPI) -> None:
    """Initialize database engine and async session factories.

    Raises if the database can't be reached (a hard dependency).
    """
    try:
        from src.shared.database.connection import get_engine

        db_engine = await get_engine()
        app.state.db_engine = db_engine
        logger.info("Database connected: %s", database_endpoint(settings.database.url))
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.error("Failed to connect to database: %s", exc, exc_info=True)
        raise

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    app.state.db_session_factory = async_sessionmaker(
        db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    from src.shared.database.session import get_session_maker

    app.state.session_factory = await get_session_maker()

    # Phase D RLS: re-bind app.current_tenant on every transaction from the
    # identity ContextVar, so the critique worker's multi-commit process_job
    # keeps the tenant GUC through to the finalize audit INSERT under enforce.
    from src.shared.database.rls_events import install_rls_tenant_rebind

    install_rls_tenant_rebind()


async def _init_redis(app: FastAPI) -> None:
    """Initialize Redis client; degrades to None on failure."""
    try:
        from src.shared.cache.redis_client import get_redis_client, init_redis

        redis_client = await init_redis()
        await redis_client.ping()
        app.state.redis_client = get_redis_client()
        logger.info("Redis connected: %s", redis_endpoint(settings.redis.url))
    except (ImportError, OSError, RuntimeError, ValueError, RedisError) as exc:
        logger.error("Failed to connect to Redis: %s", exc, exc_info=True)
        logger.warning("Continuing without Redis -- caching will be disabled")
        app.state.redis_client = None


def _init_analyze_domain(app: FastAPI, background_tasks: list[asyncio.Task[Any]]) -> None:
    """Initialize Analyze domain: AnalyzerService + TTLCaches + reaper/retention loops."""
    try:
        from cachetools import TTLCache

        from src.domains.analyze.services.analyzer_service import AnalyzerService

        app.state.analyzer_service = AnalyzerService()
        app.state.analysis_jobs = TTLCache(
            maxsize=settings.cache.job_maxsize,
            ttl=settings.cache.job_ttl,
        )
        app.state.cancellation_tokens = {}
        app.state.review_jobs = TTLCache(
            maxsize=settings.cache.review_maxsize,
            ttl=settings.cache.review_ttl,
        )
        logger.info("Analyze domain initialized (AnalyzerService + TTLCache)")

        # Stale-job reaper — the analyze-domain twin of the critique and review
        # reapers. `analysis_jobs` is created at `processing` and finalized only
        # in-band from `asyncio.create_task`, so a pod that dies mid-flight leaves
        # a row nothing in this service would ever move: not the critique reaper
        # (`running` on `critique_jobs`), not the review reaper (`processing` on
        # the two review tables), and not any prune (terminal statuses only).
        # Own setting, `settings.analyze_reaper.enabled`, defaulting to True —
        # see AnalyzeReaperSettings for why an opt-in default arms nothing.
        if settings.analyze_reaper.enabled:
            try:
                from src.domains.analyze.services.reaper import run_analyze_reaper_loop

                background_tasks.append(asyncio.create_task(run_analyze_reaper_loop()))
                logger.info("Analysis stale-job reaper started")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("Analysis stale-job reaper not available: %s", exc)

        # Retention — distinct from the reaper above the same way `settings.retention`
        # is distinct from `settings.reaper`: this DELETES old TERMINAL rows,
        # the reaper is a correctness backstop for LIVE ones. `analysis_jobs` had
        # no retention at all before migration 0015. Gated on the SAME
        # `settings.retention` switch that governs the critique prune, on purpose.
        if settings.retention.enabled:
            try:
                from src.domains.analyze.services.retention import run_analyze_retention_loop

                background_tasks.append(asyncio.create_task(run_analyze_retention_loop()))
                logger.info("Analysis retention loop started")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("Analysis retention loop not available: %s", exc)

    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to initialize Analyze domain: %s", exc, exc_info=True)


def _init_critique_domain(app: FastAPI, background_tasks: list[asyncio.Task[Any]]) -> None:
    """Initialize Critique domain: service, analytics, scheduler, retention, reaper."""
    try:
        from src.domains.critique.services.critique_service import CritiqueService

        app.state.critique_service = CritiqueService()
        logger.info("CritiqueService initialized")

        app.state.analytics_service = None
        try:
            from src.domains.critique.services.analytics_service import AnalyticsService

            app.state.analytics_service = AnalyticsService()
            logger.info("Critique AnalyticsService initialized")
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Critique AnalyticsService not available: %s", exc)

        # NOTE: there is no Redis job-queue worker here any more. It was deleted
        # (see `_RETIRED_ENV_PREFIXES["JOB_QUEUE_"]` in `src/config/__init__.py`)
        # because it had NO producer: nothing in this repo, the w2v fleet, or
        # gitops ever called `JobQueue.enqueue`, and the live Redis carried zero
        # `critique:queue:*` keys. Critique jobs are processed inline by
        # `_process_job_background` (`domains/critique/routes/_helpers.py`),
        # which is the only path that can carry `site_analysis`, `llm_config`,
        # `html_input` and `artifact_ref` — the queue payload could not.

        try:
            from src.domains.critique.services.scheduler import run_scheduler

            task = asyncio.create_task(run_scheduler(app.state.critique_service))
            background_tasks.append(task)
            logger.info("Critique scan scheduler started")
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Critique scheduler not available: %s", exc)

        if settings.retention.enabled:
            try:
                from src.domains.critique.services.retention import run_retention_loop

                task = asyncio.create_task(run_retention_loop())
                background_tasks.append(task)
                logger.info("Critique retention cleanup started")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("Critique retention cleanup not available: %s", exc)

        # Stale-job reaper. Unlike retention (opt-in cleanup of OLD terminal
        # rows) this is a correctness backstop for LIVE rows: a job whose process
        # died mid-flight stays `running` forever and the orchestrator polls it
        # forever. It is gated on its own `settings.reaper.enabled`, which
        # defaults to True — see ReaperSettings for why an opt-in default would
        # mean the guard is armed nowhere.
        if settings.reaper.enabled:
            try:
                from src.domains.critique.services.reaper import run_reaper_loop

                task = asyncio.create_task(run_reaper_loop())
                background_tasks.append(task)
                logger.info("Critique stale-job reaper started")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("Critique stale-job reaper not available: %s", exc)

    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to initialize Critique domain: %s", exc, exc_info=True)


def _init_review_domain(app: FastAPI, background_tasks: list[asyncio.Task[Any]]) -> None:
    """Initialize Review domain: service + cleanup background task."""
    try:
        from src.domains.review.services.review_service import ReviewService

        app.state.review_service = ReviewService()
        logger.info("ReviewService initialized")

        try:
            from src.domains.review.services.cleanup import cleanup_old_jobs

            cleanup_task = asyncio.create_task(
                cleanup_old_jobs(
                    retention_hours=settings.review.job_retention_hours,
                    interval_minutes=settings.review.cleanup_interval_minutes,
                )
            )
            background_tasks.append(cleanup_task)
            logger.info(
                "Review cleanup task started (retention=%dh, interval=%dm)",
                settings.review.job_retention_hours,
                settings.review.cleanup_interval_minutes,
            )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Review cleanup task not available: %s", exc)

        # Stale-job reaper — the review-domain twin of the critique reaper wired in
        # `_init_critique_domain`. Distinct from the cleanup loop above the same way
        # `settings.reaper` is distinct from `settings.retention`: cleanup deletes OLD
        # TERMINAL rows, this is a correctness backstop for LIVE rows. A review job
        # whose process died mid-flight stays `processing` forever, and nothing else
        # in the service would ever move it — not the critique reaper (whose predicate
        # is `running` on `critique_jobs`) and not the prune (terminal statuses only).
        # Own setting, `settings.review_reaper.enabled`, defaulting to True — see
        # ReviewReaperSettings for why an opt-in default would arm the guard nowhere.
        if settings.review_reaper.enabled:
            try:
                from src.domains.review.services.reaper import run_review_reaper_loop

                reaper_task = asyncio.create_task(run_review_reaper_loop())
                background_tasks.append(reaper_task)
                logger.info("Review stale-job reaper started")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("Review stale-job reaper not available: %s", exc)

    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to initialize Review domain: %s", exc, exc_info=True)


def _build_brand_pipeline_runner() -> Any:
    """Build the brand PipelineRunner with all 12 default stages."""
    from src.domains.brand.pipeline.runner import PipelineRunner
    from src.domains.brand.stages.colors import ColorStage
    from src.domains.brand.stages.fetch import FetchStage
    from src.domains.brand.stages.fonts import FontStage
    from src.domains.brand.stages.llm_analysis import LLMBrandAnalysisStage
    from src.domains.brand.stages.logo import LogoStage
    from src.domains.brand.stages.logo_download import LogoDownloadStage
    from src.domains.brand.stages.metadata import MetadataStage
    from src.domains.brand.stages.name import NameStage
    from src.domains.brand.stages.palette import PaletteStage
    from src.domains.brand.stages.quality import QualityStage
    from src.domains.brand.stages.social import SocialStage
    from src.domains.brand.stages.visual_colors import VisualColorStage

    return PipelineRunner(
        [
            FetchStage(),
            NameStage(),
            LogoStage(),
            MetadataStage(),
            ColorStage(),
            VisualColorStage(),
            FontStage(),
            SocialStage(),
            LLMBrandAnalysisStage(),
            PaletteStage(),
            LogoDownloadStage(),
            QualityStage(),
        ]
    )


async def _init_brand_domain(app: FastAPI) -> None:
    """Initialize Brand domain: HTTP client, storage, logo, cache, LLM, pipeline, events."""
    try:
        import httpx

        http_client = httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers={"User-Agent": "AnalysisService/1.0"},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        app.state.http_client = http_client

        from src.domains.brand.services.storage_service import StorageService

        brand_storage = StorageService()
        await brand_storage.initialize()
        app.state.brand_storage_service = brand_storage

        from src.domains.brand.services.logo_service import LogoService

        app.state.logo_service = LogoService(storage=brand_storage, http_client=http_client)

        app.state.brand_cache = None
        redis = getattr(app.state, "redis_client", None)
        if redis and redis.is_available:
            try:
                from src.domains.brand.services.brand_cache_service import BrandCacheService

                app.state.brand_cache = BrandCacheService(redis_client=redis)
                logger.info("BrandCacheService initialized")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("BrandCacheService not available: %s", exc)

        app.state.icp_cache = None
        if redis and redis.is_available:
            try:
                from src.domains.icp.services.icp_cache_service import IcpCacheService

                app.state.icp_cache = IcpCacheService(redis_client=redis)
                logger.info("IcpCacheService initialized")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("IcpCacheService not available: %s", exc)

        # Single shared WMClient. Underlying httpx.AsyncClient is
        # connection-pooled and the identity event hook reads
        # request-scoped ContextVars per outbound request, so the same
        # instance serves concurrent end-user requests with the right
        # per-user identity headers. WMClient is the SOLE LLM gateway
        # — see memory `wm-as-sole-llm-gateway.md`.
        wm_client = app.state.wm_client
        app.state.llm_client = wm_client
        logger.info(
            "LLM client registered for brand pipeline: llm_backend=%s wm_base_url=%s",
            getattr(wm_client, "llm_backend", "unknown"),
            wm_client.base_url,
        )

        pipeline_runner = _build_brand_pipeline_runner()
        app.state.pipeline_runner = pipeline_runner
        logger.info("Brand PipelineRunner initialized with %d stages", len(pipeline_runner._stages))

        app.state.event_publisher = None
        redis = getattr(app.state, "redis_client", None)
        if redis and redis.is_available:
            try:
                from src.domains.brand.services.event_publisher import BrandEventPublisher

                app.state.event_publisher = BrandEventPublisher(redis_client=redis)
                logger.info("BrandEventPublisher initialized")
            except (ImportError, OSError, RuntimeError, ValueError) as exc:
                logger.warning("BrandEventPublisher not available: %s", exc)

        logger.info("Brand domain initialized")

    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.warning("Failed to initialize Brand domain: %s", exc, exc_info=True)
        if not hasattr(app.state, "http_client"):
            app.state.http_client = None


async def _safe_aclose(name: str, obj: Any) -> None:
    """Call ``obj.aclose()`` if obj is not None; log+swallow expected errors.

    Keeps ``_shutdown`` under the C901 complexity cap as more closeables get added.
    """
    if obj is None:
        return
    try:
        await obj.aclose()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.error("Error closing %s: %s", name, exc, exc_info=True)


async def _shutdown(app: FastAPI, background_tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel background tasks and close shared resources."""
    logger.info("Shutting down Analysis Service")

    for task in background_tasks:
        if not task.done():
            task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
        logger.info("Background tasks cancelled (%d)", len(background_tasks))

    await _safe_aclose("HTTP client", getattr(app.state, "http_client", None))
    await _safe_aclose("WMClient", getattr(app.state, "wm_client", None))
    # Subordinate services with their own AsyncClient(s) propagate aclose via
    # their service-level aclose(); asgi only knows about top-level handles.

    try:
        from src.shared.cache.redis_client import close_redis

        await close_redis()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.error("Error closing Redis: %s", exc, exc_info=True)

    try:
        from src.shared.database.connection import close_engine

        await close_engine()
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.error("Error closing database: %s", exc, exc_info=True)

    logger.info("Analysis Service shutdown complete")


async def _startup(app: FastAPI) -> None:
    """Init DB, Redis, tracing, and all domain services (create_app on_startup)."""
    logger.info("=" * 70)
    logger.info("Starting Analysis Service")
    logger.info("=" * 70)

    background_tasks: list[asyncio.Task[Any]] = []

    await _init_tracing()
    await _init_database(app)
    await _init_redis(app)

    # Construct the shared WMClient before any domain that might use it.
    # Domains read it from app.state.wm_client.
    from src.llm_client import get_wm_client

    app.state.wm_client = get_wm_client()
    # ``base_url`` is worker_management's even when LLM_BACKEND=vllm sends every
    # call straight to vLLM -- log the backend so the line does not mislead.
    logger.info(
        "LLM client (WMClient) initialized: llm_backend=%s wm_base_url=%s",
        getattr(app.state.wm_client, "llm_backend", "unknown"),
        app.state.wm_client.base_url,
    )

    # WEBSCRAPER_AUTH_MODE (w2v_common TenantLicenceLane): shared (default, the
    # shared DOWNSTREAM_LICENSE_TOKEN) or tenant_licence (the bound tenant's own
    # minted w2v licence). Built HERE so a typo, or tenant_licence without the
    # BUNDLE_MINTER_* settings, fails the pod at boot, not the first ICP request.
    from w2v_common.auth.tenant_licence_lane import TenantLicenceLane

    app.state.webscraper_lane = TenantLicenceLane("webscraper")
    logger.info("webscraper hop auth mode: %s", app.state.webscraper_lane.mode.value)

    app.state.started_at = datetime.now(UTC)

    _init_analyze_domain(app, background_tasks)
    _init_critique_domain(app, background_tasks)
    _init_review_domain(app, background_tasks)
    await _init_brand_domain(app)

    app.state.background_tasks = background_tasks

    logger.info("=" * 70)
    logger.info("Analysis Service started successfully")
    logger.info("Environment: %s", settings.environment)
    logger.info("API: %s:%d", settings.api.host, settings.api.port)
    logger.info("=" * 70)

    app.state.shutting_down = False


async def _shutdown_hook(app: FastAPI) -> None:
    """Graceful drain then teardown (create_app on_shutdown).

    Tell ShutdownMiddleware to refuse new mutating requests before tearing down
    DB/Redis. k8s sends SIGTERM terminationGracePeriodSeconds before SIGKILL —
    the drain window must complete inside that envelope.
    """
    app.state.shutting_down = True
    await _shutdown(app, getattr(app.state, "background_tasks", []))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Back-compat context manager composing the create_app on_startup/on_shutdown
    hooks. The app itself is built via ``create_app``; this wrapper exists so tests
    can drive the full startup→shutdown cycle as one ``async with``.
    """
    await _startup(app)
    try:
        yield
    finally:
        await _shutdown_hook(app)


# ============================================================================
# Application
# ============================================================================

# Routers + service-specific middleware are imported here so create_app receives
# them via routers= / extra_middleware=.
#
# ENTITLEMENT GATE (w2v_common.auth.entitlement, >=0.75.0): every tenant-scoped
# `/api/*` router below is mounted with `dependencies=list(LICENSE_GATE)` at its
# own APIRouter(...) declaration (analyze, context, context-profiles, critique,
# review, brand, brands, icp, artifact-score, cro, seo_audit, marketing). Until
# now this service authenticated the licence token for tenant IDENTITY
# (AuthMiddleware answers "who are you") and never asked "may you" — a tenant
# whose licence is expired or revoked still carries a syntactically valid token
# and was served. LICENSE_GATE closes that: none/expired/revoked -> 402
# license_<state>, an unestablishable verdict -> 503 license_unavailable +
# Retry-After, active/expiring_soon -> pass.
#
# DELIBERATELY NOT GATED, each for a stated reason:
#   * `health_router` (/health, /health/live, /health/ready, /healthz, /readyz
#     and aliases) — a 402/503 on a probe CrashLoops the pod, and an unlicensed
#     tenant cannot be billed by killing our own deployment. The gate also
#     refuses these paths by name (`is_license_exempt_path`), so this is belt
#     and braces, not a single point of failure.
#   * /metrics and /docs|/openapi.json, added by w2v_common's create_app — a
#     Prometheus scrape and the schema carry no tenant and do no tenant work.
# There is no inbound webhook/callback receiver anywhere in this service —
# every `webhook`/`callback` hit in review and critique is an OUTBOUND
# delivery this service makes to a tenant-configured URL, already covered by
# gating the router that triggers it.
#
# DORMANT ON MERGE: LICENSE_GATE is a no-op unless W2V_LICENSE_GATE_ENABLED is
# true, read per-call from os.environ. It defaults to False, so this change
# alters no behaviour until gitops arms the flag. Preserve that default.
from src.domains.analyze.context_profiles import profiles_router
from src.domains.analyze.routes import context_router
from src.domains.analyze.routes import router as analyze_router
from src.domains.artifact_score.routes import router as artifact_score_router
from src.domains.brand.brands_routes import brands_router as brands_crud_router
from src.domains.brand.routes import router as brand_router
from src.domains.critique.routes import router as critique_router
from src.domains.cro.routes import router as cro_router
from src.domains.icp.routes import router as icp_router
from src.domains.marketing.routes import router as marketing_router
from src.domains.review.routes import router as review_router
from src.domains.seo_audit.routes import router as seo_audit_router
from src.shared.health import router as health_router
from src.shared.middleware.audit import AuditMiddleware
from src.shared.middleware.metrics import MetricsMiddleware
from src.shared.middleware.shutdown import ShutdownMiddleware
from src.shared.upstream_errors import register_upstream_error_handler

# Bespoke middleware with no (enabled) factory equivalent, appended OUTSIDE the
# canonical stack. Shutdown (outermost) refuses new mutating requests during the
# graceful-drain window; Metrics + Audit are the service-local implementations.
# The factory owns SecurityHeaders / RequestId / Auth / error-envelope / OpenAPI
# (analysis's local SecurityHeaders was a byte-identical clone of the shared one).
_extra_middleware: list[tuple[type, dict[str, Any]]] = [
    (ShutdownMiddleware, {}),
    (MetricsMiddleware, {}),
    (AuditMiddleware, {}),
]
if settings.rate_limit.enabled:
    from src.shared.middleware.rate_limiter import RateLimitMiddleware

    _extra_middleware.insert(1, (RateLimitMiddleware, {}))
    logger.info(
        "Rate limiting enabled: %d/hr, %d/day",
        settings.rate_limit.hourly_limit,
        settings.rate_limit.daily_limit,
    )

app = create_app(
    settings,
    routers=[
        health_router,
        analyze_router,
        context_router,
        profiles_router,
        critique_router,
        review_router,
        brand_router,
        brands_crud_router,
        icp_router,
        artifact_score_router,
        cro_router,
        seo_audit_router,
        marketing_router,
    ],
    on_startup=[_startup],
    on_shutdown=[_shutdown_hook],
    extra_middleware=_extra_middleware,
    title=settings.app_name,
    description=(
        "Unified website analysis service: content analysis, critique, "
        "qualitative review, and brand extraction"
    ),
    version=settings.app_version,
    redirect_slashes=False,
    swagger_ui_parameters={"persistAuthorization": True},
    # Fallback unique-id generator. Every route still sets `operation_id=`
    # explicitly per OPERATION_ID_PLAN.md; this exists so a future route
    # that forgets the kwarg degrades to `<tag>_<func>` instead of FastAPI's
    # default `<func>_<path>_<method>` (garbage in generated clients).
    generate_unique_id_function=lambda route: (
        f"{route.tags[0] if route.tags else 'default'}_{route.name}"
    ),
    # Tag names match the lower-case strings the per-router APIRouters
    # already declare (`tags=["brand"]` etc.) — keep these in sync.
    openapi_tags=[
        {"name": "health", "description": "Liveness, readiness, dependency probes."},
        {
            "name": "analyze",
            "description": "Page classification, narrative extraction, site analysis.",
        },
        {"name": "context", "description": "Site context inference + validation."},
        {"name": "context-profiles", "description": "Per-tenant context profile CRUD."},
        {
            "name": "critique",
            "description": "SEO, security, accessibility, performance, UX scoring.",
        },
        {
            "name": "review",
            "description": "Design, usability, features, content qualitative review.",
        },
        {"name": "brand", "description": "Brand identity extraction — colors, typography, logos."},
        {"name": "brands", "description": "Persisted brand records CRUD."},
        {"name": "icp", "description": "GTM/ICP extraction from a URL (recon-shaped flat record)."},
        {
            "name": "artifact-score",
            "description": "Synchronous, deterministic quality score + score-delta for a rendered artifact.",
        },
    ],
)

# Bind request_id onto every log record (the factory registers the shared
# RequestIdMiddleware, which sets the ContextVar; this filter surfaces it in the
# JSON log formatter). Auth is delegated to the factory via settings.auth.enabled.
logging.getLogger().addFilter(RequestIdLogFilter())

# Must run AFTER create_app: the factory installs the shared error-envelope
# handlers, and this adds ONE narrower handler for UpstreamStatusError only.
# Without it, a passed-through upstream 402 still gets the right status and the
# right human message, but the envelope's ``code`` degrades to a generic
# ``HTTP_402`` and the frontend loses the ``byo_llm_not_configured`` branch key.
register_upstream_error_handler(app)


# ============================================================================
# Root Endpoint
# ============================================================================


@app.get(
    "/",
    operation_id="service_root",
    tags=["health"],
    summary="Service banner + endpoint index",
)
async def root() -> dict[str, Any]:
    """Return service metadata and available endpoints."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "description": (
            "Unified website analysis service: content analysis, critique, "
            "qualitative review, and brand extraction"
        ),
        "endpoints": {
            "health": "/health",
            "docs": "/docs",
            "analyze": "/api/analyze",
            "classify": "/api/analyze/pages",
            "narrative": "/api/analyze/narrative",
            "context": "/api/context",
            "context_profiles": "/api/context/profiles",
            "critique": "/api/critique",
            "review": "/api/review",
            "brand_extract": "/api/brand/extract",
            "brand_from_url": "/api/brand/from-url",
            "brands": "/api/brands",
            "cro_score": "/api/cro/score",
            "seo_audit": "/api/seo/audit",
            "marketing_scorecard": "/api/marketing/scorecard",
        },
        "status": "running",
        "environment": settings.environment,
    }


def main() -> None:
    """Main entry point for running the ASGI application."""
    import uvicorn

    uvicorn.run(
        "src.asgi:app",
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.logging.level.lower(),
        reload=settings.api.reload,
    )


if __name__ == "__main__":
    main()
