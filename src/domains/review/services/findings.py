"""Extract structured findings from review JSONB results.

Iterates all review types and normalises category scores, strengths,
weaknesses, issues, recommendations, missing features, and proposals
into a flat list of finding dicts with a consistent severity level.
"""

from __future__ import annotations

import hashlib
import logging

from src.domains.review.services.constants import (
    REMEDIATION_EFFORT_MAP,
    SEVERITY_ORDER,
    SEVERITY_SCORE_THRESHOLDS,
)
from src.domains.review.services.text_utils import extract_item_text

logger = logging.getLogger(__name__)


def _severity_from_score(score: float) -> str:
    """Map a numeric score (0-10) to a severity label.

    Thresholds:
        < 4  -> critical
        < 6  -> high
        < 7  -> medium
        < 8  -> low
        >= 8 -> info
    """
    for threshold, severity in SEVERITY_SCORE_THRESHOLDS:
        if score < threshold:
            return severity
    return "info"


def _generate_finding_id(review_type: str, category: str, description: str) -> str:
    """Generate a deterministic finding ID from review_type + category + description."""
    key = f"{review_type}:{category}:{description}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _remediation_effort_from_severity(severity: str) -> str:
    """Infer remediation effort from severity level."""
    return REMEDIATION_EFFORT_MAP.get(severity, "unknown")


def extract_findings(results: dict[str, dict]) -> list[dict]:
    """Extract a flat list of structured findings from all review results.

    Each finding is a dict with:
        - review_type: str
        - category: str
        - severity: str   (critical | high | medium | low | info)
        - description: str
        - source: str      (e.g. "category_score", "strength", "weakness", ...)
        - finding_id: str  (deterministic hash for dedup)
        - evidence: list[str]  (observations from category data)
        - remediation_effort: str  (low | medium | high)
        - source_data: dict  (raw category data for drill-down)

    Args:
        results: Mapping of review_type -> review result dict (JSONB).

    Returns:
        List of finding dicts sorted by severity (most severe first).
    """
    findings: list[dict] = []

    for review_type, result in results.items():
        if not isinstance(result, dict) or "error" in result:
            continue

        # --- Category scores as findings ---
        categories = result.get("categories", result.get("category_scores", {}))
        if isinstance(categories, dict):
            for cat_name, cat_data in categories.items():
                score = _extract_score(cat_data)
                if score is not None:
                    severity = _severity_from_score(score)
                    description = f"{cat_name}: {score}/10"
                    evidence = _extract_evidence(cat_data)
                    source_data = cat_data if isinstance(cat_data, dict) else {"score": cat_data}
                    findings.append(
                        {
                            "review_type": review_type,
                            "category": cat_name,
                            "severity": severity,
                            "score": score,
                            "description": description,
                            "source": "category_score",
                            "finding_id": _generate_finding_id(review_type, cat_name, description),
                            "evidence": evidence,
                            "remediation_effort": _remediation_effort_from_severity(severity),
                            "source_data": source_data,
                        }
                    )

        # --- Strengths -> info severity ---
        _collect_text_items(
            findings,
            result.get("strengths", []),
            review_type=review_type,
            severity="info",
            source="strength",
        )

        # --- Weaknesses / critical_issues / issues -> medium/high severity ---
        for key, severity in (
            ("critical_issues", "high"),
            ("weaknesses", "medium"),
            ("issues", "medium"),
        ):
            _collect_text_items(
                findings,
                result.get(key, []),
                review_type=review_type,
                severity=severity,
                source=key,
            )

        # --- Recommendations / quick_wins / improvements -> low severity ---
        for key in ("recommendations", "quick_wins", "improvements", "long_term_improvements"):
            _collect_text_items(
                findings,
                result.get(key, []),
                review_type=review_type,
                severity="low",
                source=key,
            )

        # --- Missing features -> medium severity ---
        _collect_text_items(
            findings,
            result.get("missing_features", []),
            review_type=review_type,
            severity="medium",
            source="missing_feature",
        )

        # --- Proposals from feature_suggestions -> low severity ---
        suggestions = result.get("feature_suggestions", result.get("proposals", []))
        _collect_text_items(
            findings,
            suggestions,
            review_type=review_type,
            severity="low",
            source="proposal",
        )

    # Deduplicate by finding_id, keeping highest severity
    findings = _deduplicate_findings(findings)

    # Sort most severe first, preserving insertion order within same severity
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 99))
    return findings


def _deduplicate_findings(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate findings by finding_id, keeping the highest severity version."""
    seen: dict[str | int, dict[str, object]] = {}
    for f in findings:
        raw_fid = f.get("finding_id", "")
        if not raw_fid:
            # No finding_id, keep as-is
            seen[id(f)] = f
            continue
        fid = str(raw_fid)
        if fid not in seen:
            seen[fid] = f
        else:
            existing = seen[fid]
            existing_sev = SEVERITY_ORDER.get(str(existing["severity"]), 99)
            new_sev = SEVERITY_ORDER.get(str(f["severity"]), 99)
            if new_sev < existing_sev:
                seen[fid] = f
    return list(seen.values())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_score(value: object) -> float | None:
    """Try to pull a numeric score from a value (dict or scalar)."""
    if isinstance(value, dict):
        score_dict: dict[str, object] = {str(k): v for k, v in value.items()}
        raw = score_dict.get("score")
    else:
        raw = value
    if raw is None:
        return None
    try:
        return float(str(raw))
    except ValueError, TypeError:
        return None


def _extract_evidence(cat_data: object) -> list[str]:
    """Extract evidence/observations from category data."""
    if not isinstance(cat_data, dict):
        return []
    data: dict[str, object] = {str(k): v for k, v in cat_data.items()}
    evidence: list[str] = []
    for key in ("observations", "evidence", "notes", "details"):
        items = data.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, str) and item.strip():
                    evidence.append(item.strip())
        elif isinstance(items, str) and items.strip():
            evidence.append(items.strip())
    return evidence


def _collect_text_items(
    findings: list[dict],
    items: list | None,
    *,
    review_type: str,
    severity: str,
    source: str,
) -> None:
    """Append text-based findings from a list of strings or dicts."""
    if not isinstance(items, list):
        return
    for item in items:
        description = ""
        source_data: dict = {}
        if isinstance(item, str):
            description = item
        elif isinstance(item, dict):
            description = extract_item_text(item)
            # extract_item_text falls back to str(item) -- treat as empty
            if description == str(item):
                description = ""
            source_data = item
        if description:
            findings.append(
                {
                    "review_type": review_type,
                    "category": source,
                    "severity": severity,
                    "description": description,
                    "source": source,
                    "finding_id": _generate_finding_id(review_type, source, description),
                    "evidence": [],
                    "remediation_effort": _remediation_effort_from_severity(severity),
                    "source_data": source_data,
                }
            )
