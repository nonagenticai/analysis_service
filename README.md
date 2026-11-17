# Analysis Service

Unified website analysis service — page/content analysis, critique scoring, qualitative review, and brand extraction in one FastAPI app. Port 6303.

## What Analysis Service Does

When `w2v_orchestrator` runs a website-review, web-design, or brand-focused product stage, it sends this service a target URL (plus already-scraped page content in some flows) carrying the tenant's license token as `X-Tenant-Id: Bearer <token>`. Analysis Service is the consolidation of four formerly standalone services — `analyzer`, `critique`, `review`, and `brand_extractor` — that no longer exist as separate repos. Each now lives as a domain under `src/domains/`, so a single deployment answers every "look at this website and tell me about it" question the platform asks.

Once a request arrives, the relevant domain takes over. The **analyze** domain classifies pages, extracts the site narrative, and infers site context. The **critique** domain runs SEO, security, accessibility, performance, and UX scanners against the page and produces a weighted 0–100 score with per-finding detail. The **review** domain generates qualitative design, usability, feature, and content-quality reviews. The **brand** domain runs a twelve-stage pipeline (fetch → name → logo → metadata → colors → visual colors → fonts → social → LLM analysis → palette → logo download → quality) to extract a brand identity. Two recon-shaped domains — **icp** and **artifact-score** — derive a GTM/ICP profile from a URL and score a rendered artifact synchronously. Headless Playwright/Chromium renders pages when a live DOM or computed styles are needed, and every model call is routed through `worker_management` (`POST /workers/{name}/message`) — the sole LLM gateway; the service never talks to providers directly.

The outputs are structured records: critique scores and findings, review reports (with HTML/PDF/CSV/SARIF exports), extracted brand records with colors/typography/logos, and ICP/artifact-score results. Durable data is persisted to PostgreSQL and brand logos/assets land in the `analysis-service` MinIO bucket, from which the orchestrator and downstream video/content services fetch them. Critique work is long-running, so jobs are queued in Redis and processed by a background worker with a scheduler for recurring scans; the finalize step commits multiple times and writes an audit row per job.

Architecturally, the service is stateful and tenant-isolated. It enforces Postgres row-level security: the runtime connects as the restricted non-owner role `w2v_analysis_app` with `TENANT_RLS_MODE=enforce`, and because the critique worker's `process_job` commits many times, `install_rls_tenant_rebind()` re-binds the `app.current_tenant` GUC from the request-scoped identity ContextVar on every transaction so the tenant survives to the finalize audit INSERT. Redis is a soft dependency — if it is unreachable the service degrades to no-cache and disables the critique job queue rather than failing — while PostgreSQL and `worker_management` are hard dependencies.

## Features

- **Four analysis domains in one service** — analyze, critique, review, and brand share one FastAPI app and one deployment so the orchestrator hits a single endpoint instead of four services with four failure modes
- **Multi-scanner critique** — SEO, security, accessibility (WCAG perceivable/operable/understandable/robust), performance (LCP/CLS/INP/DOM), and UX scanners produce a category-weighted 0–100 score so a site's health is one comparable number plus actionable findings
- **Twelve-stage brand pipeline** — fetch, name, logo, metadata, colors, visual colors, fonts, social, LLM analysis, palette, logo download, and quality stages extract a full brand identity so downstream video/content generation can stay on-brand
- **Qualitative review with exports** — design/usability/feature/content-quality reviews render to HTML, PDF, CSV, and SARIF so results feed both humans and security tooling
- **ICP + artifact scoring** — recon-shaped `/api/brand/icp-from-url`, `/api/brand/score-artifact`, and `/api/brand/score-delta` endpoints derive a GTM/ICP profile and deterministically score a rendered artifact for the Merqi branding-coach flow
- **Headless Playwright rendering** — Chromium renders pages with a configurable 1440×900 viewport and `networkidle` wait so scanners and extractors see the real, JS-executed DOM
- **Asynchronous critique jobs** — a Redis-backed queue, background worker, and scheduler run long scans off the request path with recurring-scan support so callers get a job ID and poll for progress
- **RLS tenant isolation** — enforced Postgres row-level security via the restricted `w2v_analysis_app` role with per-transaction GUC re-bind so one tenant can never read another's jobs, findings, or brand records
- **LLM through a single gateway** — all model calls route through `worker_management` with per-call output schemas, so the service never holds provider keys and gets structured output guarantees while degrading gracefully when Redis/tracing are absent
- **Trend analytics** — critique history endpoints expose per-domain trends, summaries, and top findings so operators can track a site's score over time

