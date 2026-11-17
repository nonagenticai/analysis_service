# Operation ID Plan: analysis-service

Canonical registry of `operation_id` values for every route in
`analysis-service`. These IDs feed the org-wide n8n custom-node generator and
the `ultrareview` cross-service catalogue, so they must be:

- **stable** — never rename without coordinating downstream,
- **`verb_noun` snake_case** — matches the org standard (nlp_service,
  worker_management, kag),
- **globally unique** — disambiguated against the rest of the fleet's verbs.

This service hosts five domains (analyze, critique, review, brand, redesign)
plus a health surface. Where a generic verb (`start_*`, `get_batch_*`,
`get_*_status`, `list_*`) would collide across domains, the operation_id is
prefixed with the domain name.

## Files to edit

- `src/asgi.py` (root endpoint)
- `src/shared/health.py`
- `src/domains/analyze/routes.py`
- `src/domains/analyze/context_profiles.py` *(if any decorators present)*
- `src/domains/brand/routes.py`
- `src/domains/brand/brands_routes.py` *(brands CRUD — currently no decorators counted; flag in PR if any exist)*
- `src/domains/critique/routes/*.py`
- `src/domains/redesign/routes/*.py`
- `src/domains/review/routes.py`

## Mapping Tables

### `src/asgi.py`

| Method | Function | operation_id   |
|--------|----------|----------------|
| GET    | `root`   | `service_root` |

### `src/shared/health.py`

| Method | Function           | operation_id        |
|--------|--------------------|---------------------|
| GET    | `health_check`     | `health_check`      |
| GET    | `health_live`      | `health_live`       |
| GET    | `health_ready`     | `health_ready`      |
| GET    | `health_startup`   | `health_startup`    |
| GET    | `prometheus_metrics` | `prometheus_metrics` |

### `src/domains/analyze/routes.py` (tag: `Analyze`)

| Method | Function                | operation_id              |
|--------|-------------------------|---------------------------|
| POST   | `start_analysis`        | `start_analysis`          |
| GET    | `list_analysis_jobs`    | `list_analysis_jobs`      |
| GET    | `get_analysis`          | `get_analysis`            |
| DELETE | `cancel_analysis`       | `cancel_analysis`         |
| GET    | `get_artifacts`         | `get_analysis_artifacts`  |
| POST   | `classify_pages`        | `classify_pages`          |
| POST   | `extract_narrative`     | `extract_narrative`       |
| POST   | `start_review`          | `start_analyze_subreview` |
| GET    | `get_review`            | `get_analyze_subreview`   |

(`/analyze/review/*` exists alongside top-level `/review/*`. Domain-prefix the
analyze sub-review op_ids to disambiguate from the review domain's primary
routes below.)

### `src/domains/analyze/context_profiles.py` + context router (tag: `Context`)

To be filled in during execution — Phase 0 audit showed no decorator-level
hits in this file, but the asgi mounts `profiles_router` and `context_router`
sourced from `src/domains/analyze/`. Verify and add op_ids if any routes exist;
otherwise document "none" in the PR description.

### `src/domains/brand/routes.py` (tag: `Brand`)

| Method | Function           | operation_id           |
|--------|--------------------|------------------------|
| POST   | `extract_brand`    | `extract_brand`        |
| POST   | `_check_url_cache` | `extract_brand_from_url` |
| POST   | `batch_extract`   | `batch_extract_brand`   |
| POST   | `download_logo`   | `download_brand_logo`   |
| GET    | `serve_logo`      | `get_brand_logo`        |
| DELETE | `delete_logo`     | `delete_brand_logo`     |

(`_check_url_cache` is the handler's name in the source — the op_id reflects
its actual behaviour rather than the implementation-leaking underscore name.)

### `src/domains/brand/brands_routes.py` (tag: `Brands`)

