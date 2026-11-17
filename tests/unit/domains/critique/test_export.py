"""Tests for export_service and finding_converters."""

from src.domains.critique.services.export_service import findings_to_csv, findings_to_sarif
from src.domains.critique.services.finding_converters import (
    UnifiedFinding,
    analysis_issue_to_critique_finding,
    critique_finding_to_analysis_issue,
    critique_finding_to_unified,
    unified_to_critique_finding,
)


class TestExportCSV:
    def test_csv_empty(self):
        result = findings_to_csv([])
        assert "finding_id" in result  # Header row
        lines = result.strip().split("\n")
        assert len(lines) == 1  # Header only

    def test_csv_with_findings(self, sample_findings):
        result = findings_to_csv(sample_findings, "https://example.com")
        lines = result.strip().split("\n")
        assert len(lines) == len(sample_findings) + 1  # Header + rows
        assert "seo" in lines[1]

    def test_csv_special_characters(self):
        findings = [
            {
                "finding_id": "test-1",
                "category": "seo",
                "severity": "low",
                "title": 'Title with "quotes" and, commas',
                "description": "Description\nwith newline",
            }
        ]
        result = findings_to_csv(findings)
        assert "test-1" in result


class TestExportSARIF:
    def test_sarif_structure(self, sample_findings):
        sarif = findings_to_sarif(sample_findings, "https://example.com")
        assert sarif["version"] == "2.1.0"
        assert "$schema" in sarif
        assert len(sarif["runs"]) == 1
        assert len(sarif["runs"][0]["results"]) == len(sample_findings)

    def test_sarif_empty(self):
        sarif = findings_to_sarif([])
        assert sarif["runs"][0]["results"] == []

    def test_sarif_severity_mapping(self):
        findings = [
            {
                "finding_id": "1",
                "category": "security",
                "severity": "critical",
                "title": "CSP",
                "description": "Missing CSP",
                "check_id": "sec-csp",
            },
            {
                "finding_id": "2",
                "category": "seo",
                "severity": "low",
                "title": "SEO",
                "description": "SEO issue",
                "check_id": "seo-test",
            },
        ]
        sarif = findings_to_sarif(findings)
        results = sarif["runs"][0]["results"]
        assert results[0]["level"] == "error"
        assert results[1]["level"] == "note"


class TestFindingConverters:
    def test_critique_to_unified(self, sample_findings):
        unified = critique_finding_to_unified(sample_findings[0])
        assert isinstance(unified, UnifiedFinding)
        assert unified.source_service == "critique_service"
        assert unified.category == "seo"

    def test_unified_to_critique(self):
        unified = UnifiedFinding(
            id="test-id",
            source_service="critique_service",
            category="security",
            severity="high",
            title="Test",
            description="Test desc",
        )
        finding = unified_to_critique_finding(unified)
        assert finding["finding_id"] == "test-id"
        assert finding["category"] == "security"

    def test_roundtrip(self, sample_findings):
        original = sample_findings[0]
        unified = critique_finding_to_unified(original)
        back = unified_to_critique_finding(unified)
        assert back["category"] == original["category"]
        assert back["severity"] == original["severity"]
        assert back["title"] == original["title"]

    def test_critique_to_analysis_issue(self, sample_findings):
        issue = critique_finding_to_analysis_issue(sample_findings[1])
        assert issue["type"] == "error"  # critical -> error
        assert issue["source"] == "critique_service"

    def test_analysis_issue_to_critique(self):
        issue = {
            "type": "warning",
            "category": "code_quality",
            "message": "Unused variable",
            "details": "Variable x is unused",
            "suggestion": "Remove x",
            "source": "linter",
            "metadata": {"check_id": "unused-var"},
        }
        finding = analysis_issue_to_critique_finding(issue)
        assert finding["severity"] == "medium"
        assert finding["check_id"] == "unused-var"
