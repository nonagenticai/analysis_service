"""Tests for API schemas -- validate Pydantic models and enums."""

import pytest
from pydantic import ValidationError

from src.domains.critique.schemas import (
    AcknowledgeFindingRequest,
    BatchCritiqueRequest,
    BatchCritiqueResponse,
    BatchStatusResponse,
    CategoryScoreDetail,
    ComparisonResponse,
    ConfigOverridesResponse,
    CreateScheduleRequest,
    CreateSuppressionRequest,
    CritiqueCategory,
    CritiqueJobResponse,
    CritiqueJSONReportResponse,
    CritiqueListResponse,
    CritiqueReportResponse,
    CritiqueRequest,
    CritiqueResultResponse,
    CritiqueSeverity,
    DomainAnalyticsResponse,
    DomainSummaryItem,
    DomainTrendResponse,
    FindingChangeEvent,
    FindingResponse,
    FindingsListResponse,
    HistoricalComparisonResponse,
    RerunRequest,
    ScheduleListResponse,
    ScheduleResponse,
    ScoringWeightsResponse,
    ScoringWeightsUpdateRequest,
    SuppressionListResponse,
    SuppressionResponse,
    TagsUpdateRequest,
    TenantAnalyticsResponse,
    TopFindingResponse,
    TrendDataPoint,
    UpdateScheduleRequest,
    WebhookConfig,
)


class TestCritiqueCategory:
    def test_enum_values(self):
        assert CritiqueCategory.seo == "seo"
        assert CritiqueCategory.security == "security"
        assert CritiqueCategory.accessibility == "accessibility"
        assert CritiqueCategory.performance == "performance"
        assert CritiqueCategory.ux == "ux"
        assert CritiqueCategory.code_quality == "code_quality"


class TestCritiqueSeverity:
    def test_enum_values(self):
        assert CritiqueSeverity.critical == "critical"
        assert CritiqueSeverity.high == "high"
        assert CritiqueSeverity.medium == "medium"
        assert CritiqueSeverity.low == "low"
        assert CritiqueSeverity.info == "info"


class TestFindingResponse:
    def test_minimal(self):
        f = FindingResponse(
            finding_id="f1",
            category="seo",
            severity="high",
            title="Test",
            description="Desc",
        )
        assert f.finding_id == "f1"
        assert f.url == ""
        assert f.selector is None
        assert f.check_id is None

    def test_full(self):
        f = FindingResponse(
            finding_id="f1",
            category="security",
            severity="critical",
            title="CSP Missing",
            description="No CSP header",
            url="https://example.com",
            recommendation="Add CSP",
            selector="head",
            check_id="sec-csp",
            wcag_criterion=None,
            evidence="<head>...</head>",
            source_tool="security_scanner",
            false_positive_risk="low",
            remediation_effort="medium",
        )
        assert f.source_tool == "security_scanner"
        assert f.remediation_effort == "medium"


class TestWebhookConfig:
    def test_defaults(self):
        wc = WebhookConfig()
        assert wc.events == ["completed"]
        assert wc.severity_threshold is None

    def test_custom(self):
        wc = WebhookConfig(events=["completed", "failed"], severity_threshold="high")
        assert len(wc.events) == 2
        assert wc.severity_threshold == "high"


class TestCritiqueRequest:
    def test_minimal(self):
        r = CritiqueRequest(url="https://example.com")
        assert r.url == "https://example.com"
        assert r.categories is None
        assert r.max_pages == 1
        assert r.force_rescan is False

    def test_full(self):
        r = CritiqueRequest(
            url="https://example.com",
            categories=["seo", "security"],
            max_pages=5,
            instructions="Focus on SEO",
            github_repo="https://github.com/org/repo",
            force_rescan=True,
            callback_url="https://hooks.example.com",
            tags=["v2"],
        )
        assert r.max_pages == 5
        assert r.force_rescan is True
        assert r.tags == ["v2"]

    def test_max_pages_validation(self):
        with pytest.raises(ValidationError):
            CritiqueRequest(url="https://example.com", max_pages=0)

        with pytest.raises(ValidationError):
            CritiqueRequest(url="https://example.com", max_pages=51)


class TestCritiqueJobResponse:
    def test_creation(self):
        r = CritiqueJobResponse(job_id="j1", poll_url="/api/critique/j1")
        assert r.status == "pending"


class TestCritiqueResultResponse:
    def test_minimal(self):
        r = CritiqueResultResponse(
            job_id="j1",
            status="pending",
            url="https://example.com",
        )
        assert r.findings is None
        assert r.overall_score is None

    def test_full(self):
        r = CritiqueResultResponse(
            job_id="j1",
            status="completed",
            url="https://example.com",
            findings=[
                FindingResponse(
                    finding_id="f1",
                    category="seo",
                    severity="low",
                    title="T",
                    description="D",
                )
            ],
            category_scores={"seo": 90.0},
            overall_score=90.0,
            confidence_scores={"seo": 0.95},
            summary="Good",
            scan_duration=5.0,
            pages_scanned=1,
            created_at="2024-01-01T00:00:00+00:00",
            completed_at="2024-01-01T00:00:05+00:00",
        )
        assert r.findings is not None
        assert len(r.findings) == 1
        assert r.scan_duration == 5.0


class TestCritiqueListResponse:
    def test_creation(self):
        r = CritiqueListResponse(items=[], total=0, page=1, per_page=20)
        assert r.items == []


