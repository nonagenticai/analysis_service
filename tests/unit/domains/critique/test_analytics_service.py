"""Tests for AnalyticsService."""

from unittest.mock import AsyncMock, MagicMock

from src.domains.critique.services.analytics_service import AnalyticsService


class TestAnalyticsServiceDomainTrends:
    """Test get_domain_trends method."""

    async def test_domain_trends_returns_list(self):
        svc = AnalyticsService()
        db = AsyncMock()

        mock_job = MagicMock()
        mock_job.job_id = "j1"
        mock_job.created_at = MagicMock()
        mock_job.created_at.isoformat.return_value = "2024-01-01T00:00:00+00:00"
        mock_job.overall_score = 85.0
        mock_job.category_scores = {"seo": 90.0}

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_job]
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_domain_trends(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "example.com", 30
        )
        assert len(result) == 1
        assert result[0]["job_id"] == "j1"
        assert result[0]["overall_score"] == 85.0

    async def test_domain_trends_empty(self):
        svc = AnalyticsService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_domain_trends(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "nope.com", 30
        )
        assert result == []


class TestAnalyticsServiceDomainAnalytics:
    """Test get_domain_analytics method."""

    async def test_no_jobs(self):
        svc = AnalyticsService()
        db = AsyncMock()

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc.get_domain_analytics(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "nope.com"
        )
        assert result["total_scans"] == 0
        assert result["avg_score"] is None
        assert result["score_trend"] == "stable"

    async def test_with_jobs_improving(self):
        svc = AnalyticsService()
        db = AsyncMock()

        # Create mock jobs with improving scores (most recent first)
        jobs = []
        for i, score in enumerate([90, 88, 85, 80, 78]):
            job = MagicMock()
            job.job_id = f"j{i}"
            job.overall_score = float(score)
            job.category_scores = {"seo": float(score)}
            jobs.append(job)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = jobs

        # Mock for _get_top_findings_for_jobs
        findings_result = MagicMock()
        findings_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[mock_result, findings_result])

        result = await svc.get_domain_analytics(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "example.com"
        )
        assert result["total_scans"] == 5
        assert result["score_trend"] == "improving"
        assert result["avg_score"] is not None

    async def test_with_jobs_declining(self):
        svc = AnalyticsService()
        db = AsyncMock()

        jobs = []
        for i, score in enumerate([70, 75, 80, 85, 90]):
            job = MagicMock()
            job.job_id = f"j{i}"
            job.overall_score = float(score)
            job.category_scores = {"seo": float(score)}
            jobs.append(job)

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = jobs

        findings_result = MagicMock()
        findings_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[mock_result, findings_result])

        result = await svc.get_domain_analytics(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "example.com"
        )
        assert result["score_trend"] == "declining"

    async def test_single_job_stable(self):
        svc = AnalyticsService()
        db = AsyncMock()

        job = MagicMock()
        job.job_id = "j1"
        job.overall_score = 85.0
        job.category_scores = {"seo": 85.0}

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [job]

        findings_result = MagicMock()
        findings_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[mock_result, findings_result])

        result = await svc.get_domain_analytics(
            db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e", "example.com"
        )
        assert result["score_trend"] == "stable"


class TestAnalyticsServiceTenantSummary:
    """Test get_tenant_summary method."""

    async def test_empty_summary(self):
        svc = AnalyticsService()
        db = AsyncMock()

        count_result = MagicMock()
        count_result.scalar.return_value = 0

        domain_result = MagicMock()
        domain_result.all.return_value = []

        db.execute = AsyncMock(side_effect=[count_result, domain_result])

        result = await svc.get_tenant_summary(db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")
        assert result["total_scans"] == 0
        assert result["total_domains"] == 0
        assert result["avg_score"] is None

    async def test_summary_with_domains(self):
        svc = AnalyticsService()
        db = AsyncMock()

        count_result = MagicMock()
        count_result.scalar.return_value = 5

        domain_row = MagicMock()
        domain_row.__getitem__ = lambda self, idx: ["example.com", 5][idx]

        domain_result = MagicMock()
        domain_result.all.return_value = [domain_row]

        latest_row = MagicMock()
        latest_row.__getitem__ = lambda self, idx: [
            85.0,
            MagicMock(isoformat=lambda: "2024-01-01"),
        ][idx]
        latest_result = MagicMock()
        latest_result.first.return_value = latest_row

        db.execute = AsyncMock(side_effect=[count_result, domain_result, latest_result])

        result = await svc.get_tenant_summary(db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")
        assert result["total_scans"] == 5
        assert result["total_domains"] == 1
        assert len(result["domains"]) == 1


class TestAnalyticsServiceTopFindings:
    """Test get_top_findings method."""

    async def test_no_jobs(self):
        svc = AnalyticsService()
        db = AsyncMock()

        job_result = MagicMock()
        job_result.all.return_value = []
        db.execute = AsyncMock(return_value=job_result)

        result = await svc.get_top_findings(db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")
        assert result == []

    async def test_with_findings(self):
        svc = AnalyticsService()
        db = AsyncMock()

        job_row = MagicMock()
        job_row.__getitem__ = lambda self, idx: "j1"
        job_result = MagicMock()
        job_result.all.return_value = [job_row]

        finding_row = MagicMock()
        finding_row.__getitem__ = lambda self, idx: [
            "sec-csp",
            "Missing CSP",
            "security",
            "critical",
            5,
        ][idx]
        finding_result = MagicMock()
        finding_result.all.return_value = [finding_row]

        db.execute = AsyncMock(side_effect=[job_result, finding_result])

        result = await svc.get_top_findings(db, "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e")
        assert len(result) == 1
        assert result[0]["check_id"] == "sec-csp"
        assert result[0]["count"] == 5


class TestAnalyticsServiceTopFindingsForJobs:
    """Test _get_top_findings_for_jobs."""

    async def test_empty_job_ids(self):
        svc = AnalyticsService()
        db = AsyncMock()

        result = await svc._get_top_findings_for_jobs(db, [])
        assert result == []

    async def test_with_job_ids(self):
        svc = AnalyticsService()
        db = AsyncMock()

        row = MagicMock()
        row.__getitem__ = lambda self, idx: [
            "a11y-img-alt",
            "Missing alt text",
            "accessibility",
            "medium",
            3,
        ][idx]

        mock_result = MagicMock()
        mock_result.all.return_value = [row]
        db.execute = AsyncMock(return_value=mock_result)

        result = await svc._get_top_findings_for_jobs(db, ["j1", "j2"])
        assert len(result) == 1
        assert result[0]["count"] == 3
