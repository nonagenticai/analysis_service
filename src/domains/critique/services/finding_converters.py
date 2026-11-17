"""Finding format converters for cross-service interoperability."""

from __future__ import annotations

from pydantic import BaseModel, Field


class UnifiedFinding(BaseModel):
    """Cross-service standard finding format.

    Used for exchanging findings between critique_service,
    skill_builder analysis, and other microservices.
    """

    id: str = Field(description="Unique finding identifier")
    source_service: str = Field(description="Originating service name")
    category: str = Field(description="Finding category")
    severity: str = Field(description="Severity level")
    title: str = Field(description="Short title")
    description: str = Field(description="Detailed description")
    url: str = Field(default="", description="Related URL")
    recommendation: str = Field(default="", description="Fix suggestion")
    check_id: str | None = Field(default=None, description="Stable check identifier")
    metadata: dict = Field(default_factory=dict, description="Additional metadata")


def critique_finding_to_unified(finding: dict) -> UnifiedFinding:
    """Convert a critique_service finding dict to UnifiedFinding."""
    metadata: dict = {}
    for key in (
        "wcag_criterion",
        "evidence",
        "source_tool",
        "false_positive_risk",
        "remediation_effort",
        "selector",
    ):
        if finding.get(key):
            metadata[key] = finding[key]

    return UnifiedFinding(
        id=finding.get("finding_id", ""),
        source_service="critique_service",
        category=finding.get("category", ""),
        severity=finding.get("severity", "info"),
        title=finding.get("title", ""),
        description=finding.get("description", ""),
        url=finding.get("url", ""),
        recommendation=finding.get("recommendation", ""),
        check_id=finding.get("check_id"),
        metadata=metadata,
    )


def unified_to_critique_finding(unified: UnifiedFinding) -> dict:
    """Convert a UnifiedFinding back to critique_service finding dict."""
    import uuid

    finding: dict = {
        "finding_id": unified.id or str(uuid.uuid7()),
        "category": unified.category,
        "severity": unified.severity,
        "title": unified.title,
        "description": unified.description,
        "url": unified.url,
        "recommendation": unified.recommendation,
    }
    if unified.check_id:
        finding["check_id"] = unified.check_id
    # Extract metadata fields back to top-level
    for key in (
        "wcag_criterion",
        "evidence",
        "source_tool",
        "false_positive_risk",
        "remediation_effort",
        "selector",
    ):
        if unified.metadata.get(key):
            finding[key] = unified.metadata[key]
    return finding


def critique_finding_to_analysis_issue(finding: dict) -> dict:
    """Convert critique finding to skill_builder analysis issue format."""
    severity_map = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "info",
        "info": "info",
    }
    return {
        "type": severity_map.get(finding.get("severity", "info"), "info"),
        "category": finding.get("category", ""),
        "message": finding.get("title", ""),
        "details": finding.get("description", ""),
        "suggestion": finding.get("recommendation", ""),
        "source": "critique_service",
        "metadata": {
            "check_id": finding.get("check_id"),
            "finding_id": finding.get("finding_id"),
            "wcag_criterion": finding.get("wcag_criterion"),
        },
    }


def analysis_issue_to_critique_finding(issue: dict) -> dict:
    """Convert skill_builder analysis issue to critique finding format."""
    import uuid

    type_to_severity = {
        "error": "high",
        "warning": "medium",
        "info": "low",
    }
    metadata = issue.get("metadata", {})
    return {
        "finding_id": metadata.get("finding_id", str(uuid.uuid7())),
        "category": issue.get("category", "other"),
        "severity": type_to_severity.get(issue.get("type", "info"), "info"),
        "title": issue.get("message", ""),
        "description": issue.get("details", ""),
        "url": "",
        "recommendation": issue.get("suggestion", ""),
        "check_id": metadata.get("check_id"),
        "source_tool": issue.get("source", "external"),
    }
