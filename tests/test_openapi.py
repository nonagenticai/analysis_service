"""Guardrail tests for the OpenAPI surface of analysis_service.

Locks in the org compliance standard:

- every route has an explicit ``operation_id="…"`` per
  ``OPERATION_ID_PLAN.md`` (no reliance on the
  ``generate_unique_id_function`` fallback — that is defence-in-depth only);
- the standard OpenAPI metadata (BearerAuth security scheme, openapi_tags
  grouping by domain) is present;
- protected ``/api/...`` paths advertise the security requirement.

If a new route lands, update ``OPERATION_ID_PLAN.md`` *and* the
``EXPECTED_OPERATION_IDS`` set below — they should move together. Drift is
loud on purpose (see [[feedback-false-merge-pattern-watch-PR-titles]]).
"""

from __future__ import annotations

import pytest

# The asgi module imports settings at top-level which requires env vars from
# conftest.py to be set first. pytest imports conftest before test modules,
# so by the time we run, the env is configured.
from src.asgi import app

# Source of truth — keep in sync with OPERATION_ID_PLAN.md.
EXPECTED_OPERATION_IDS: frozenset[str] = frozenset(
    {
        # Root + health
        "service_root",
        "health_check",
        "health_live",
        "health_ready",
        "health_startup",
        "health.live",
        "health.ready",
        "prometheus_metrics",
        # analyze
        "start_analysis",
        "list_analysis_jobs",
        "get_analysis",
        "cancel_analysis",
        "get_analysis_artifacts",
        "classify_pages",
        "extract_narrative",
        "start_analyze_subreview",
        "get_analyze_subreview",
        # brand
        "extract_brand",
        "extract_brand_from_url",
        "batch_extract_brand",
        "download_brand_logo",
        "get_brand_logo",
        "delete_brand_logo",
        # icp
        "analyze_icp_from_url",
        # artifact-score
        "score_artifact",
        "score_delta",
        # cro
        "score_cro",
        # seo-audit
        "audit_seo",
        # marketing scorecard
        "score_marketing",
        # critique
        "get_critique_analytics_trends",
        "get_critique_domain_analytics",
        "get_critique_tenant_summary",
        "get_critique_top_findings",
        "create_critique_batch",
        "get_critique_batch_status",
        "compare_critiques",
        "list_critique_compare_history",
        "export_critique_csv",
        "export_critique_sarif",
        "get_critique_report",
        "get_critique_report_pdf",
        "get_critique_unified_findings",
        "acknowledge_critique_finding",
        "get_critique_findings",
        "create_critique_schedule",
        "list_critique_schedules",
        "get_critique_schedule",
        "update_critique_schedule",
        "delete_critique_schedule",
        "get_critique_scoring_weights",
        "update_critique_scoring_weights",
        "get_critique_config_overrides",
        "delete_critique_config_override",
        "create_critique_suppression",
        "list_critique_suppressions",
        "delete_critique_suppression",
        # review
        "start_review",
        "compare_reviews",
        "start_batch_review",
        "get_review_batch_status",
        "request_review_video",
        "get_review",
        "get_review_summary",
        "get_review_findings",
        "get_review_report",
        "get_review_progress",
        "list_reviews",
        "get_review_usage",
        "get_review_domain_history",
        # context (analyze sub-router)
        "infer_context",
        "validate_context",
        "enrich_context",
        # context-profiles
        "create_context_profile",
        "list_context_profiles",
        "get_context_profile",
        "delete_context_profile",
        # brands CRUD
        "list_brands",
        "get_brand",
        "invalidate_brand_cache",
        "get_brand_history",
        "get_brand_domain_logo",
        "override_brand",
        "export_brand",
        "get_brand_quality_report",
        "compare_brands",
        # critique jobs (write + read routers)
        "create_critique",
        "update_critique_job_tags",
        "rerun_critique",
        "stream_critique_progress",
        "get_critique",
        "list_critiques",
        "delete_critique",
    }
)


@pytest.fixture(scope="module")
def openapi_schema() -> dict:
    """Build the OpenAPI schema once per module."""
    app.openapi_schema = None
    return app.openapi()


