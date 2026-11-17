"""Unit tests for report generator."""

import uuid

import pytest

from src.domains.critique.services.report_generator import (
    compare_findings,
    compute_category_score,
    compute_confidence_scores,
    compute_overall_score,
    compute_scores,
    deduplicate_findings,
    generate_markdown_report,
)


def _make_finding(
    category: str = "seo",
    severity: str = "medium",
    title: str = "Test finding",
    url: str = "https://example.com",
) -> dict:
    return {
        "finding_id": str(uuid.uuid7()),
        "category": category,
        "severity": severity,
        "title": title,
        "description": "Test description",
        "url": url,
        "recommendation": "Fix it",
        "selector": None,
    }


class TestDeduplication:
    def test_removes_duplicates(self) -> None:
        findings = [
            _make_finding(title="Dup", url="https://a.com"),
            _make_finding(title="Dup", url="https://a.com"),
            _make_finding(title="Unique", url="https://a.com"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_different_urls_not_deduped(self) -> None:
        findings = [
            _make_finding(title="Same", url="https://a.com"),
            _make_finding(title="Same", url="https://b.com"),
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 2

    def test_empty_input(self) -> None:
        assert deduplicate_findings([]) == []

    def test_check_id_dedup(self) -> None:
        findings = [
            {**_make_finding(title="A"), "check_id": "seo-001", "source_tool": "seo_scanner"},
            {**_make_finding(title="B"), "check_id": "seo-001", "source_tool": "a11y_scanner"},
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1

    def test_cross_scanner_equivalent_checks(self) -> None:
        from src.domains.critique.services.report_generator import EQUIVALENT_CHECKS

        for src_id, dst_id in EQUIVALENT_CHECKS.items():
            findings = [
                {**_make_finding(), "check_id": src_id, "source_tool": "seo_scanner"},
                {**_make_finding(), "check_id": dst_id, "source_tool": "a11y_scanner"},
            ]
            result = deduplicate_findings(findings)
            assert len(result) == 1, f"Expected dedup for {src_id} <-> {dst_id}"

    def test_severity_promotion(self) -> None:
        findings = [
            {
                **_make_finding(severity="low"),
                "check_id": "test-001",
                "source_tool": "scanner_a",
            },
            {
                **_make_finding(severity="high"),
                "check_id": "test-001",
                "source_tool": "scanner_b",
            },
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["severity"] == "high"

    def test_scanner_count_annotation(self) -> None:
        findings = [
            {**_make_finding(), "check_id": "x-001", "source_tool": "scanner_a"},
            {**_make_finding(), "check_id": "x-001", "source_tool": "scanner_b"},
            {**_make_finding(), "check_id": "x-001", "source_tool": "scanner_c"},
        ]
        result = deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["scanner_count"] == 3
        assert sorted(result[0]["confirmed_by"]) == ["scanner_a", "scanner_b", "scanner_c"]

    def test_single_finding_gets_annotations(self) -> None:
        findings = [
            {**_make_finding(), "check_id": "solo-001", "source_tool": "seo_scanner"},
        ]
        result = deduplicate_findings(findings)
        assert result[0]["scanner_count"] == 1
        assert result[0]["confirmed_by"] == ["seo_scanner"]


class TestScoring:
    def test_no_findings_scores_100(self) -> None:
        score = compute_category_score([], "seo")
        assert score == 100.0

    def test_critical_findings_lower_score(self) -> None:
        findings = [_make_finding(severity="critical")]
        score = compute_category_score(findings, "seo")
        assert 50.0 < score < 100.0

    def test_score_minimum_is_50(self) -> None:
        findings = [_make_finding(severity="critical") for _ in range(20)]
        score = compute_category_score(findings, "seo")
        assert score >= 50.0

    def test_scores_spread_by_severity(self) -> None:
        high_findings = [_make_finding(severity="high")]
        crit_findings = [_make_finding(severity="critical")]
        high_score = compute_category_score(high_findings, "seo")
        crit_score = compute_category_score(crit_findings, "seo")
        assert high_score > crit_score > 50.0

    def test_info_findings_dont_affect_score(self) -> None:
        findings = [_make_finding(severity="info") for _ in range(10)]
        score = compute_category_score(findings, "seo")
        assert score == 100.0

    def test_compute_scores_all_categories(self) -> None:
        findings = [
            _make_finding(category="seo", severity="high"),
            _make_finding(category="security", severity="critical"),
            _make_finding(category="accessibility", severity="low"),
        ]
        scores = compute_scores(findings)
        assert "seo" in scores
        assert "security" in scores
        assert "accessibility" in scores
        assert scores["security"] < scores["seo"]  # critical is worse than high


class TestMarkdownReport:
    def test_report_contains_url(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 85.0},
            summary="Test summary",
        )
        assert "https://example.com" in report

    def test_report_contains_summary(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[],
            scores={},
            summary="Everything looks good",
        )
        assert "Everything looks good" in report

    def test_report_contains_scores(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[_make_finding()],
            scores={"seo": 75.0, "security": 90.0},
            summary="Mixed results",
        )
        assert "75.0" in report
        assert "90.0" in report

    def test_report_contains_findings(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[_make_finding(title="Missing title tag")],
            scores={"seo": 85.0},
            summary="One issue found",
        )
        assert "Missing title tag" in report

    def test_report_with_github_repo(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[],
            scores={},
            summary="Clean",
            github_repo="https://github.com/user/repo",
        )
        assert "https://github.com/user/repo" in report


class TestOverallScore:
    def test_compute_overall_score_weighted(self) -> None:
        scores = {
            "accessibility": 80.0,
            "security": 90.0,
            "performance": 70.0,
            "seo": 100.0,
            "ux": 60.0,
        }
        overall = compute_overall_score(scores)
        # Weighted: a11y(25%)*80 + sec(25%)*90 + perf(20%)*70 + seo(15%)*100 + ux(15%)*60
        # = 20 + 22.5 + 14 + 15 + 9 = 80.5
        assert 79.0 <= overall <= 82.0

    def test_compute_overall_score_empty(self) -> None:
        assert compute_overall_score({}) == 0.0

    def test_compute_overall_score_partial(self) -> None:
        scores = {"seo": 80.0, "security": 90.0}
        overall = compute_overall_score(scores)
        assert 80.0 <= overall <= 90.0

    def test_compute_overall_score_single(self) -> None:
        overall = compute_overall_score({"security": 75.0})
        assert overall == 75.0


class TestConfidenceScores:
    def test_basic_confidence(self) -> None:
        findings = [_make_finding(category="seo", severity="high")]
        confidence = compute_confidence_scores(findings, pages_scanned=1, max_pages=1)
        assert "seo" in confidence
        assert 0.0 <= confidence["seo"] <= 1.0

    def test_ai_findings_lower_confidence(self) -> None:
        findings_no_ai = [_make_finding(category="seo")]
        findings_ai = [{**_make_finding(category="ux"), "false_positive_risk": "medium"}]
        conf_no_ai = compute_confidence_scores(findings_no_ai)
        conf_ai = compute_confidence_scores(findings_ai)
        assert conf_no_ai.get("seo", 0) >= conf_ai.get("ux", 0)

    def test_empty_findings(self) -> None:
        confidence = compute_confidence_scores([])
        assert confidence == {}


class TestGradeSystem:
    def test_grade_a_plus(self) -> None:
        from src.domains.critique.services.report_generator import _score_emoji

        assert _score_emoji(95) == "A+"

    def test_grade_a(self) -> None:
        from src.domains.critique.services.report_generator import _score_emoji

        assert _score_emoji(90) == "A"

    def test_grade_b(self) -> None:
        from src.domains.critique.services.report_generator import _score_emoji

        assert _score_emoji(80) == "B"

    def test_grade_c(self) -> None:
        from src.domains.critique.services.report_generator import _score_emoji

        assert _score_emoji(70) == "C"

    def test_grade_d(self) -> None:
        from src.domains.critique.services.report_generator import _score_emoji

        assert _score_emoji(60) == "D"

    def test_grade_f(self) -> None:
        from src.domains.critique.services.report_generator import _score_emoji

        assert _score_emoji(50) == "F"


class TestCompareFindings:
    def test_new_findings(self) -> None:
        old = [_make_finding(title="Old issue", category="seo")]
        new = [
            _make_finding(title="Old issue", category="seo"),
            _make_finding(title="New issue", category="seo"),
        ]
        new_f, resolved_f, persistent = compare_findings(old, new)
        assert len(new_f) == 1
        assert new_f[0]["title"] == "New issue"
        assert len(resolved_f) == 0
        assert persistent == 1

    def test_resolved_findings(self) -> None:
        old = [
            _make_finding(title="Issue A", category="seo"),
            _make_finding(title="Issue B", category="seo"),
        ]
        new = [_make_finding(title="Issue A", category="seo")]
        new_f, resolved_f, persistent = compare_findings(old, new)
        assert len(new_f) == 0
        assert len(resolved_f) == 1
        assert resolved_f[0]["title"] == "Issue B"
        assert persistent == 1

    def test_check_id_matching(self) -> None:
        old = [{**_make_finding(title="X"), "check_id": "seo-001"}]
        new = [{**_make_finding(title="Y"), "check_id": "seo-001"}]
        new_f, resolved_f, persistent = compare_findings(old, new)
        assert persistent == 1
        assert len(new_f) == 0
        assert len(resolved_f) == 0


class TestComputeScoreDeltas:
    def test_after_minus_before_rounded(self) -> None:
        from src.domains.critique.services.report_generator import compute_score_deltas

        deltas = compute_score_deltas(
            {"seo": 50.0, "accessibility": 70.0}, {"seo": 90.0, "accessibility": 70.0}
        )
        assert deltas == {"seo": 40.0, "accessibility": 0.0}

    def test_missing_side_counts_as_zero(self) -> None:
        from src.domains.critique.services.report_generator import compute_score_deltas

        # 'seo' only after → full after value; 'perf' only before → negative.
        deltas = compute_score_deltas({"perf": 80.0}, {"seo": 60.0})
        assert deltas == {"seo": 60.0, "perf": -80.0}

    def test_empty_inputs(self) -> None:
        from src.domains.critique.services.report_generator import compute_score_deltas

        assert compute_score_deltas({}, {}) == {}


class TestExtractDomain:
    def test_extract_domain_simple(self) -> None:
        from src.domains.critique.services.report_generator import _extract_domain

        assert _extract_domain("https://example.com/page") == "example.com"

    def test_extract_domain_www(self) -> None:
        from src.domains.critique.services.report_generator import _extract_domain

        assert _extract_domain("https://www.example.com/page") == "example.com"

    def test_extract_domain_with_port(self) -> None:
        from src.domains.critique.services.report_generator import _extract_domain

        result = _extract_domain("https://example.com:8080/page")
        assert "example.com" in result

    def test_extract_domain_subdomain(self) -> None:
        from src.domains.critique.services.report_generator import _extract_domain

        assert _extract_domain("https://sub.example.co.uk/path") == "sub.example.co.uk"

    def test_extract_domain_invalid(self) -> None:
        from src.domains.critique.services.report_generator import _extract_domain

        result = _extract_domain("not-a-url")
        assert isinstance(result, str)


class TestSeverityOrder:
    def test_known_severities(self) -> None:
        from src.domains.critique.services.report_generator import _severity_order

        assert _severity_order("critical") == 0
        assert _severity_order("high") == 1
        assert _severity_order("medium") == 2
        assert _severity_order("low") == 3
        assert _severity_order("info") == 4

    def test_unknown_severity(self) -> None:
        from src.domains.critique.services.report_generator import _severity_order

        assert _severity_order("unknown") == 5


class TestGetSeverityWeights:
    def test_default_weights(self) -> None:
        from src.domains.critique.services.report_generator import _get_severity_weights

        weights = _get_severity_weights()
        assert "critical" in weights
        assert "info" in weights
        assert weights["critical"] > weights["high"] > weights["medium"]

    def test_fallback_on_error(self) -> None:
        from unittest.mock import patch

        from src.domains.critique.services.report_generator import _get_severity_weights

        with patch(
            "src.domains.critique.services.report_generator.get_settings", side_effect=RuntimeError
        ):
            weights = _get_severity_weights()
            assert weights["critical"] == 30

    def test_get_decay_k_default(self) -> None:
        from src.domains.critique.services.report_generator import _get_decay_k

        k = _get_decay_k()
        assert k > 0

    def test_get_decay_k_fallback(self) -> None:
        from unittest.mock import patch

        from src.domains.critique.services.report_generator import _get_decay_k

        with patch(
            "src.domains.critique.services.report_generator.get_settings", side_effect=RuntimeError
        ):
            assert _get_decay_k() == 25


class TestBasicSummary:
    def test_with_critical(self) -> None:
        from src.domains.critique.services.report_generator import _basic_summary

        findings = [{"severity": "critical"}, {"severity": "high"}]
        result = _basic_summary(findings, "https://test.com", {"security": 50.0})
        assert "critical" in result.lower()
        assert "high" in result.lower()

    def test_weakest_area(self) -> None:
        from src.domains.critique.services.report_generator import _basic_summary

        findings = [{"severity": "info"}]
        scores = {"seo": 60.0, "security": 90.0}
        result = _basic_summary(findings, "https://test.com", scores)
        assert "seo" in result.lower()

    def test_empty_scores(self) -> None:
        from src.domains.critique.services.report_generator import _basic_summary

        result = _basic_summary([], "https://test.com", {})
        assert "test.com" in result


class TestFormatFindingsForSummary:
    def test_critical_high_only(self) -> None:
        from src.domains.critique.services.report_generator import _format_findings_for_summary

        findings = [
            {"severity": "critical", "title": "Crit Issue", "description": "Bad"},
            {"severity": "info", "title": "Info Thing", "description": "Meh"},
        ]
        result = _format_findings_for_summary(findings)
        assert "CRITICAL" in result
        assert "Info Thing" not in result

    def test_truncation_at_15(self) -> None:
        from src.domains.critique.services.report_generator import _format_findings_for_summary

        findings = [
            {"severity": "high", "title": f"Issue {i}", "description": "x" * 200} for i in range(20)
        ]
        result = _format_findings_for_summary(findings)
        assert "... and 5 more" in result


class TestGenerateSummaryAsync:
    async def test_wm_success_returns_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.domains.critique.services.report_generator import generate_summary
        from tests.llm_test_doubles import FakeWMClient, install_wm_factory

        wm = FakeWMClient(outputs=["A clear human-written summary."])
        install_wm_factory(monkeypatch, lambda: wm)

        result, token_usage = await generate_summary(
            [{"severity": "high", "category": "seo"}],
            "https://example.com",
            {"seo": 70.0},
        )
        assert result == "Overall score: 70.0/100 (SEO 70.0).\n\nA clear human-written summary."
        assert token_usage is not None
        assert wm.calls[0].worker_name == "critique-summary"

    async def test_wm_error_falls_back_to_basic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.domains.critique.services.report_generator import generate_summary
        from tests.llm_test_doubles import FakeWMClient, install_wm_factory

        wm = FakeWMClient(raise_after="always", error=RuntimeError("fail"))
        install_wm_factory(monkeypatch, lambda: wm)

        result, token_usage = await generate_summary(
            [{"severity": "critical"}],
            "https://example.com",
            {"seo": 50.0},
        )
        assert "example.com" in result
        assert token_usage is None


class TestMarkdownReportExtended:
    def test_scan_duration(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[],
            scores={},
            summary="Fast",
            scan_duration=2.5,
        )
        assert "2.5s" in report

    def test_finding_with_recommendation(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[
                {
                    "category": "seo",
                    "severity": "high",
                    "title": "Missing title",
                    "description": "No title",
                    "recommendation": "Add title tag",
                    "url": "https://example.com",
                }
            ],
            scores={"seo": 70.0},
            summary="Issue found",
        )
        assert "Add title tag" in report
        assert "**Recommendation:**" in report

    def test_overall_score_in_report(self) -> None:
        report = generate_markdown_report(
            url="https://example.com",
            findings=[],
            scores={"seo": 80.0, "security": 90.0},
            summary="Good",
        )
        assert "Overall Score" in report

    def test_compute_overall_score_fallback(self) -> None:
        from unittest.mock import patch

        with patch(
            "src.domains.critique.services.report_generator.get_settings", side_effect=RuntimeError
        ):
            result = compute_overall_score({"seo": 80.0})
            assert result > 0