## Authentication

Auth is enabled by default (`AUTH_ENABLED=true`). Inbound requests carry the license token as `X-Tenant-Id: Bearer <license_token>` — **not** the `Authorization` header, which is reserved for Keycloak JWTs issued to the SPA. `AuthMiddleware` (`src/shared/auth/middleware.py`, from `w2v_common`) introspects the token against the licenses service and caches the result for ~5 minutes (`AUTH_CACHE_TTL=300`). Requests from trusted CIDRs can be granted a synthetic internal identity mapped to the default tenant (off by default, fail-closed). For service-to-service calls, identity is forwarded as plain `X-Tenant-Id`, `X-License-Id`, and `X-Subject-Id` headers (no `Bearer` prefix), riding ContextVars per request.

| Priority | Method | Details |
|----------|--------|---------|
| 1 | License token | `X-Tenant-Id: Bearer <token>` introspected against the licenses service, cached ~5 min |
| 2 | Trusted network | Synthetic internal identity → `AUTH_DEFAULT_TENANT_ID` for trusted CIDRs (fail-closed, off by default) |

This service **enforces** Postgres row-level security. The runtime connects as the restricted non-owner role `w2v_analysis_app` with `TENANT_RLS_MODE=enforce`; `install_rls_tenant_rebind()` re-binds the `app.current_tenant` GUC from the identity ContextVar on every transaction, so the critique worker's multi-commit `process_job` keeps the tenant through to its finalize audit INSERT.

Exempt paths (no token required): `/`, `/health`, `/health/live`, `/health/ready`, `/health/startup`, `/healthz`, `/readyz`, `/ready`, `/docs`, `/redoc`, `/openapi.json`, `/favicon.ico`, `/metrics`.

## Quick Start

```bash
uv sync
uv run uvicorn src.asgi:app --host 0.0.0.0 --port 6303
uv run pytest tests/ -v --cov=src
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
```

## API Endpoints

All routes are unversioned (`/api/<resource>`).

### Analyze `/api/analyze`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/analyze` | Submit a site analysis job |
| GET | `/analyze` | List analysis jobs |
| GET | `/analyze/{job_id}` | Get an analysis job |
| DELETE | `/analyze/{job_id}` | Cancel/delete an analysis job |
| GET | `/analyze/{job_id}/artifacts` | List a job's artifacts |
| POST | `/analyze/pages` | Classify pages |
| POST | `/analyze/narrative` | Extract site narrative |
| POST | `/analyze/review` | Generate a page review |
| GET | `/analyze/review/{review_job_id}` | Get a review job result |

### Context `/api/context`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/infer` | Infer site context |
| POST | `/validate` | Validate a context |
| POST | `/enrich` | Enrich a context |

### Context Profiles `/api/context/profiles`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/` | Create a per-tenant context profile |
| GET | `/` | List context profiles |
| GET | `/{profile_id}` | Get a context profile |
| DELETE | `/{profile_id}` | Delete a context profile |