class TestComparisonResponse:
    def test_creation(self):
        r = ComparisonResponse(
            job_id_1="j1",
            job_id_2="j2",
            url_1="https://a.com",
            url_2="https://b.com",
            score_deltas={"seo": 5.0},
            overall_delta=3.0,
            new_findings=2,
            resolved_findings=1,
            persistent_findings=5,
        )
        assert r.overall_delta == 3.0


class TestBatchSchemas:
    def test_batch_request_min(self):
        r = BatchCritiqueRequest(urls=["https://a.com"])
        assert len(r.urls) == 1
        assert r.max_pages == 1

    def test_batch_request_empty_urls(self):
        with pytest.raises(ValidationError):
            BatchCritiqueRequest(urls=[])

    def test_batch_response(self):
        r = BatchCritiqueResponse(batch_id="b1", job_ids=["j1", "j2"], total=2)
        assert r.status == "pending"

    def test_batch_status(self):
        r = BatchStatusResponse(
            batch_id="b1", status="running", total=3, completed=1, failed=0, job_ids=["j1"]
        )
        assert r.completed == 1


class TestAnalyticsSchemas:
    def test_trend_data_point(self):
        p = TrendDataPoint(job_id="j1", overall_score=85.0)
        assert p.date is None

    def test_domain_trend_response(self):
        r = DomainTrendResponse(domain="example.com", days=30)
        assert r.data_points == []

    def test_top_finding_response(self):
        f = TopFindingResponse(
            title="Missing alt", category="a11y", severity="medium", count=10, percentage=50.0
        )
        assert f.check_id is None

    def test_domain_analytics_response(self):
        r = DomainAnalyticsResponse(domain="example.com", total_scans=5)
        assert r.score_trend == "stable"

    def test_domain_summary_item(self):
        d = DomainSummaryItem(scan_count=3)
        assert d.domain is None

    def test_tenant_analytics_response(self):
        r = TenantAnalyticsResponse(
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", total_scans=10, total_domains=3
        )
        assert r.domains == []


class TestSuppressionSchemas:
    def test_create_suppression(self):
        r = CreateSuppressionRequest(domain="example.com", check_id="sec-csp", reason="FP")
        assert r.status == "suppressed"

    def test_suppression_response(self):
        r = SuppressionResponse(
            id="s1", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", status="suppressed"
        )
        assert r.domain is None

    def test_suppression_list(self):
        r = SuppressionListResponse(items=[], total=0)
        assert len(r.items) == 0


class TestScheduleSchemas:
    def test_create_schedule(self):
        r = CreateScheduleRequest(url="https://example.com")
        assert r.cron_expression == "@daily"

    def test_update_schedule(self):
        r = UpdateScheduleRequest(enabled=False)
        assert r.url is None

    def test_schedule_response(self):
        r = ScheduleResponse(
            schedule_id="s1",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://example.com",
            cron_expression="@daily",
            enabled=True,
        )
        assert r.run_count == 0

    def test_schedule_list(self):
        r = ScheduleListResponse(items=[], total=0)
        assert len(r.items) == 0


class TestScoringSchemas:
    def test_scoring_weights_response(self):
        r = ScoringWeightsResponse(weights={"critical_weight": 30})
        assert r.weights["critical_weight"] == 30

    def test_scoring_weights_update(self):
        r = ScoringWeightsUpdateRequest(weights={"critical_weight": 25})
        assert r.weights["critical_weight"] == 25

    def test_config_overrides(self):
        r = ConfigOverridesResponse(overrides={"scoring": {"critical_weight": "25"}})
        assert "scoring" in r.overrides


class TestHistoricalSchemas:
    def test_finding_change_event(self):
        e = FindingChangeEvent(
            title="CSP Missing", category="security", severity="critical", change="new"
        )
        assert e.check_id is None

    def test_historical_comparison(self):
        r = HistoricalComparisonResponse(domain="example.com", jobs_compared=3)
        assert r.new_findings == []
        assert r.resolved_findings == []

    def test_rerun_request(self):
        r = RerunRequest()
        assert r.categories is None

        r2 = RerunRequest(categories=["seo"])
        assert r2.categories == ["seo"]


class TestTagsSchema:
    def test_tags_update(self):
        r = TagsUpdateRequest(tags=["v1", "production"])
        assert len(r.tags) == 2

    def test_tags_required(self):
        with pytest.raises(ValidationError):
            TagsUpdateRequest()


class TestAcknowledgeFindingRequest:
    def test_default(self):
        r = AcknowledgeFindingRequest()
        assert r.reason is None

    def test_with_reason(self):
        r = AcknowledgeFindingRequest(reason="Known issue")
        assert r.reason == "Known issue"


class TestCritiqueReportResponse:
    def test_creation(self):
        r = CritiqueReportResponse(job_id="j1", url="https://example.com", report="# Report")
        assert r.format == "markdown"


class TestCritiqueJSONReportResponse:
    def test_creation(self):
        r = CritiqueJSONReportResponse(
            url="https://example.com",
            overall_score=85.0,
            grade="B+",
        )
        assert r.domain is None
        assert r.category_scores == {}
        assert r.summary == ""

    def test_category_score_detail(self):
        d = CategoryScoreDetail(score=90.0, grade="A")
        assert d.score == 90.0


class TestFindingsListResponse:
    def test_creation(self):
        r = FindingsListResponse(
            job_id="j1",
            findings=[],
            total=0,
            page=1,
            per_page=20,
        )
        assert r.findings == []
