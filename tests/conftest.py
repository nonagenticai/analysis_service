"""Shared test fixtures for analysis_service.

Merges common fixtures from the five source services:
- analyzer_service
- critique_service
- brand_extractor_service
- review_service
- redesign_service
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Set test environment variables BEFORE any app imports
os.environ.setdefault("ENVIRONMENT", "test")
# All LLM calls now route through worker_management; tests mock WMClient.message
# directly. Provide a harmless WM URL so any accidental construction of a real
# WMClient hits an unroutable host instead of leaking outbound traffic.
os.environ.setdefault("WORKER_MANAGEMENT_URL", "http://wm.test.invalid:8887")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ.setdefault("STORAGE_LOCAL_PATH", "/tmp/analysis_service_test")
# Required field on StorageSettings (no localhost fallback in w2v_common 0.15.0+).
os.environ.setdefault("STORAGE_MINIO_ENDPOINT", "localhost:9000")
# Required field on RedisSettings (no localhost fallback in w2v_common 0.15.0+).
os.environ.setdefault("REDIS_URL", "redis://localhost:6393/1")
os.environ.setdefault("CACHE_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Port 5471 (the `analysis_service_test_postgres` compose service), not 5432: a 5432
# default resolves to whatever postgres the developer happens to be running, which
# answers the TCP connect and THEN fails auth — disguising a config error as a
# connection error.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://ci_test:ci_test_password_secure@127.0.0.1:5471/analysis_service_ci",  # pragma: allowlist secret
)

# Capture the test DB URL BEFORE the autouse _clear_settings fixture
# monkeypatches DATABASE_URL to its in-test default. CI sets DATABASE_URL
# to its analysis_service_ci postgres; locally falls back to the default above
# (developer must run a local postgres on :5434 to execute db_engine tests).
TEST_DATABASE_URL = os.environ["DATABASE_URL"]

# Ensure the configured local storage path exists so health checks see a writable dir.
Path(os.environ["STORAGE_LOCAL_PATH"]).mkdir(parents=True, exist_ok=True)

import pytest  # noqa: E402 -- must import after env setup above
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402
from w2v_common.cache.redis_client import RedisClient  # noqa: E402
from w2v_common.testing import inject_fakeredis  # noqa: E402

from src.config import clear_settings_cache  # noqa: E402
from tests.dns_stub import DnsStub  # noqa: E402
from tests.dns_stub import install as install_dns_stub  # noqa: E402

# ---------------------------------------------------------------------------
# Settings cache management
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings(monkeypatch):
    """Clear settings cache before and after each test.

    Removes CI env vars that interfere with config-default tests, then
    sets API_PORT so any Settings() construction picks up a test default.
    """
    for key in list(os.environ):
        if any(
            key.startswith(p)
            for p in (
                "DATABASE_",
                "AUTH_",
                "REDIS_",
                "API_",
                "STORAGE_",
                "LOG_",
                "CACHE_",
                "TRACING_",
                "WM_",
                "WORKER_MANAGEMENT_",
                "FFMPEG_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    # Restore test-only env vars that were cleared above
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("WORKER_MANAGEMENT_URL", "http://wm.test.invalid:8887")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_PATH", "/tmp/analysis_service_test")
    monkeypatch.setenv("STORAGE_MINIO_ENDPOINT", "localhost:9000")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6393/1")
    Path("/tmp/analysis_service_test").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("API_PORT", "6310")
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.fixture(autouse=True, scope="session")
def _disable_otel_sdk():
    """Disable OpenTelemetry SDK during tests."""
    os.environ["OTEL_SDK_DISABLED"] = "true"
    clear_settings_cache()
    yield
    os.environ.pop("OTEL_SDK_DISABLED", None)


@pytest.fixture(autouse=True)
def dns_stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[DnsStub]:
    """Autouse, repo-wide: give every test an in-process resolver.

    This used to live in ``tests/unit/domains/critique/conftest.py``, because
    critique was the only domain whose SSRF guard resolved DNS. It is no longer
    the only one: the brand, scorecard and SEO *fetch* paths now screen with
    :func:`~src.shared.security.url_validator.validate_fetch_url`, which
    resolves too. Two consequences, both of which want this at the root:

    * Those suites are full of placeholder hosts. ``https://example.com/page``
      (``tests/unit/domains/brand/test_brand_ssrf_guard.py``) would resolve for
      real, from inside the test process, once per guarded call -- a live
      network dependency under ``-n 4`` that ``--reruns 2`` would paper over.
    * ``example.test`` and friends are reserved *never* to resolve (RFC 6761),
      and the guard fails closed, so they would flip from "allowed" to "blocked"
      for a reason that has nothing to do with what the test asserts.

    Hermetic-by-default is the invariant worth holding: no unit test should be
    able to emit a DNS query at all. A test that needs a specific answer says so
    with ``dns_stub.map(...)`` / ``dns_stub.fail(...)``.

    See ``tests/dns_stub.py`` for the resolution policy, why the patch targets
    ``url_validator.socket`` rather than the process-global ``socket`` module,
    and the one case you must still stub by hand (a public-*looking* name whose
    DNS answer points into the ``100.64.0.0/10`` tailnet).

    Yields:
        The installed stub, so a test can pin an answer with
        ``dns_stub.map("rebind.example.com", TAILNET_ANSWER)``.
    """
    yield install_dns_stub(monkeypatch)


# ---------------------------------------------------------------------------
# Explicit trust lanes (w2v-common >= 0.89.0)
# ---------------------------------------------------------------------------

# w2v-common 0.89.0 made TRUSTED_CIDRS default to EMPTY: no peer is trusted
# unless the operator names the networks. Tests that exercise the trusted-
# network lane must configure it explicitly — they may not lean on a default.
# These are the lanes the tests were written against (the pre-0.89 default);
# 192.168.0.0/16 stays out (SSR-proxy bypass vector, 2026-04-18).
EXPLICIT_TRUSTED_CIDRS = "127.0.0.1/32,::1/128,172.16.0.0/12,10.0.0.0/8"


@pytest.fixture
def explicit_trusted_cidrs(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure TRUSTED_CIDRS explicitly for the duration of one test.

    ``TRUSTED_NETWORKS`` is parsed from the env once, at import, and
    ``_is_trusted_network`` / the dispatch lanes read that module global at
    call time — so set the env (for anything re-reading it) AND pin the parsed
    global, instead of reloading the module mid-suite.
    """
    import ipaddress

    import w2v_common.auth.middleware as auth_middleware

    monkeypatch.setenv("TRUSTED_CIDRS", EXPLICIT_TRUSTED_CIDRS)
    monkeypatch.setattr(
        auth_middleware,
        "TRUSTED_NETWORKS",
        [ipaddress.ip_network(c, strict=False) for c in EXPLICIT_TRUSTED_CIDRS.split(",")],
    )
    return EXPLICIT_TRUSTED_CIDRS


