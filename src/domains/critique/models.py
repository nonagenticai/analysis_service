"""SQLAlchemy ORM models for critique_service."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

# ``tenant_id`` is a native Postgres ``uuid`` (migration 0013), declared with
# **as_uuid=False**. That flag is LOAD-BEARING, not style:
#
# * ``as_uuid=True`` returns ``uuid.UUID`` objects, and ``uuid.UUID(t) == t`` is
#   False for every string ``t``. This repo carries the tenant as ``str`` in ~118
#   annotations across 31 files, so every ``row.tenant_id == some_str`` ownership
#   guard would silently evaluate False — 404ing the owner on their own rows —
#   and every scope predicate would match nothing. ``==`` between ``UUID`` and
#   ``str`` is not a type error, so ty and ruff stay green while prod breaks.
# * ``bind_session_tenant`` ends in ``set_config('app.current_tenant', $2, true)``,
#   which is ``(text, text, bool)`` with no uuid overload; a ``uuid.UUID`` there
#   raises ``UndefinedFunction`` and kills every transaction under RLS.
#
# With ``as_uuid=False`` the Python surface does not change at all — the
# ``Mapped[str]`` / ``Mapped[str | None]`` annotations below are still exactly
# right — and only the Postgres column type moves ``varchar(255)`` -> ``uuid``.
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.database.base import Base


class CritiqueJob(Base):
    """Critique job tracking.

    Attributes:
        job_id: Unique job identifier (UUID).
        tenant_id: Owning tenant.
        url: Target URL being critiqued.
        domain: Extracted domain name.
        status: Job status (pending, running, completed, failed).
        findings: JSONB list of finding dicts.
        category_scores: JSONB dict of category-to-score mappings.
        overall_score: Weighted overall score (0-100).
    """

    __tablename__ = "critique_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    url: Mapped[str] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    categories: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    max_pages: Mapped[int] = mapped_column(Integer, default=1)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_repo: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    category_scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    report_markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    pages_scanned: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0)
    overall_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(10), default="2.0")
    confidence_scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    webhook_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # AuthInfo (type=basic|bearer|cookies + credentials) the orchestrator
    # forwards so the critique fetcher can reach an authenticated target.
    # See migration 0003_critique_jobs_auth_info; at-rest encryption is a
    # tracked follow-up.
    auth_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_critique_jobs_status_created", "status", "created_at"),
        Index("ix_critique_jobs_tenant_created", "tenant_id", "created_at"),
        Index("ix_critique_jobs_domain", "domain"),
        Index(
            "ix_critique_jobs_tenant_domain_created",
            "tenant_id",
            "domain",
            "created_at",
        ),
    )


class CritiqueUsage(Base):
    """Daily critique usage aggregation per tenant.

    Attributes:
        tenant_id: Owning tenant.
        date: Aggregation date (midnight UTC).
        scan_count: Number of scans on this date.
        pages_scanned: Total pages scanned.
        findings_count: Total findings produced.
        error_count: Number of failed scans.
    """

    __tablename__ = "critique_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scan_count: Mapped[int] = mapped_column(Integer, default=0)
    pages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)


class CritiqueBatch(Base):
    """Batch critique tracking.

    Attributes:
        batch_id: Unique batch identifier.
        tenant_id: Owning tenant.
        job_ids: List of constituent job IDs.
        total: Total jobs in the batch.
        completed: Count of completed jobs.
        failed: Count of failed jobs.
        status: Overall batch status (pending, running, completed).
    """

    __tablename__ = "critique_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    job_ids: Mapped[list] = mapped_column(JSONB, default=list)
    total: Mapped[int] = mapped_column(Integer, default=0)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """Audit log for tracking events."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    tenant_id: Mapped[str | None] = mapped_column(PgUUID(as_uuid=False), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CritiqueFinding(Base):
    """Individual critique finding (normalized from JSONB).

    Dual-written alongside the JSONB findings column on CritiqueJob
    to support efficient querying and analytics.

    Carries its own NOT NULL ``tenant_id`` (copied from the parent
    ``CritiqueJob`` on write). That column is what enrols the table in RLS:
    ``w2v_common.database.rls.tenant_table_names`` selects every table with a
    ``tenant_id``, so the strict ``tenant_isolation`` policy is rendered for
    ``critique_findings`` by both the migration and the ``create_all`` event.
    Before it existed this table was the one tenant-owned table in the service
    sitting entirely outside RLS, protected only by application-level joins.

    NOT NULL here is both the TARGET state and, since ``0009``, the state of
    prod: ``0008`` shipped the column NULLABLE on purpose (expand — so old
    replicas that do not stamp it survived the rolling deploy) and
    ``0009_critique_tenant_not_null`` contracted it, re-backfilling, deleting the
    un-tenantable remainder, running ``SET NOT NULL`` and replacing the loose
    global-or-own policy set with the strict ``tenant_isolation`` policy. So the
    ``create_all`` schema (tests/dev) and the migrated schema now agree, and no
    ``tenant_id IS NULL`` read escape exists. See ``0008``/``0009`` docstrings.
    """

    __tablename__ = "critique_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    # nullable=False is explicit, not incidental: this column's nullability is
    # what selects the RLS policy shape in ``w2v_common.database.rls`` (NOT NULL
    # → strict ``tenant_isolation``; NULLABLE → the global-or-own set where a
    # NULL row is readable by every tenant). Stating it here keeps the
    # ``create_all`` test schema identical to what ``0009`` leaves in prod.
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True, nullable=False)
    finding_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(50), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    check_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    wcag_criterion: Mapped[str | None] = mapped_column(String(20), nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_tool: Mapped[str | None] = mapped_column(String(100), nullable=True)
    false_positive_risk: Mapped[str | None] = mapped_column(String(20), nullable=True)
    remediation_effort: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    is_suppressed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    suppression_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    __table_args__ = (
        Index("ix_critique_findings_job_category", "job_id", "category"),
        Index("ix_critique_findings_severity_category", "severity", "category"),
        Index("ix_critique_findings_created_at", "created_at"),
    )


class FindingSuppression(Base):
    """Suppression rules for known/accepted findings.

    Matches findings by check_id, title_hash, or category to mark them
    as suppressed during scoring and reporting.
    """

    __tablename__ = "finding_suppressions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    suppression_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    check_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    title_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="suppressed")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_suppressions_tenant_domain", "tenant_id", "domain"),
        Index("ix_suppressions_check_id", "check_id"),
    )


