"""Export findings to CSV and SARIF formats."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime


def findings_to_csv(findings: list[dict], job_url: str = "") -> str:
    """Convert findings list to CSV string."""
    output = io.StringIO()
    fieldnames = [
        "finding_id",
        "category",
        "severity",
        "title",
        "description",
        "url",
        "recommendation",
        "check_id",
        "wcag_criterion",
        "evidence",
        "source_tool",
        "false_positive_risk",
        "remediation_effort",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for f in findings:
        row = {k: f.get(k, "") for k in fieldnames}
        writer.writerow(row)
    return output.getvalue()


_SARIF_PROP_KEYS = (
    "wcag_criterion",
    "source_tool",
    "false_positive_risk",
    "remediation_effort",
    "evidence",
)


def _sarif_rules_from_findings(findings: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """Build the SARIF ``rules`` array (one entry per unique check_id)."""
    rules: list[dict] = []
    rule_index: dict[str, int] = {}
    for f in findings:
        check_id = f.get("check_id", f.get("finding_id", "unknown"))
        if check_id in rule_index:
            continue
        rule_index[check_id] = len(rules)
        rule: dict = {
            "id": check_id,
            "shortDescription": {"text": f.get("title", "")},
            "fullDescription": {"text": f.get("description", "")},
            "defaultConfiguration": {
                "level": _severity_to_sarif_level(f.get("severity", "info")),
            },
            "properties": {"category": f.get("category", "")},
        }
        if f.get("recommendation"):
            rule["help"] = {"text": f["recommendation"]}
        rules.append(rule)
    return rules, rule_index


def _sarif_result_from_finding(f: dict, rule_index: dict[str, int], job_url: str) -> dict:
    """Build a single SARIF ``results[]`` entry for a finding."""
    check_id = f.get("check_id", f.get("finding_id", "unknown"))
    result: dict = {
        "ruleId": check_id,
        "ruleIndex": rule_index.get(check_id, 0),
        "level": _severity_to_sarif_level(f.get("severity", "info")),
        "message": {"text": f.get("description", "")},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": f.get("url") or job_url},
                },
            },
        ],
    }
    props = {k: f[k] for k in _SARIF_PROP_KEYS if f.get(k)}
    if props:
        result["properties"] = props
    return result


def findings_to_sarif(
    findings: list[dict],
    job_url: str = "",
    tool_versions: dict[str, str] | None = None,
) -> dict:
    """Convert findings to SARIF 2.1.0 JSON format.

    SARIF (Static Analysis Results Interchange Format) is an OASIS standard
    for expressing static analysis results.
    """
    tool_versions = tool_versions or {}

    rules, rule_index = _sarif_rules_from_findings(findings)
    results = [_sarif_result_from_finding(f, rule_index, job_url) for f in findings]

    sarif: dict = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/sarif-2.1/schema/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "CritiqueService",
                        "version": tool_versions.get("critique_service", "0.1.0"),
                        "informationUri": "https://github.com/nonagenticai/critique-service",
                        "rules": rules,
                    },
                },
                "results": results,
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "endTimeUtc": datetime.now(UTC).isoformat(),
                    },
                ],
                "artifacts": [
                    {
                        "location": {"uri": job_url},
                        "roles": ["analysisTarget"],
                    },
                ]
                if job_url
                else [],
            },
        ],
    }

    return sarif


def _severity_to_sarif_level(severity: str) -> str:
    """Map critique severity to SARIF level."""
    mapping = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "note",
        "info": "note",
    }
    return mapping.get(severity, "note")