# ---------------------------------------------------------------------------
# Mock Redis client (used by critique, brand, redesign, review)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis() -> AsyncMock:
    """Mock RedisClient with common async methods."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    redis.exists = AsyncMock(return_value=False)
    redis.hset = AsyncMock(return_value=True)
    redis.hgetall = AsyncMock(return_value={})
    redis.pipeline = MagicMock()
    redis.ping = AsyncMock(return_value=True)
    redis.close = AsyncMock()
    redis.connect = AsyncMock()
    redis.disconnect = AsyncMock()
    redis.is_available = True
    return redis


# ---------------------------------------------------------------------------
# Mock database session (used by critique, brand, redesign)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_db_session() -> AsyncMock:
    """Async mock AsyncSession for database operations."""
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    session.execute = AsyncMock()
    session.delete = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Critique-domain fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_findings() -> list[dict]:
    """List of findings covering all categories and severities."""
    return [
        {
            "finding_id": str(uuid.uuid7()),
            "category": "seo",
            "severity": "high",
            "title": "Missing Open Graph tags",
            "description": "No OG tags found",
            "url": "https://example.com",
            "recommendation": "Add OG meta tags",
            "check_id": "seo-og-missing",
            "source_tool": "seo_scanner",
        },
        {
            "finding_id": str(uuid.uuid7()),
            "category": "security",
            "severity": "critical",
            "title": "Missing CSP header",
            "description": "No Content-Security-Policy header",
            "url": "https://example.com",
            "recommendation": "Add CSP header",
            "check_id": "sec-missing-csp",
            "source_tool": "security_scanner",
        },
        {
            "finding_id": str(uuid.uuid7()),
            "category": "accessibility",
            "severity": "medium",
            "title": "Images missing alt text",
            "description": "2 images without alt attributes",
            "url": "https://example.com",
            "recommendation": "Add alt text",
            "check_id": "a11y-img-alt",
            "source_tool": "a11y_scanner",
            "wcag_criterion": "1.1.1",
        },
        {
            "finding_id": str(uuid.uuid7()),
            "category": "performance",
            "severity": "low",
            "title": "No resource hints",
            "description": "Missing preconnect hints",
            "url": "https://example.com",
            "recommendation": "Add preconnect",
            "check_id": "perf-no-resource-hints",
            "source_tool": "performance_scanner",
        },
        {
            "finding_id": str(uuid.uuid7()),
            "category": "ux",
            "severity": "medium",
            "title": "Unclear navigation",
            "description": "Navigation could be improved",
            "url": "https://example.com",
            "recommendation": "Simplify nav",
            "source_tool": "ux_reviewer",
            "false_positive_risk": "medium",
        },
    ]


# ---------------------------------------------------------------------------
# Real database fixtures (postgres+psycopg, TRUNCATE-based isolation)
# ---------------------------------------------------------------------------

# Per-process guard for the xdist per-worker schema provisioning below. An xdist
# worker is a separate OS process, so this module-level set is private to one
# worker: every connection that worker opens targets the SAME schema name. The
# catalog-mutating DDL (CREATE SCHEMA + GRANT) only needs to run ONCE per worker;
# gating it here stops two concurrently-checked-out NullPool connections from
# issuing racing GRANTs on the same pg_namespace tuple ("tuple concurrently
# updated"). ``SET search_path`` is NOT gated — it is session-local and must run
# on every connect.
_PROVISIONED_SCHEMAS: set[str] = set()


@pytest.fixture
async def db_engine():
    """Per-test async postgres engine with TRUNCATE-based isolation.

    Connects to _TEST_DATABASE_URL captured at module load (CI postgres in
    CI, localhost:5434 locally). Runs Base.metadata.create_all (idempotent)
    then TRUNCATEs every model table so each test starts from a clean slate.

    Uses ``NullPool`` so each connection is opened on demand and closed on
    return: this service merged five into one, so its real_db suite runs ~90
    of these per CI job through the shared pgbouncer (NodePort -> master:5433).
    A pooled engine would leave idle connections to be reaped between tests,
    surfacing as ``server closed the connection unexpectedly`` on the next
    checkout. The setup connect is also retried with backoff, because that
    same shared pgbouncer occasionally drops a brand-new connection under
    churn — riding over the transient is correct here, the data is recreated
    idempotently inside a single transaction that rolls back on failure.
    """
    # Import all model modules so their tables register on Base.metadata
    # before create_all runs.
    # Phase D RLS: apply the same ENABLE+tenant_isolation policies on create_all that
    # the alembic migration applies in prod (single source: w2v_common.database.rls).
    from w2v_common.database.rls import register_rls_ddl

    from src.domains.analyze import models as _analyze_models  # noqa: F401
    from src.domains.analyze import review_job_models as _analyze_review_models  # noqa: F401
    from src.domains.brand import models as _brand_models  # noqa: F401
    from src.domains.critique import models as _critique_models  # noqa: F401
    from src.domains.icp import models as _icp_models  # noqa: F401
    from src.domains.review import models as _review_models  # noqa: F401
    from src.shared.database.base import Base

    register_rls_ddl(Base.metadata)

    engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)

    # pytest-xdist isolation: when running in parallel, give each worker its own
    # schema so per-test ``TRUNCATE`` (and create_all) never races a sibling worker.
    # The archetype already pins a per-JOB ``search_path`` via PGOPTIONS; this narrows
    # it to a per-WORKER schema. Serial runs (no PYTEST_XDIST_WORKER) are unchanged.
    _worker = os.environ.get("PYTEST_XDIST_WORKER")
    if _worker:
        from sqlalchemy import event

        _schema = f"test_{_worker}"

        @event.listens_for(engine.sync_engine, "connect")
        def _isolate_worker_schema(dbapi_conn: object, _rec: object) -> None:
            cur = dbapi_conn.cursor()  # ty: ignore[unresolved-attribute]
            try:
                # Run the catalog-mutating DDL once per worker: two connections
                # checked out at the same time (NullPool → every checkout connects)
                # would otherwise fire concurrent GRANTs on the same pg_namespace
                # tuple → "tuple concurrently updated". Add to the set only AFTER
                # both statements succeed so a failure does not poison later connects.
                if _schema not in _PROVISIONED_SCHEMAS:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{_schema}"')
                    # The RLS-isolation test SET ROLEs to a throwaway non-owner role that
                    # must still resolve tables in this worker's schema → grant USAGE to all.
                    cur.execute(f'GRANT USAGE ON SCHEMA "{_schema}" TO PUBLIC')
                    _PROVISIONED_SCHEMAS.add(_schema)
                # Session-local: must run on EVERY connect, never gated.
                cur.execute(f'SET search_path TO "{_schema}"')
            finally:
                cur.close()

    last_exc: OperationalError | None = None
    for attempt in range(5):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                if Base.metadata.sorted_tables:
                    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
                    await conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))
            break
        except OperationalError as exc:
            last_exc = exc
            await asyncio.sleep(0.5 * (attempt + 1))
    else:
        assert last_exc is not None
        raise last_exc
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    """Real async_sessionmaker for analysis_service tests."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def db_session(db_session_factory):
    """Real AsyncSession for a single test."""
    async with db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Real Redis fixtures (fakeredis)
# ---------------------------------------------------------------------------


@pytest.fixture
def real_redis_client():
    """RedisClient backed by fakeredis (real commands, in-memory)."""
    client = RedisClient(redis_url="redis://localhost:6379/0")
    inject_fakeredis(client)
    return client