### Critique `/api/critique`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/critique` | Submit a critique scan job |
| GET | `/critique` | List critique jobs |
| GET | `/critique/{job_id}` | Get a critique job |
| DELETE | `/critique/{job_id}` | Delete a critique job |
| GET | `/critique/{job_id}/progress` | Job progress (SSE) |
| PATCH | `/critique/{job_id}/tags` | Update job tags |
| POST | `/critique/{job_id}/rerun` | Re-run a critique job |
| GET | `/critique/{job_id}/findings` | List findings |
| GET | `/critique/{job_id}/findings/unified` | Unified findings view |
| POST | `/critique/{job_id}/findings/{finding_id}/acknowledge` | Acknowledge a finding |
| GET | `/critique/{job_id}/export/csv` | Export findings as CSV |
| GET | `/critique/{job_id}/export/sarif` | Export findings as SARIF |
| GET | `/critique/{job_id}/report` | HTML report |
| GET | `/critique/{job_id}/report/pdf` | PDF report |
| POST | `/critique/batch` | Submit a batch scan |
| GET | `/critique/batch/{batch_id}` | Get batch status |
| GET | `/critique/compare` | Compare two scans |
| GET | `/critique/compare/history` | Comparison history |
| GET/PUT | `/scoring/weights` | Get/update scoring weights |
| GET | `/config/overrides` | List runtime config overrides |
| DELETE | `/config/overrides/{section}/{key}` | Delete a config override |
| POST/GET | `/critique/suppressions` | Create/list finding suppressions |
| DELETE | `/critique/suppressions/{suppression_id}` | Delete a suppression |
| POST/GET | `/critique/schedules` | Create/list scheduled scans |
| GET/PUT/DELETE | `/critique/schedules/{schedule_id}` | Get/update/delete a schedule |
| GET | `/critique/analytics/trends` | Score trends |
| GET | `/critique/analytics/domain/{domain}` | Per-domain analytics |
| GET | `/critique/analytics/summary` | Analytics summary |
| GET | `/critique/analytics/findings/top` | Top findings |

### Review `/api/review`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/review` | Submit a review job |
| GET | `/review` | List review jobs |
| GET | `/review/{job_id}` | Get a review job |
| GET | `/review/{job_id}/summary` | Review summary |
| GET | `/review/{job_id}/findings` | Review findings |
| GET | `/review/{job_id}/report` | Review report |
| GET | `/review/{job_id}/progress` | Progress (SSE) |
| POST | `/review/{job_id}/video` | Generate a review video |
| GET | `/review/compare` | Compare reviews |
| POST | `/review/batch` | Submit a batch review |
| GET | `/review/batch/{batch_id}` | Get batch status |
| GET | `/usage` | Tenant usage |
| GET | `/domain/{domain}/history` | Per-domain review history |

### Brand `/api/brand` and `/api/brands`

| Method | Path | Description |
|--------|------|-------------|
| POST | `/brand/extract` | Extract brand identity from supplied content |
| POST | `/brand/from-url` | Extract brand identity from a URL |
| POST | `/brand/batch-extract` | Batch brand extraction |
| POST | `/brand/logo/download` | Download/store a logo asset |
| GET | `/brand/logo/{asset_id}` | Get a logo asset |
| DELETE | `/brand/logo/{asset_id}` | Delete a logo asset |
| POST | `/brand/icp-from-url` | Derive a GTM/ICP profile from a URL |
| POST | `/brand/score-artifact` | Score a rendered artifact (deterministic) |
| POST | `/brand/score-delta` | Score the delta between two artifacts |
| GET | `/brands` | List persisted brand records |
| GET | `/brands/{domain}` | Get a brand record |
| GET | `/brands/{domain}/history` | Brand history |
| GET | `/brands/{domain}/logo` | Brand logo |
| GET | `/brands/{domain}/export` | Export a brand record |
| GET | `/brands/{domain}/quality` | Brand quality report |
| PUT | `/brands/{domain}/override` | Override brand fields |
| DELETE | `/brands/{domain}/cache` | Invalidate brand cache |
| POST | `/brands/compare` | Compare two brands |

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Service info and endpoint listing |
| GET | `/health` | Health check |
| GET | `/health/live` | Liveness probe |
| GET | `/health/ready` | Readiness probe (checks DB) |
| GET | `/health/startup` | Startup probe |
| GET | `/metrics` | Prometheus metrics |

## Configuration

All configuration via environment variables.

