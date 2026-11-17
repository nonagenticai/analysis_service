"""Tests for src.services.findings -- extract structured findings from review results."""

from src.domains.review.services.findings import (
    _deduplicate_findings,
    _generate_finding_id,
    _remediation_effort_from_severity,
    _severity_from_score,
    extract_findings,
)


class TestSeverityFromScore:
    def test_critical(self):
        assert _severity_from_score(3.0) == "critical"

    def test_high(self):
        assert _severity_from_score(5.0) == "high"

    def test_medium(self):
        assert _severity_from_score(6.5) == "medium"

    def test_low(self):
        assert _severity_from_score(7.5) == "low"

    def test_info(self):
        assert _severity_from_score(9.0) == "info"


class TestGenerateFindingId:
    def test_deterministic(self):
        id1 = _generate_finding_id("design_review", "layout", "test desc")
        id2 = _generate_finding_id("design_review", "layout", "test desc")
        assert id1 == id2

    def test_different_inputs_different_ids(self):
        id1 = _generate_finding_id("design_review", "layout", "desc A")
        id2 = _generate_finding_id("design_review", "layout", "desc B")
        assert id1 != id2

    def test_returns_16_char_hex(self):
        fid = _generate_finding_id("a", "b", "c")
        assert len(fid) == 16
        int(fid, 16)  # should not raise


class TestRemediationEffort:
    def test_critical_maps_to_high(self):
        assert _remediation_effort_from_severity("critical") == "high"

    def test_high_maps_to_high(self):
        assert _remediation_effort_from_severity("high") == "high"

    def test_medium_maps_to_medium(self):
        assert _remediation_effort_from_severity("medium") == "medium"

    def test_low_maps_to_low(self):
        assert _remediation_effort_from_severity("low") == "low"

    def test_info_maps_to_low(self):
        assert _remediation_effort_from_severity("info") == "low"

    def test_unknown_maps_to_unknown(self):
        assert _remediation_effort_from_severity("bogus") == "unknown"


class TestExtractFindings:
    def test_empty_results(self):
        assert extract_findings({}) == []

    def test_skips_error_results(self):
        results = {"design_review": {"error": "timeout"}}
        assert extract_findings(results) == []

    def test_extracts_category_scores(self):
        results = {
            "design_review": {
                "overall_score": 7.0,
                "categories": {
                    "layout_consistency": {"score": 8, "observations": ["good"]},
                    "whitespace_usage": {"score": 3, "observations": ["bad"]},
                },
                "strengths": [],
                "weaknesses": [],
            }
        }
        findings = extract_findings(results)
        cats = [f for f in findings if f["source"] == "category_score"]
        assert len(cats) == 2
        # Severity ordering: critical first
        assert findings[0]["severity"] == "critical"

    def test_category_findings_have_finding_id(self):
        results = {
            "design_review": {
                "categories": {
                    "layout": {"score": 5, "observations": ["needs work"]},
                },
            }
        }
        findings = extract_findings(results)
        assert len(findings) == 1
        assert findings[0]["finding_id"]
        assert len(findings[0]["finding_id"]) == 16

    def test_category_findings_have_evidence(self):
        results = {
            "design_review": {
                "categories": {
                    "layout": {"score": 5, "observations": ["needs work", "inconsistent"]},
                },
            }
        }
        findings = extract_findings(results)
        assert findings[0]["evidence"] == ["needs work", "inconsistent"]

    def test_category_findings_have_remediation_effort(self):
        results = {
            "design_review": {
                "categories": {
                    "layout": {"score": 3},  # critical -> high effort
                },
            }
        }
        findings = extract_findings(results)
        assert findings[0]["remediation_effort"] == "high"

    def test_category_findings_have_source_data(self):
        results = {
            "design_review": {
                "categories": {
                    "layout": {"score": 7, "observations": ["ok"]},
                },
            }
        }
        findings = extract_findings(results)
        assert findings[0]["source_data"] == {"score": 7, "observations": ["ok"]}

    def test_extracts_strengths_as_info(self):
        results = {
            "design_review": {
                "strengths": ["Great typography", "Clean layout"],
            }
        }
        findings = extract_findings(results)
        strengths = [f for f in findings if f["source"] == "strength"]
        assert len(strengths) == 2
        assert all(f["severity"] == "info" for f in strengths)

    def test_text_findings_have_finding_id(self):
        results = {
            "design_review": {
                "strengths": ["Great typography"],
            }
        }
        findings = extract_findings(results)
        assert findings[0]["finding_id"]
        assert findings[0]["remediation_effort"] == "low"  # info -> low

    def test_extracts_weaknesses_as_medium(self):
        results = {
            "design_review": {
                "weaknesses": ["Poor contrast"],
            }
        }
        findings = extract_findings(results)
        weak = [f for f in findings if f["source"] == "weaknesses"]
        assert len(weak) == 1
        assert weak[0]["severity"] == "medium"

    def test_extracts_recommendations_as_low(self):
        results = {
            "usability_review": {
                "recommendations": ["Improve nav"],
                "quick_wins": ["Add alt text"],
            }
        }
        findings = extract_findings(results)
        recs = [f for f in findings if f["source"] in ("recommendations", "quick_wins")]
        assert len(recs) == 2
        assert all(f["severity"] == "low" for f in recs)

    def test_extracts_dict_items_by_description(self):
        results = {
            "feature_review": {
                "missing_features": [
                    {"description": "Search functionality", "importance": "high"},
                ],
            }
        }
        findings = extract_findings(results)
        missing = [f for f in findings if f["source"] == "missing_feature"]
        assert len(missing) == 1
        assert missing[0]["description"] == "Search functionality"
        assert missing[0]["source_data"] == {
            "description": "Search functionality",
            "importance": "high",
        }

    def test_sorted_by_severity(self):
        results = {
            "design_review": {
                "strengths": ["Good"],
                "weaknesses": ["Bad"],
                "critical_issues": [{"description": "Very bad"}],
                "recommendations": ["Fix it"],
            }
        }
        findings = extract_findings(results)
        severities = [f["severity"] for f in findings]
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        assert severities == sorted(severities, key=lambda s: order.get(s, 99))


class TestDeduplicateFindings:
    def test_dedup_keeps_highest_severity(self):
        findings: list[dict[str, object]] = [
            {"finding_id": "abc", "severity": "low", "description": "dup"},
            {"finding_id": "abc", "severity": "high", "description": "dup"},
        ]
        result = _deduplicate_findings(findings)
        assert len(result) == 1
        assert result[0]["severity"] == "high"

    def test_dedup_no_finding_id_kept(self):
        findings: list[dict[str, object]] = [
            {"severity": "low", "description": "no id"},
            {"severity": "medium", "description": "no id either"},
        ]
        result = _deduplicate_findings(findings)
        assert len(result) == 2

    def test_dedup_different_ids_kept(self):
        findings: list[dict[str, object]] = [
            {"finding_id": "aaa", "severity": "low", "description": "first"},
            {"finding_id": "bbb", "severity": "low", "description": "second"},
        ]
        result = _deduplicate_findings(findings)
        assert len(result) == 2