class CritiqueSchedule(Base):
    """Scheduled recurring scan configuration.

    Supports preset cron expressions (@hourly, @daily, @weekly, etc.)
    to automatically trigger critique jobs on a schedule.
    """

    __tablename__ = "critique_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    tenant_id: Mapped[str] = mapped_column(PgUUID(as_uuid=False), index=True)
    # HOW the creating request established its tenant (``TenantSource`` value).
    #
    # The scheduler tick has NO request scope: it establishes identity from this
    # row alone. Without provenance persisted here it can only call
    # ``set_identity_context`` with ``tenant_source=None``, which the outbound
    # hook correctly treats as UNVOUCHED -- so every metered hop a scheduled
    # critique makes (worker_management via WMClient) raises
    # ``UnvouchedTenantError``. Provenance is captured at creation time, which IS
    # in request scope, and restored at the tick.
    #
    # NULLABLE with NO server_default ON PURPOSE. Rows written before this column
    # existed genuinely have unknown provenance, and a default would be a FORGED
    # vouch: it would hand an arbitrary stored tenant the right to spend at a
    # metered peer on the strength of a value nobody ever proved. NULL means "not
    # recorded" and the tick fails closed on it.
    tenant_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    url: Mapped[str] = mapped_column(Text)
    categories: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    cron_expression: Mapped[str] = mapped_column(String(50), default="@daily")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_schedules_tenant", "tenant_id"),
        Index("ix_schedules_next_run", "enabled", "next_run_at"),
    )