| Variable | Default | Description |
|----------|---------|-------------|
| `API_HOST` | `0.0.0.0` | Listen host |
| `API_PORT` | `6303` | Listen port (base `Settings` rejects `8000`) |
| `API_DEBUG` | `false` | Enable debug mode / `/docs` |
| `AUTH_ENABLED` | `true` | Toggle the auth middleware |
| `AUTH_DEFAULT_TENANT_ID` | unattributed sentinel UUID | Tenant for trusted-network synthetic identity |
| `AUTH_CACHE_TTL` | `300` | License introspection cache TTL (seconds) |
| `TENANT_RLS_MODE` | `enforce` (prod) | Postgres RLS mode; inert until app connects as `w2v_analysis_app` |
| `LOG_LEVEL` | `INFO` | Logging level |
| `LOG_JSON_LOGS` | `false` | Structured JSON logging |
| `DATABASE_URL` | — (required) | PostgreSQL connection string (psycopg 3) |
| `DATABASE_POOL_SIZE` | `10` | Connection pool size |
| `DATABASE_POOL_MIN_SIZE` | `2` | Minimum pool connections |
| `DATABASE_MAX_OVERFLOW` | `20` | Max connections beyond pool size |
| `REDIS_URL` | — (required) | Redis URL (cache + critique job queue) |
| `REDIS_MAX_CONNECTIONS` | `10` | Redis pool max connections |
| `STORAGE_MINIO_BUCKET` | `analysis-service` | MinIO bucket for brand logos/assets |
| `PLAYWRIGHT_HEADLESS` | `true` | Run Chromium headless |
| `PLAYWRIGHT_TIMEOUT_MS` | `30000` | Page load timeout (ms) |
| `PLAYWRIGHT_VIEWPORT_WIDTH` | `1440` | Browser viewport width |
| `PLAYWRIGHT_VIEWPORT_HEIGHT` | `900` | Browser viewport height |
| `PLAYWRIGHT_WAIT_UNTIL` | `networkidle` | Playwright wait strategy |
| `WM_URL` | in-cluster default | Worker-management gateway base URL |
| `WM_TIMEOUT_SECONDS` | `600` | Per-message HTTP timeout to worker_management |
| `ANALYZE_PARALLEL_WORKERS` | `4` | Parallel page-analysis workers |
| `SCANNER_*` | see `ScannerSettings` | Per-scanner timeouts + retries (critique) |
| `RATE_LIMIT_ENABLED` | `false` | Toggle rate-limiting middleware |
| `RETENTION_ENABLED` | `false` | Toggle critique job/audit retention cleanup |
| `TRACING_ENABLED` | `false` | Enable OpenTelemetry tracing (`TRACING_ENDPOINT` opt-in) |

## Architecture

```
src/
├── asgi.py                         # FastAPI app, lifespan, middleware stack, root + OpenAPI
├── config.py                       # Pydantic settings (extends w2v_common) — per-domain sections
├── llm_client.py                   # WMClient factory — the sole LLM gateway
├── llm_credentials.py              # LLM credential resolution
├── logging_config.py               # Structured logging setup
├── metrics.py                      # Prometheus metric definitions
├── domains/
│   ├── analyze/                    # Page classification, narrative, context, profiles
│   │   ├── routes.py               # /api/analyze + /api/context routers
│   │   ├── context_profiles.py     # /api/context/profiles CRUD
│   │   └── services/               # analyzer, page/narrative/metadata extractors, context, repos
│   ├── critique/                   # SEO/security/a11y/perf/UX scoring
│   │   ├── routes/                 # jobs, findings, scoring, exports, analytics, schedules, ...
│   │   └── services/               # critique_service, scheduler, scanners/, reports
│   ├── review/                     # Qualitative design/usability/content review
│   │   ├── routes.py               # /api/review router
│   │   └── services/               # review_service, scoring, reports, video_client, cleanup
│   ├── brand/                      # Brand identity extraction
│   │   ├── routes.py               # /api/brand router
│   │   ├── brands_routes.py        # /api/brands persisted-record CRUD
│   │   ├── pipeline/               # 12-stage PipelineRunner (base, context, runner, types)
│   │   ├── stages/                 # fetch, name, logo, colors, fonts, palette, quality, ...
│   │   └── services/               # storage, logo, cache, comparator, extractors, analyzers
│   ├── icp/                        # /api/brand/icp-from-url (GTM/ICP recon)
│   └── artifact_score/             # /api/brand/score-artifact + score-delta (deterministic)
└── shared/
    ├── auth/                       # AuthMiddleware + tenant models
    ├── cache/redis_client.py       # Redis client (soft dependency)
    ├── database/
    │   ├── connection.py           # Async engine
    │   ├── session.py              # Session maker
    │   ├── rls_events.py           # install_rls_tenant_rebind() — per-txn GUC re-bind
    │   └── migrations/versions/    # Alembic migrations 0001–0006
    ├── health.py                   # Health + metrics endpoints
    ├── middleware/                 # shutdown, request_id, security, audit, metrics, rate_limiter
    ├── observability/tracing.py    # OpenTelemetry setup (opt-in)
    └── security/url_validator.py   # SSRF-safe URL validation
```

