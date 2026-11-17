"""Trend analytics and domain-level analytics service."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.models import CritiqueFinding, CritiqueJob

logger = logging.getLogger(__name__)


class AnalyticsService:
    """Provides analytics over critique jobs and findings."""

    async def get_domain_trends(
        self,
        db: AsyncSession,
        tenant_id: str,
        domain: str,
        days: int = 30,
    ) -> list[dict]:
        """Get score timeline for a domain over N days.

        Returns list of {job_id, date, overall_score, category_scores} sorted by date.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await db.execute(
            select(CritiqueJob)
            .where(
                CritiqueJob.tenant_id == tenant_id,
                CritiqueJob.domain == domain,
                CritiqueJob.status == "completed",
                CritiqueJob.created_at >= cutoff,
            )
            .order_by(CritiqueJob.created_at)
        )
        jobs = result.scalars().all()

        return [
            {
                "job_id": job.job_id,
                "date": job.created_at.isoformat() if job.created_at else None,
                "overall_score": job.overall_score,
                "category_scores": job.category_scores or {},
            }
            for job in jobs
        ]

    async def get_domain_analytics(
        self,
        db: AsyncSession,
        tenant_id: str,
        domain: str,
    ) -> dict:
        """Get aggregate analytics for a domain.

        Returns: total_scans, avg_score, latest_score, score_trend (improving/declining/stable),
        top_recurring_findings (most common check_ids across jobs).
        """
        # Get all completed jobs for this domain
        result = await db.execute(
            select(CritiqueJob)
            .where(
                CritiqueJob.tenant_id == tenant_id,
                CritiqueJob.domain == domain,
                CritiqueJob.status == "completed",
            )
            .order_by(desc(CritiqueJob.created_at))
        )
        jobs = list(result.scalars().all())

        if not jobs:
            return {
                "domain": domain,
                "total_scans": 0,
                "avg_score": None,
                "latest_score": None,
                "score_trend": "stable",
                "top_recurring_findings": [],
                "category_averages": {},
            }

        scores = [j.overall_score for j in jobs if j.overall_score is not None]
        avg_score = round(sum(scores) / len(scores), 1) if scores else None
        latest_score = scores[0] if scores else None

        # Determine trend from last 5 scans
        recent_scores = scores[:5]
        if len(recent_scores) >= 2:
            delta = recent_scores[0] - recent_scores[-1]
            if delta > 3:
                trend = "improving"
            elif delta < -3:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        # Category averages
        cat_totals: dict[str, list[float]] = {}
        for job in jobs:
            if job.category_scores:
                for cat, score in job.category_scores.items():
                    cat_totals.setdefault(cat, []).append(score)
        cat_averages = {cat: round(sum(vals) / len(vals), 1) for cat, vals in cat_totals.items()}

        # Top recurring findings (by check_id)
        job_ids = [j.job_id for j in jobs]
        top_findings = await self._get_top_findings_for_jobs(db, job_ids, limit=10)

        return {
            "domain": domain,
            "total_scans": len(jobs),
            "avg_score": avg_score,
            "latest_score": latest_score,
            "score_trend": trend,
            "top_recurring_findings": top_findings,
            "category_averages": cat_averages,
        }

    async def get_tenant_summary(
        self,
        db: AsyncSession,
        tenant_id: str,
    ) -> dict:
        """Get cross-domain overview for a tenant.

        Returns: total_scans, total_domains, avg_score, domains list with latest scores.
        """
        # Count total completed scans
        count_result = await db.execute(
            select(func.count(CritiqueJob.id)).where(
                CritiqueJob.tenant_id == tenant_id,
                CritiqueJob.status == "completed",
            )
        )
        total_scans = count_result.scalar() or 0

        # Get unique domains with latest scores
        # Use a subquery to get the latest job per domain
        result = await db.execute(
            select(CritiqueJob.domain, func.count(CritiqueJob.id).label("scan_count"))
            .where(
                CritiqueJob.tenant_id == tenant_id,
                CritiqueJob.status == "completed",
                CritiqueJob.domain.isnot(None),
            )
            .group_by(CritiqueJob.domain)
        )
        domain_rows = result.all()

        domains = []
        for row in domain_rows:
            domain_name = row[0]
            scan_count = row[1]
            # Get latest score for this domain
            latest = await db.execute(
                select(CritiqueJob.overall_score, CritiqueJob.created_at)
                .where(
                    CritiqueJob.tenant_id == tenant_id,
                    CritiqueJob.domain == domain_name,
                    CritiqueJob.status == "completed",
                )
                .order_by(desc(CritiqueJob.created_at))
                .limit(1)
            )
            latest_row = latest.first()
            domains.append(
                {
                    "domain": domain_name,
                    "scan_count": scan_count,
                    "latest_score": latest_row[0] if latest_row else None,
                    "last_scanned": (
                        latest_row[1].isoformat() if latest_row and latest_row[1] else None
                    ),
                }
            )

        all_scores = [d["latest_score"] for d in domains if d["latest_score"] is not None]
        avg_score = round(sum(all_scores) / len(all_scores), 1) if all_scores else None

        return {
            "tenant_id": tenant_id,
            "total_scans": total_scans,
            "total_domains": len(domains),
            "avg_score": avg_score,
            "domains": domains,
        }

    async def get_top_findings(
        self,
        db: AsyncSession,
        tenant_id: str,
        *,
        domain: str | None = None,
        category: str | None = None,
        severity: str | None = None,
        limit: int = 20,
        days: int = 30,
    ) -> list[dict]:
        """Get most common findings across jobs.

        Returns list of {check_id, title, category, severity, count, percentage}.
        """
        cutoff = datetime.now(UTC) - timedelta(days=days)

        # Get job_ids for this tenant (and optionally domain)
        job_query = select(CritiqueJob.job_id).where(
            CritiqueJob.tenant_id == tenant_id,
            CritiqueJob.status == "completed",
            CritiqueJob.created_at >= cutoff,
        )
        if domain:
            job_query = job_query.where(CritiqueJob.domain == domain)

        job_result = await db.execute(job_query)
        job_ids = [r[0] for r in job_result.all()]

        if not job_ids:
            return []

        total_jobs = len(job_ids)

        # Count findings by check_id
        finding_query = (
            select(
                CritiqueFinding.check_id,
                CritiqueFinding.title,
                CritiqueFinding.category,
                CritiqueFinding.severity,
                func.count(CritiqueFinding.id).label("count"),
            )
            .where(
                # ``critique_findings`` carries its own tenant now: scope directly
                # rather than trusting the job_id list to be the only boundary.
                CritiqueFinding.tenant_id == tenant_id,
                CritiqueFinding.job_id.in_(job_ids),
                CritiqueFinding.check_id.isnot(None),
            )
            .group_by(
                CritiqueFinding.check_id,
                CritiqueFinding.title,
                CritiqueFinding.category,
                CritiqueFinding.severity,
            )
            .order_by(func.count(CritiqueFinding.id).desc())
            .limit(limit)
        )

        if category:
            finding_query = finding_query.where(CritiqueFinding.category == category)
        if severity:
            finding_query = finding_query.where(CritiqueFinding.severity == severity)

        result = await db.execute(finding_query)
        rows = result.all()

        return [
            {
                "check_id": row[0],
                "title": row[1],
                "category": row[2],
                "severity": row[3],
                "count": row[4],
                "percentage": round(row[4] / total_jobs * 100, 1) if total_jobs else 0,
            }
            for row in rows
        ]

    async def _get_top_findings_for_jobs(
        self,
        db: AsyncSession,
        job_ids: list[str],
        limit: int = 10,
    ) -> list[dict]:
        """Get top recurring findings across specific jobs."""
        if not job_ids:
            return []

        result = await db.execute(
            select(
                CritiqueFinding.check_id,
                CritiqueFinding.title,
                CritiqueFinding.category,
                CritiqueFinding.severity,
                func.count(CritiqueFinding.id).label("count"),
            )
            .where(
                CritiqueFinding.job_id.in_(job_ids),
                CritiqueFinding.check_id.isnot(None),
            )
            .group_by(
                CritiqueFinding.check_id,
                CritiqueFinding.title,
                CritiqueFinding.category,
                CritiqueFinding.severity,
            )
            .order_by(func.count(CritiqueFinding.id).desc())
            .limit(limit)
        )
        rows = result.all()

        return [
            {
                "check_id": row[0],
                "title": row[1],
                "category": row[2],
                "severity": row[3],
                "count": row[4],
            }
            for row in rows
        ]
