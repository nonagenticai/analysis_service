"""Initial combined migration for analysis_service.

Creates all tables from 5 merged domains:
- analyze: analysis_jobs, analysis_usage, user_context_profiles
- critique: critique_jobs, critique_usage, critique_batches, audit_logs,
            critique_findings, finding_suppressions, critique_schedules
- review: review_jobs, review_usage, review_batches
- brand: brand_extraction_logs, brands, brand_versions, brand_overrides, logo_assets
- redesign: redesign_jobs, redesign_usage, redesign_variants, custom_styles,
            batch_jobs, webhook_delivery_log

Revision ID: a3f7b1c9d4e2
Revises:
Create Date: 2026-03-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a3f7b1c9d4e2"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # Analyze domain
    # -----------------------------------------------------------------------

    op.create_table(
        "analysis_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("pages_input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("pages_analyzed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("site_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("narrative_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("results_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("analysis_artifacts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("total_pages", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_analysis_jobs_job_id", "analysis_jobs", ["job_id"])
    op.create_index("ix_analysis_jobs_tenant_id", "analysis_jobs", ["tenant_id"])

    op.create_table(
        "analysis_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column(
            "date",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("analysis_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_analyzed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_usage_tenant_id", "analysis_usage", ["tenant_id"])

    op.create_table(
        "user_context_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("profile_name", sa.String(length=255), nullable=False),
        sa.Column("context_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_context_profiles_tenant_id", "user_context_profiles", ["tenant_id"])

    # -----------------------------------------------------------------------
    # Critique domain
    # -----------------------------------------------------------------------

    op.create_table(
        "critique_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("max_pages", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("github_repo", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("category_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("scan_duration", sa.Float(), nullable=True),
        sa.Column("report_markdown", sa.Text(), nullable=True),
        sa.Column("pages_scanned", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("schema_version", sa.String(length=10), server_default="2.0"),
        sa.Column("confidence_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("progress", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("webhook_config", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_critique_jobs_job_id", "critique_jobs", ["job_id"])
    op.create_index("ix_critique_jobs_tenant_id", "critique_jobs", ["tenant_id"])
    op.create_index("ix_critique_jobs_status", "critique_jobs", ["status"])
    op.create_index("ix_critique_jobs_status_created", "critique_jobs", ["status", "created_at"])
    op.create_index("ix_critique_jobs_tenant_created", "critique_jobs", ["tenant_id", "created_at"])
    op.create_index("ix_critique_jobs_domain", "critique_jobs", ["domain"])
    op.create_index(
        "ix_critique_jobs_tenant_domain_created",
        "critique_jobs",
        ["tenant_id", "domain", "created_at"],
    )

    op.create_table(
        "critique_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column(
            "date",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("scan_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_scanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_critique_usage_tenant_id", "critique_usage", ["tenant_id"])
    op.create_index("ix_critique_usage_date", "critique_usage", ["date"])

    op.create_table(
        "critique_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column(
            "job_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id"),
    )
    op.create_index("ix_critique_batches_batch_id", "critique_batches", ["batch_id"])
    op.create_index("ix_critique_batches_tenant_id", "critique_batches", ["tenant_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=True),
        sa.Column("job_id", sa.String(length=36), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_event_type", "audit_logs", ["event_type"])
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_job_id", "audit_logs", ["job_id"])

    op.create_table(
        "critique_findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("finding_id", sa.String(length=36), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.Text(), nullable=True),
        sa.Column("check_id", sa.String(length=100), nullable=True),
        sa.Column("wcag_criterion", sa.String(length=20), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("source_tool", sa.String(length=100), nullable=True),
        sa.Column("false_positive_risk", sa.String(length=20), nullable=True),
        sa.Column("remediation_effort", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "is_suppressed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("suppression_id", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("finding_id"),
    )
    op.create_index("ix_critique_findings_job_id", "critique_findings", ["job_id"])
    op.create_index("ix_critique_findings_finding_id", "critique_findings", ["finding_id"])
    op.create_index("ix_critique_findings_category", "critique_findings", ["category"])
    op.create_index("ix_critique_findings_severity", "critique_findings", ["severity"])
    op.create_index("ix_critique_findings_check_id", "critique_findings", ["check_id"])
    op.create_index(
        "ix_critique_findings_job_category",
        "critique_findings",
        ["job_id", "category"],
    )
    op.create_index(
        "ix_critique_findings_severity_category",
        "critique_findings",
        ["severity", "category"],
    )
    op.create_index("ix_critique_findings_created_at", "critique_findings", ["created_at"])

    op.create_table(
        "finding_suppressions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("suppression_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.Column("check_id", sa.String(length=100), nullable=True),
        sa.Column("title_hash", sa.String(length=64), nullable=True),
        sa.Column("category", sa.String(length=50), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="suppressed"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("suppression_id"),
    )
    op.create_index(
        "ix_finding_suppressions_suppression_id",
        "finding_suppressions",
        ["suppression_id"],
    )
    op.create_index("ix_finding_suppressions_tenant_id", "finding_suppressions", ["tenant_id"])
    op.create_index("ix_finding_suppressions_check_id", "finding_suppressions", ["check_id"])
    op.create_index(
        "ix_suppressions_tenant_domain",
        "finding_suppressions",
        ["tenant_id", "domain"],
    )
    op.create_index("ix_suppressions_check_id", "finding_suppressions", ["check_id"])

    op.create_table(
        "critique_schedules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "cron_expression",
            sa.String(length=50),
            nullable=False,
            server_default="@daily",
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_job_id", sa.String(length=36), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schedule_id"),
    )
    op.create_index("ix_critique_schedules_schedule_id", "critique_schedules", ["schedule_id"])
    op.create_index("ix_critique_schedules_tenant_id", "critique_schedules", ["tenant_id"])
    op.create_index("ix_schedules_tenant", "critique_schedules", ["tenant_id"])
    op.create_index("ix_schedules_next_run", "critique_schedules", ["enabled", "next_run_at"])

    # -----------------------------------------------------------------------
    # Review domain
    # -----------------------------------------------------------------------

    op.create_table(
        "review_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("review_types", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("results", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("domain", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_review_jobs_job_id", "review_jobs", ["job_id"])
    op.create_index("ix_review_jobs_tenant_id", "review_jobs", ["tenant_id"])
    op.create_index("ix_review_jobs_domain", "review_jobs", ["domain"])

    op.create_table(
        "review_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "date", name="uq_usage_tenant_date"),
    )
    op.create_index("ix_review_usage_tenant_id", "review_usage", ["tenant_id"])

    op.create_table(
        "review_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column(
            "job_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id"),
    )
    op.create_index("ix_review_batches_batch_id", "review_batches", ["batch_id"])
    op.create_index("ix_review_batches_tenant_id", "review_batches", ["tenant_id"])

    # -----------------------------------------------------------------------
    # Brand domain
    # -----------------------------------------------------------------------

    op.create_table(
        "brand_extraction_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brand_extraction_logs_tenant_id", "brand_extraction_logs", ["tenant_id"])

    op.create_table(
        "brands",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=512), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("logo_asset_id", sa.String(length=255), nullable=True),
        sa.Column("has_logo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "primary_color",
            sa.String(length=20),
            nullable=False,
            server_default="#2563EB",
        ),
        sa.Column(
            "secondary_color",
            sa.String(length=20),
            nullable=False,
            server_default="#1E40AF",
        ),
        sa.Column("fonts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("color_palette", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("social_images", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="url"),
        sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tagline", sa.Text(), nullable=True),
        sa.Column("brand_voice", sa.Text(), nullable=True),
        sa.Column("social_profiles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("industry", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("content_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("logo_intelligence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("quality_score", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extraction_methods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("font_family_primary", sa.String(length=255), nullable=True),
        sa.Column("font_family_secondary", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "domain", name="uq_brand_tenant_domain"),
    )
    op.create_index("ix_brands_tenant_id", "brands", ["tenant_id"])
    op.create_index("ix_brands_domain", "brands", ["domain"])

    op.create_table(
        "brand_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("brand_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=512), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brand_versions_brand_id", "brand_versions", ["brand_id"])
    op.create_index("ix_brand_versions_tenant_id", "brand_versions", ["tenant_id"])

    op.create_table(
        "brand_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=512), nullable=False),
        sa.Column("overrides", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("applied_by", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_brand_overrides_tenant_id", "brand_overrides", ["tenant_id"])
    op.create_index("ix_brand_overrides_domain", "brand_overrides", ["domain"])

    op.create_table(
        "logo_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.String(length=255), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=512), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=True),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=100),
            nullable=False,
            server_default="image/png",
        ),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id"),
    )
    op.create_index("ix_logo_assets_asset_id", "logo_assets", ["asset_id"])
    op.create_index("ix_logo_assets_tenant_id", "logo_assets", ["tenant_id"])

    # -----------------------------------------------------------------------
    # Redesign domain
    # -----------------------------------------------------------------------

    op.create_table(
        "redesign_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("html_content", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=20), nullable=False, server_default="full_page"),
        sa.Column("style", sa.String(length=50), nullable=False, server_default="modern"),
        sa.Column("page_type", sa.String(length=50), nullable=True),
        sa.Column("critique_findings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("brand_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("site_analysis", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("template_version", sa.String(length=20), nullable=True),
        sa.Column("user_context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("styles", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("variant_count", sa.Integer(), nullable=True),
        sa.Column("parent_job_id", sa.String(length=36), nullable=True),
        sa.Column("root_job_id", sa.String(length=36), nullable=True),
        sa.Column("iteration_number", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("feedback_text", sa.Text(), nullable=True),
        sa.Column("component_target", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("component_html_path", sa.String(length=500), nullable=True),
        sa.Column("accessibility_score", sa.Float(), nullable=True),
        sa.Column(
            "accessibility_report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "original_screenshot_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("comparison_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column("webhook_secret", sa.String(length=512), nullable=True),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("priority_score", sa.Float(), nullable=True),
        sa.Column("redesigned_html_path", sa.String(length=500), nullable=True),
        sa.Column(
            "section_html_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("screenshot_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "concept_image_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "html_validation_report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("pages_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index("ix_redesign_jobs_job_id", "redesign_jobs", ["job_id"])
    op.create_index("ix_redesign_jobs_tenant_id", "redesign_jobs", ["tenant_id"])
    op.create_index("ix_redesign_jobs_status", "redesign_jobs", ["status"])
    op.create_index("ix_redesign_jobs_parent_job_id", "redesign_jobs", ["parent_job_id"])
    op.create_index("ix_redesign_jobs_root_job_id", "redesign_jobs", ["root_job_id"])
    op.create_index("ix_redesign_jobs_batch_id", "redesign_jobs", ["batch_id"])
    op.create_index("ix_redesign_jobs_status_created", "redesign_jobs", ["status", "created_at"])

    op.create_table(
        "redesign_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_redesigned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("screenshots_rendered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "concept_images_generated",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("llm_tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("variants_generated", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "date", name="uq_redesign_usage_tenant_date"),
    )
    op.create_index("ix_redesign_usage_tenant_id", "redesign_usage", ["tenant_id"])
    op.create_index("ix_redesign_usage_date", "redesign_usage", ["date"])

    op.create_table(
        "redesign_variants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("variant_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("style", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("redesigned_html_path", sa.String(length=500), nullable=True),
        sa.Column("screenshot_paths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "concept_image_paths",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("variant_id"),
    )
    op.create_index("ix_redesign_variants_variant_id", "redesign_variants", ["variant_id"])
    op.create_index("ix_redesign_variants_job_id", "redesign_variants", ["job_id"])

    op.create_table(
        "custom_styles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("style_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color_palette", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("typography", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "example_references",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("style_id"),
    )
    op.create_index("ix_custom_styles_style_id", "custom_styles", ["style_id"])
    op.create_index("ix_custom_styles_tenant_id", "custom_styles", ["tenant_id"])
    op.create_index("ix_custom_styles_tenant_slug", "custom_styles", ["tenant_id", "slug"])

    op.create_table(
        "batch_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("total_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shared_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id"),
    )
    op.create_index("ix_batch_jobs_batch_id", "batch_jobs", ["batch_id"])
    op.create_index("ix_batch_jobs_tenant_id", "batch_jobs", ["tenant_id"])

    op.create_table(
        "webhook_delivery_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_delivery_log_job_id", "webhook_delivery_log", ["job_id"])


def downgrade() -> None:
    # Redesign domain
    op.drop_table("webhook_delivery_log")
    op.drop_table("batch_jobs")
    op.drop_table("custom_styles")
    op.drop_table("redesign_variants")
    op.drop_table("redesign_usage")
    op.drop_table("redesign_jobs")

    # Brand domain (brand_versions before brands due to FK)
    op.drop_table("logo_assets")
    op.drop_table("brand_overrides")
    op.drop_table("brand_versions")
    op.drop_table("brands")
    op.drop_table("brand_extraction_logs")

    # Review domain
    op.drop_table("review_batches")
    op.drop_table("review_usage")
    op.drop_table("review_jobs")

    # Critique domain
    op.drop_table("critique_schedules")
    op.drop_table("finding_suppressions")
    op.drop_table("critique_findings")
    op.drop_table("audit_logs")
    op.drop_table("critique_batches")
    op.drop_table("critique_usage")
    op.drop_table("critique_jobs")

    # Analyze domain
    op.drop_table("user_context_profiles")
    op.drop_table("analysis_usage")
    op.drop_table("analysis_jobs")