Audit showed 0 decorators in Phase 0; if any exist on execution, name them
`list_brands`, `get_brand`, `create_brand`, `update_brand`, `delete_brand`
following standard CRUD verbs.

### `src/domains/critique/routes/` (tag: `Critique`)

| File | Method | Function                       | operation_id                    |
|------|--------|--------------------------------|---------------------------------|
| analytics.py | GET    | `get_analytics_trends`         | `get_critique_analytics_trends` |
| analytics.py | GET    | `get_domain_analytics`         | `get_critique_domain_analytics` |
| analytics.py | GET    | `get_tenant_summary`           | `get_critique_tenant_summary`   |
| analytics.py | GET    | `get_top_findings`             | `get_critique_top_findings`     |
| batch.py     | POST   | `create_batch_critique`        | `create_critique_batch`         |
| batch.py     | GET    | `get_batch_status`             | `get_critique_batch_status`     |
| compare.py   | GET    | `compare_critiques`            | `compare_critiques`             |
| compare.py   | GET    | `compare_history`              | `list_critique_compare_history` |
| exports.py   | GET    | `export_csv`                   | `export_critique_csv`           |
| exports.py   | GET    | `export_sarif`                 | `export_critique_sarif`         |
| exports.py   | GET    | `get_critique_report`          | `get_critique_report`           |
| exports.py   | GET    | `get_critique_report_pdf`      | `get_critique_report_pdf`       |
| findings.py  | GET    | `get_unified_findings`         | `get_critique_unified_findings` |
| findings.py  | POST   | `acknowledge_finding`          | `acknowledge_critique_finding`  |
| findings.py  | GET    | `get_critique_findings`        | `get_critique_findings`         |
| schedules.py | POST   | `create_schedule`              | `create_critique_schedule`      |
| schedules.py | GET    | `list_schedules`               | `list_critique_schedules`       |
| schedules.py | GET    | `get_schedule`                 | `get_critique_schedule`         |
| schedules.py | PUT    | `update_schedule`              | `update_critique_schedule`      |
| schedules.py | DELETE | `delete_schedule`              | `delete_critique_schedule`      |
| scoring.py   | GET    | `get_scoring_weights_endpoint` | `get_critique_scoring_weights`  |
| scoring.py   | PUT    | `update_scoring_weights`       | `update_critique_scoring_weights` |
| scoring.py   | GET    | `get_config_overrides`         | `get_critique_config_overrides` |
| scoring.py   | DELETE | `delete_config_override`       | `delete_critique_config_override` |
| suppressions.py | POST | `create_suppression`           | `create_critique_suppression`   |
| suppressions.py | GET  | `list_suppressions`            | `list_critique_suppressions`    |
| suppressions.py | DELETE | `delete_suppression`         | `delete_critique_suppression`   |

### `src/domains/redesign/routes/` (tag: `Redesign`)