def test_every_route_has_explicit_operation_id(openapi_schema: dict) -> None:
    """No route may rely on FastAPI's auto-generated operationId.

    FastAPI's auto-format is ``<function>_<path>_<method>`` and produces
    awful client names. The fallback ``generate_unique_id_function`` in
    asgi.py is defence-in-depth only — every production route must set
    ``operation_id="…"`` explicitly.
    """
    auto_suffixes = ("_post", "_get", "_put", "_delete", "_patch")
    offenders: list[tuple[str, str, str]] = []
    for path, item in openapi_schema.get("paths", {}).items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId", "")
            if op_id.endswith(auto_suffixes) and op_id not in EXPECTED_OPERATION_IDS:
                offenders.append((method.upper(), path, op_id))
    assert not offenders, f"Routes without explicit operation_id (auto-generated): {offenders}"


def test_operation_ids_match_plan(openapi_schema: dict) -> None:
    """The set of operationIds in the schema must equal the plan."""
    actual: set[str] = set()
    for item in openapi_schema.get("paths", {}).values():
        for op in item.values():
            if isinstance(op, dict) and op.get("operationId"):
                actual.add(op["operationId"])

    missing = EXPECTED_OPERATION_IDS - actual
    unexpected = actual - EXPECTED_OPERATION_IDS
    assert not missing and not unexpected, (
        f"operationId drift — missing: {sorted(missing)}, unexpected: {sorted(unexpected)}"
    )


def test_operation_ids_are_unique(openapi_schema: dict) -> None:
    """Every operationId must be unique across the whole schema."""
    seen: dict[str, tuple[str, str]] = {}
    duplicates: list[tuple[str, tuple[str, str], tuple[str, str]]] = []
    for path, item in openapi_schema.get("paths", {}).items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId")
            if not op_id:
                continue
            if op_id in seen:
                duplicates.append((op_id, seen[op_id], (method.upper(), path)))
            else:
                seen[op_id] = (method.upper(), path)
    assert not duplicates, f"Duplicate operationIds: {duplicates}"


def test_bearer_auth_scheme_is_declared(openapi_schema: dict) -> None:
    """The BearerAuth security scheme must be present in components."""
    schemes = openapi_schema.get("components", {}).get("securitySchemes", {})
    assert "BearerAuth" in schemes, "BearerAuth scheme missing from OpenAPI"
    scheme = schemes["BearerAuth"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"


def test_tenant_id_header_parameter_is_declared(openapi_schema: dict) -> None:
    """The reusable TenantIdHeader parameter must be in components.parameters."""
    parameters = openapi_schema.get("components", {}).get("parameters", {})
    assert "TenantIdHeader" in parameters, "TenantIdHeader parameter missing"
    param = parameters["TenantIdHeader"]
    assert param["in"] == "header"
    assert param["name"].lower() == "x-tenant-id"


def test_protected_routes_have_security_requirement(openapi_schema: dict) -> None:
    """Every /api/... operation must advertise BearerAuth."""
    api_paths = [p for p in openapi_schema.get("paths", {}) if p.startswith("/api/")]
    assert api_paths, "No /api/* paths found in schema"
    offenders: list[tuple[str, str]] = []
    for path in api_paths:
        for method, op in openapi_schema["paths"][path].items():
            if not isinstance(op, dict):
                continue
            security = op.get("security", [])
            if not any("BearerAuth" in entry for entry in security):
                offenders.append((method.upper(), path))
    assert not offenders, f"Protected routes missing BearerAuth: {offenders[:5]}…"


def test_public_routes_have_no_security_requirement(openapi_schema: dict) -> None:
    """Health + root paths must not require auth in the schema."""
    public = {"/", "/health", "/health/ready", "/health/live"}
    for path in public:
        item = openapi_schema.get("paths", {}).get(path)
        if item is None:
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            assert not op.get("security"), (
                f"Public route {method.upper()} {path} unexpectedly has security={op.get('security')}"
            )


def test_tag_groups_are_defined(openapi_schema: dict) -> None:
    """``openapi_tags`` must define a group per domain.

    Tag names are lowercase to match the per-router ``tags=[…]`` declarations.
    """
    tag_names = {t["name"] for t in openapi_schema.get("tags", [])}
    required = {"health", "analyze", "critique", "review", "brand", "brands"}
    assert required <= tag_names, f"Missing required tag groups; have {sorted(tag_names)}"