## Testing

```bash
uv run pytest tests/ -v --cov=src          # Full suite with coverage
uv run pytest tests/unit/ -v               # Unit tests
uv run ruff check src/ tests/              # Lint
uv run ruff format --check src/ tests/     # Format check
bash scripts/pip-audit.sh                  # Dependency CVE audit (the script CI runs)
```

Coverage threshold: 80% (enforced in `pyproject.toml`). The suite is large — roughly 2,800 Postgres-backed tests across the four analysis domains — and is DB-bound, so `pytest` runs with `-n 4` (per-worker schema isolation) and `--reruns 2` to absorb parallel-load flakes. Tests connect to a per-service CI database; RLS and multi-commit paths are exercised against real Postgres, not SQLite.

## CI/CD

`.github/workflows/ci-public.yml` is self-contained and runs on GitHub-hosted runners: a betterleaks secret scan, the `no-private-refs` guard, then `ruff check`, `ruff format --check`, `pytest` against a Postgres 18 service container, and `scripts/pip-audit.sh`.

No image is published from this repository. The end-to-end harness lived in the private estate and is not part of this source tree.

## Docker

```bash
docker build -t analysis-service .
docker run -p 6303:6303 analysis-service
docker run -p 6303:6303 -e LOG_LEVEL=DEBUG -e AUTH_ENABLED=false \
  -e DATABASE_URL=postgresql+psycopg://host/analysis_service \
  -e REDIS_URL=redis://host:6379/0 analysis-service
```

For local testing only — production deploys via CI/CD (push → build → `registry.test:5000` → Flux → K3s).

## Dependencies

- **w2v_orchestrator** — upstream caller; drives website-review / web-design / brand product stages
- **worker_management** (port 8887) — the sole LLM gateway; all model calls go through `POST /workers/{name}/message`
- **licenses service** — validates license tokens via introspection (cached ~5 min)
- **ai_content_generation** / **video_production** — optional downstream peers (resolved via `w2v_common.discovery.service_url`)
- **PostgreSQL 18** — persistent storage via PgBouncer (`pgbouncer.pgbouncer:6432`), psycopg 3 driver, RLS-enforced
- **Redis** — cache + critique job queue/scheduler (soft dependency; degrades to no-cache if absent)
- **MinIO** — `analysis-service` bucket for brand logos and assets
- **FastAPI** / **Uvicorn** — web framework and ASGI server; **Pydantic v2** / **pydantic-settings** — schema and settings validation
- **SQLAlchemy** (async) + **Alembic** — ORM and migrations
- **Playwright** — headless Chromium rendering for scanners and extractors
- **BeautifulSoup4** / **lxml** — HTML parsing
- **WeasyPrint** — PDF report rendering
- **Pillow** / **numpy** — image and color analysis
- **httpx** — async HTTP client; **cachetools** — in-process TTL caches
- **OpenTelemetry** — optional distributed tracing
- **w2v-common** — shared config, auth middleware, clients, and error envelope

## Known Issues

- **~~Inert legacy config classes~~ — resolved.** `DalleSettings`, `FluxSettings`, `ScreenshotSettings`, `ContentGenerationSettings`, `BrandSettings` and the `CritiqueSettings` decoy have been **deleted**: nothing read any of them, so each was a control panel wired to nothing. Their env prefixes (`DALLE_`, `FLUX_`, `SCREENSHOT_`, `CONTENT_GENERATION_`, `BRAND_`, `CRITIQUE_`) now **fail startup** naming the replacement, because an unclaimed env var is ignored just as silently as a dead class read it.

  Two classes that were *listed* as inert are not, and must not be deleted: `ScannerSettings` is the live per-scanner timeout surface (`settings.scanners`, prefix `SCANNER_`), and `PlaywrightSettings` drives the brand pipeline's browser (`src/domains/brand/stages/fetch.py`, prefix `PLAYWRIGHT_`). `tests/unit/shared/test_no_dead_settings_surface.py` enforces both directions.

## License

Apache 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

## Licensing

This service contains no licence enforcement of its own and runs no licence server.
It runs with no licence switch set; unset means off.