| File | Method | Function                       | operation_id                    |
|------|--------|--------------------------------|---------------------------------|
| artifacts.py | GET    | `download_html`              | `download_redesign_html`        |
| artifacts.py | GET    | `download_screenshot`         | `download_redesign_screenshot`  |
| artifacts.py | GET    | `download_component_html`     | `download_redesign_component_html` |
| artifacts.py | GET    | `download_original_screenshot` | `download_redesign_original_screenshot` |
| artifacts.py | GET    | `download_comparison`         | `download_redesign_comparison`  |
| artifacts.py | GET    | `download_diff`               | `download_redesign_diff`        |
| batch.py     | POST   | `create_batch`                | `create_redesign_batch`         |
| batch.py     | GET    | `get_batch_status`            | `get_redesign_batch_status`     |
| batch.py     | GET    | `get_batch_results`           | `get_redesign_batch_results`    |
| batch.py     | GET    | `list_batches`                | `list_redesign_batches`         |
| batch.py     | POST   | `cancel_batch`                | `cancel_redesign_batch`         |
| batch.py     | POST   | `retry_failed_batch_pages`    | `retry_redesign_batch_failed`   |
| jobs.py      | GET    | `get_job_status`              | `get_redesign_job_status`       |
| jobs.py      | GET    | `get_job_result`              | `get_redesign_job_result`       |
| jobs.py      | POST   | `cancel_job`                  | `cancel_redesign_job`           |
| jobs.py      | GET    | `list_jobs`                   | `list_redesign_jobs`            |
| refinement.py | POST  | `refine_redesign`             | `refine_redesign`               |
| refinement.py | GET   | `get_iteration_history`       | `list_redesign_iteration_history` |
| styles.py    | GET    | `list_styles`                 | `list_redesign_styles`          |
| styles.py    | POST   | `create_style`                | `create_redesign_style`         |
| styles.py    | GET    | `get_style`                   | `get_redesign_style`            |
| styles.py    | PUT    | `update_style`                | `update_redesign_style`         |
| styles.py    | DELETE | `delete_style`                | `delete_redesign_style`         |
| styles.py    | POST   | `preview_style`               | `preview_redesign_style`        |
| variants.py  | GET    | `compare_variants`            | `compare_redesign_variants`     |
| variants.py  | GET    | `download_variant_html`       | `download_redesign_variant_html` |
| webhooks.py  | GET    | `get_webhook_deliveries`      | `list_redesign_webhook_deliveries` |

### `src/domains/review/routes.py` (tag: `Review`)

| Method | Function                  | operation_id            |
|--------|---------------------------|-------------------------|
| POST   | `start_review`            | `start_review`          |
| GET    | `compare_reviews_endpoint` | `compare_reviews`       |
| POST   | `start_batch_review`      | `start_batch_review`    |
| GET    | `get_batch_status`        | `get_review_batch_status` |
| POST   | `request_review_video`    | `request_review_video`  |
| GET    | `get_review`              | `get_review`            |
| GET    | `get_review_summary`      | `get_review_summary`    |
| GET    | `get_review_findings`     | `get_review_findings`   |
| GET    | `get_review_report`       | `get_review_report`     |
| GET    | `get_review_progress`     | `get_review_progress`   |
| GET    | `list_reviews`            | `list_reviews`          |
| GET    | `get_usage`               | `get_review_usage`      |
| GET    | `get_domain_history`      | `get_review_domain_history` |

## Verification

```bash
cd analysis_service
uv run pytest tests/test_openapi.py -x
```

`tests/test_openapi.py` loads `app.openapi()` and asserts that the set of
`operationId` values equals the union of every right-hand column above.
Drift (rename, missed decorator, fallback) fails the test loudly.

## Disambiguation rationale

Concrete renames where the function name was generic enough to collide
across the fleet or within this service:

- `get_artifacts` → `get_analysis_artifacts` — `get_artifacts` collides with
  the `artifact_service`'s primary verb.
- `start_review` (analyze domain) → `start_analyze_subreview`, and `get_review`
  (analyze domain) → `get_analyze_subreview` — analyze hosts its own internal
  sub-review pipeline distinct from the top-level review domain.
- `get_batch_status` (critique vs redesign vs review) → prefixed by domain.
- `create_batch` (redesign), `create_batch_critique` (critique), `start_batch_review`
  (review) → `create_redesign_batch`, `create_critique_batch`, `start_batch_review`
  (last is already domain-specific enough).
- All redesign artifact downloads → `download_redesign_*` to keep them out of
  any generic `download_html` / `download_screenshot` namespace.
- `_check_url_cache` (brand) → `extract_brand_from_url` — the source name
  leaks implementation; the op_id describes the operation.

## Future routes

When adding a route in this service, **always** set `operation_id="…"`
explicitly. The `generate_unique_id_function` configured in `asgi.py` is a
safety net for forgotten kwargs, never the primary identifier. New IDs go
in the table above as part of the same PR (no out-of-band updates).
