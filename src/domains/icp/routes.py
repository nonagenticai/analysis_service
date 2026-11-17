"""API route for GTM/ICP extraction from a URL.

``POST /api/brand/icp-from-url`` — the **authenticated** ICP endpoint consumed
by Merqi's recon module. Distinct from the visual ``/api/brand/from-url``
extractor: this returns recon's flat GTM/ICP key set (see
:mod:`src.domains.icp.schemas`). Auth is enforced by the shared
``AuthMiddleware`` (any ``/api/*`` path that is not ``/api/health*`` requires
``X-Tenant-Id: Bearer <license>``); this route is deliberately NOT exempt.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from w2v_common.auth import LICENSE_GATE
from w2v_common.clients import WebscraperClient
from w2v_common.clients._ssl import get_ssl_context
from w2v_common.clients.webscraper import MAX_BULK_URLS, ScrapeResponse
from w2v_common.database.rls import bind_session_tenant
from w2v_common.discovery import service_url
from w2v_common.tenant import MissingTenantError

from src.domains.analyze.schemas import PageType
from src.domains.analyze.services.page_classifier import PageClassifier
from src.domains.icp.models import IcpExtraction
from src.domains.icp.schemas import ICPFromUrlRequest, ICPResponse
from src.domains.icp.service import analyze_icp_with_llm, build_prompt, merge_page_sections
from src.shared.database.session import get_db_session
from src.shared.security.url_validator import validate_callback_url
from src.shared.tenant import require_request_tenant, tenant_uuid
from src.shared.upstream_errors import upstream_http_exception

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["icp"], dependencies=list(LICENSE_GATE))

# Page types worth fetching beyond the landing page: pricing carries
# `pricing_signals`; about/product carry `competitors` / `positioning` / `category`
# that rarely appear on the landing page. Ordered by extraction value.
_EXTRA_PAGE_PRIORITY: dict[PageType, int] = {
    PageType.PRICING: 0,
    PageType.ABOUT: 1,
    PageType.PRODUCT: 2,
}


def _get_tenant_id(request: Request) -> str:
    """Get tenant ID from request state (set by AuthMiddleware)."""
    return str(require_request_tenant(request))


async def _check_icp_cache(request: Request, tenant_id: str, url: str) -> ICPResponse | None:
    """Return a cached ICP record for ``url`` if present (else ``None``). Best-effort."""
    try:
        icp_cache = getattr(request.app.state, "icp_cache", None)
        if icp_cache:
            cached = await icp_cache.get_by_url(tenant_id, url)
            if cached:
                logger.info("ICP cache hit for url=%s, returning cached result", url)
                return cached
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("ICP cache lookup failed: %s", e)
    return None


async def _cache_icp(request: Request, tenant_id: str, url: str, result: ICPResponse) -> None:
    """Best-effort cache write of a successful extraction (keyed by the URL's domain)."""
    try:
        icp_cache = getattr(request.app.state, "icp_cache", None)
        domain = urlparse(url).netloc.removeprefix("www.")
        if icp_cache and domain:
            await icp_cache.set(tenant_id, domain, result)
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Failed to cache ICP result: %s", e)


async def _persist_icp(tenant_id: str, url: str, result: ICPResponse) -> None:
    """Best-effort audit-trail write of a successful extraction.

    Runs in its own tenant-bound transaction (``get_db_session`` binds the GUC
    from context; we also bind explicitly with the tenant we hold so RLS scopes
    the INSERT even before the table's migration has rolled out everywhere). A
    single INSERT + commit — no multi-commit, so no ``after_begin`` rebind needed.
    Any failure (table missing pre-rollout, RLS, transport) is swallowed: recon
    still gets its ``200``.
    """
    try:
        domain = urlparse(url).netloc.removeprefix("www.")
        async with get_db_session() as session:
            await bind_session_tenant(session, tenant_uuid(tenant_id))
            session.add(
                IcpExtraction(
                    tenant_id=tenant_id,
                    domain=domain or None,
                    url=url,
                    response=result.model_dump(mode="json", exclude_unset=True),
                    confidence=result.confidence,
                )
            )
    except Exception as e:  # noqa: BLE001 - persistence is best-effort, never fatal
        logger.warning("Best-effort ICP persist failed: %s", e)


def _normalize_url(url: str) -> str:
    """Drop the fragment and a trailing slash so URLs dedupe cleanly."""
    return url.split("#", 1)[0].rstrip("/")


def _link_href(link: object) -> str | None:
    """Extract a usable href from one scraped link entry, or ``None``.

    ``WebscraperClient`` normalises ``ScrapeResponse.links`` to URL strings, but the
    retired KAG hop returned anchor dicts (``{"text", "url", "rel", "type"}``) under
    the same ``list[str]`` annotation, and some tests still hand back that shape.
    Normalize both and reject anything unusable (``None`` / non-str junk) so
    best-effort enrichment never feeds ``urljoin`` a non-str and 500s the request.
    Skipping the ``dict`` shape is why multi-page enrichment once never fired.
    """
    if isinstance(link, str):
        return link or None
    if isinstance(link, dict):
        url = cast("dict[str, Any]", link).get("url")
        return url if isinstance(url, str) and url else None
    return None


def _select_extra_urls(landing_url: str, links: list[str], max_pages: int) -> list[tuple[str, str]]:
    """Pick up to ``max_pages`` on-domain pricing/about/product links to enrich extraction.

    Resolves each discovered link against the landing URL, keeps only same-domain
    http(s) links whose path classifies as a wanted page type, dedupes, drops the
    landing page itself, and returns ``(page_type_value, url)`` ordered by
    extraction value (pricing first). Returns ``[]`` when nothing matches — the
    caller then falls back to the landing page alone.
    """
    base_netloc = urlparse(landing_url).netloc.removeprefix("www.")
    landing_norm = _normalize_url(landing_url)
    classifier = PageClassifier()
    seen: set[str] = {landing_norm}
    picked: list[tuple[PageType, str]] = []
    for link in links:
        # Links arrive as anchor dicts ({"url": ...}) or occasionally plain
        # strings; ``_link_href`` normalizes both and drops unusable junk so
        # ``urljoin`` never sees a non-str. Enrichment is best-effort.
        href = _link_href(link)
        if href is None:
            continue
        absolute = urljoin(landing_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.removeprefix("www.") != base_netloc:
            continue
        norm = _normalize_url(absolute)
        if norm in seen:
            continue
        page_type = classifier.classify(norm)
        if page_type not in _EXTRA_PAGE_PRIORITY:
            continue
        seen.add(norm)
        picked.append((page_type, norm))
    picked.sort(key=lambda pt: _EXTRA_PAGE_PRIORITY[pt[0]])
    return [(pt.value, url) for pt, url in picked[:max_pages]]


async def _screen_harvested_url(label: str, url: str) -> tuple[str, str] | None:
    """Re-screen one harvested URL for SSRF; ``None`` means "do not fetch it".

    Why a SECOND check when the landing URL is already validated
    -----------------------------------------------------------
    ``_select_extra_urls`` keeps only links whose ``netloc`` equals the landing
    page's, so the entry guard does carry over to almost every harvested link.
    It is not airtight, and the gaps are cheap to close here rather than argued
    about later:

    * The same-domain test compares ``netloc.removeprefix("www.")``, so
      ``www.example.com`` and ``example.com`` are treated as one domain. They
      are two distinct DNS names and may hold two distinct A records — only one
      of which was resolved at admission.
    * ``getaddrinfo`` is asked again at fetch time, from a different network
      position (in-cluster, where split-DNS answers estate names). A name whose
      authoritative server changes its answer between the two lookups is the
      rebinding case the entry guard explicitly does not close.

    Both are string-level escapes from a string-level filter, so the answer is
    to stop matching strings and re-resolve. Off-thread, because
    ``validate_callback_url`` calls the blocking resolver and this runs for up
    to ``max_pages`` URLs concurrently — the same reason
    ``playwright_guard.install_ssrf_guard`` dispatches its check with
    ``asyncio.to_thread``. (The route's own entry check stays inline, matching
    ``critique/routes/jobs.py``: one URL, on the request's own critical path.)

    A rejected link is DROPPED, not fatal. Enrichment is documented best-effort
    and the landing page has already been fetched successfully; a legitimate
    site whose footer links ``http://localhost:3000`` must not turn a good
    extraction into a 502.

    Args:
        label: The page-type label ``_select_extra_urls`` assigned to *url*.
        url: The harvested absolute URL.

    Returns:
        ``(label, url)`` when the URL is safe to dereference, else ``None``.
    """
    try:
        await asyncio.to_thread(validate_callback_url, url)
    except (ValueError, OSError) as exc:
        # OSError as well as ValueError: the validator converts ``gaierror``
        # into ``ValueError``, but a resolver failure that is not gaierror
        # (EAI_AGAIN surfacing as a bare OSError) would otherwise escape and
        # abort enrichment instead of dropping one URL. Fail CLOSED either way.
        logger.warning("ICP enrichment: refusing harvested URL %s (%s)", url, exc)
        return None
    return (label, url)


async def _fetch_extra_pages(
    ws_client: WebscraperClient, picked: list[tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """Bulk-fetch the selected extra URLs; return ``(label, url, text)`` for the good ones.

    Best-effort: a bulk-fetch failure (or a single page erroring) never fails the
    request — the landing page already succeeded, so we degrade to whatever pages
    we could fetch.

    Every URL is re-screened by :func:`_screen_harvested_url` immediately before
    the bulk fetch. Placing the guard here rather than inside the selection
    helper is deliberate: this is the function that owns the sink, so a future
    caller that assembles its own list still cannot reach ``scrape_bulk``
    unscreened.
    """
    screened = await asyncio.gather(*(_screen_harvested_url(label, url) for label, url in picked))
    # webscraper's bulk endpoint is synchronous and capped at MAX_BULK_URLS (the
    # client raises ValueError above it). ``max_pages`` is <=5 today; the slice keeps
    # a future schema bump from silently degrading enrichment to nothing.
    picked = [item for item in screened if item is not None][:MAX_BULK_URLS]
    if not picked:
        return []
    urls = [url for _, url in picked]
    try:
        results: list[ScrapeResponse] = await ws_client.scrape_bulk(urls)
    except Exception as e:  # noqa: BLE001 - enrichment is best-effort (incl. HTTPStatusError)
        logger.warning("ICP extra-page bulk fetch failed (using landing only): %s", e)
        return []
    extra: list[tuple[str, str, str]] = []
    for (label, url), result in zip(picked, results, strict=False):
        if not result.error and result.text:
            extra.append((label, url, result.text))
    return extra


class _ScrapesUrls(Protocol):
    """The one capability :func:`_scrape_or_raise` needs.

    Typed by capability rather than by WebscraperClient so a test can substitute a double
    that raises a chosen exception class — the branches below are the point of the
    function, and an untestable error path is how the blanket `except` survived this long.
    """

    async def scrape_url(self, url: str) -> ScrapeResponse: ...


async def _scrape_or_raise(ws_client: _ScrapesUrls, url: str) -> ScrapeResponse:
    """Scrape one URL, turning each failure CLASS into a distinguishable 502.

    Every branch stays 502 and stays fail-closed. What changes is that the class survives
    into the detail and the log. The old blanket ``except Exception`` flattened "the scraper was
    slow", "the scraper was unreachable", "the scraper answered 4xx" and "the client raised" into one
    indistinguishable string -- which is why merqi.nl's 2026-08-30 failure could not be
    explained afterwards: marketing's ``recon_discovery_job.attempt_log`` recorded
    ``502 Bad Gateway`` and nothing else.

    Status is deliberately NOT changed (a timeout is arguably 504). Marketing classifies a
    brand-leg failure as non-retryable by status -- {400,401,402,403,404,422} -- so every
    class here is already correctly transient, and moving one to 504 would be a behaviour
    change bought for no diagnostic gain.
    """
    # Screen HERE, adjacent to the sink, not only at the route entry.
    #
    # The route validates `body.url` before the cache lookup (so a refused URL is refused
    # outright rather than answered from a pre-guard cache record) -- that check exists for
    # a CACHE-POISONING reason and must stay where it is. This one exists for a different
    # reason: a delegated fetch is still this service's SSRF, and the screen has to be in
    # the same function as the call that hands the URL to webscraper, so the helper is safe to
    # call from anywhere later. tests/unit/shared/test_scrape_sinks_guarded.py enforces
    # exactly that locality, and it caught this function when the sink was extracted out
    # from under the route's guard.
    #
    # Cost is one extra blocking resolve on a path that then scrapes and invokes an LLM.
    try:
        await asyncio.to_thread(validate_callback_url, url)
    except ValueError as e:
        logger.warning("icp-from-url: refused an unsafe scrape target %s: %s", url, e)
        raise HTTPException(status_code=400, detail=f"Invalid URL: {e}") from e

    try:
        scraped = await ws_client.scrape_url(url)
    except httpx.TimeoutException as e:
        logger.exception("icp-from-url: webscraper scrape timed out for %s", url)
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL (timeout): {e!r}") from e
    except httpx.HTTPStatusError as e:
        logger.exception("icp-from-url: webscraper returned %s for %s", e.response.status_code, url)
        raise upstream_http_exception(
            e,
            upstream="webscraper",
            fallback_detail=(
                f"Failed to fetch URL (webscraper returned {e.response.status_code}): {url}"
            ),
        ) from e
    except httpx.TransportError as e:
        logger.exception("icp-from-url: webscraper unreachable for %s", url)
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch URL (webscraper unreachable): {e!r}"
        ) from e
    except Exception as e:
        logger.exception("icp-from-url: webscraper scrape raised for %s", url)
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch URL ({type(e).__name__}): {e}"
        ) from e

    # webscraper answers a page it could not scrape with HTTP 200 + status "failed";
    # the client maps that onto ``.error`` (every non-2xx already raised above).
    if scraped.error:
        logger.warning(
            "icp-from-url: webscraper reported a scrape error for %s: %s", url, scraped.error
        )
        raise HTTPException(
            status_code=502, detail=f"Failed to fetch URL (scrape error): {scraped.error}"
        )
    return scraped


@router.post(
    "/brand/icp-from-url",
    response_model=ICPResponse,
    response_model_exclude_unset=True,
    responses={
        400: {"description": "URL is not a safe, public fetch target"},
        # Passed through from worker_management, not synthesized here: the tenant
        # has no LLM provider configured. The envelope's ``error.code`` carries
        # the upstream's machine-readable code (e.g. ``byo_llm_not_configured``)
        # so the SPA can link straight to Settings instead of showing a 502.
        402: {"description": "Tenant action required upstream (e.g. LLM provider not configured)"},
        422: {"description": "Validation error"},
        502: {"description": "Failed to fetch or analyze the URL"},
    },
    operation_id="analyze_icp_from_url",
)
async def extract_icp_from_url(body: ICPFromUrlRequest, request: Request) -> ICPResponse:
    """Fetch a URL and return a flat, grounded GTM/ICP record.

    Fail-closed: any fetch/LLM/shape failure raises ``502`` — a fabricated
    ``200`` is never returned. Ungrounded keys are omitted (recon reads them as
    ``unknown``).

    The one exception to "everything is a 502" is a genuinely caller-fixable
    upstream status (today only ``402``, e.g. the tenant has not configured an
    LLM provider), which is re-emitted with the upstream's actionable
    ``{code, message}`` — see :mod:`src.shared.upstream_errors`. It is still
    fail-closed: no fabricated ``200``, just an honest status.
    """
    tenant_id = _get_tenant_id(request)

    # Screen the tenant-supplied target for SSRF BEFORE anything reads or writes
    # it. ``body.url`` is a plain ``str`` on the schema and reached
    # the scraper's ``scrape_url`` untouched, so a tenant could aim this
    # service's scraper at ``http://registry.test:5000/v2/_catalog`` or
    # ``http://169.254.169.254/latest/meta-data/`` and read the answer back out
    # of the 200 — an exfiltration primitive, not merely a blind fetch.
    #
    # That the *fetch* happens inside webscraper does not move the problem.
    # webscraper's own SSRF screen lives in another repo, behind its own feature
    # switch, so it is not a backstop this service can rely on. (The retired KAG
    # hop had no screen at all.) This service accepts the attacker-controlled
    # input, so this is where it can still be answered 4xx.
    #
    # It also screens the SECOND-ORDER surface. ``_select_extra_urls`` derives
    # its same-domain filter from ``body.url``, so an unvalidated landing URL of
    # ``http://registry.test:5000/`` makes ``registry.test:5000`` the
    # "same domain" and every internal link harvested off that page becomes
    # fetchable. Validating the entry URL collapses that whole surface (and
    # ``_screen_harvested_url`` closes the residue, see its docstring).
    #
    # Inline, not off-thread: this is one URL on the request's own critical
    # path, matching ``critique/routes/jobs.py`` and ``routes/batch.py``.
    # Deliberately NOT a pydantic field validator on ``ICPFromUrlRequest`` — the
    # guard does DNS I/O and that schema is also constructed by S2S callers and
    # in tests, where a blocking resolver call during model construction would
    # be both a latency and a correctness hazard.
    #
    # Before the cache lookup, so a refused URL is refused outright rather than
    # answered from a record a previous (pre-guard) call may have left behind.
    try:
        validate_callback_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid URL: {exc}") from exc

    # Serve a prior grounded extraction for this tenant+domain without re-scraping
    # or re-invoking the LLM. Tenant isolation is inherent in the cache key.
    cached = await _check_icp_cache(request, tenant_id, body.url)
    if cached is not None:
        return cached

    wm_client = getattr(request.app.state, "wm_client", None)
    if wm_client is None:
        raise HTTPException(status_code=502, detail="LLM gateway unavailable")

    # Fetch public page content from webscraper DIRECTLY (KAG removal, 2026-10-17:
    # KAG was only ever a gateway whose scrape routes forwarded to webscraper).
    # webscraper is NOT a metered/delegating destination, so the hop carries this
    # service's licence as ``X-Tenant-Id: Bearer`` and no X-Effective-Tenant-Id;
    # identity_event_hook still stamps the vouched tenant's ids. The tenant comes
    # from identity, never from the inbound caller's raw credential
    # (request.state.license_token is None on the cell lane).
    # 0.67.0: _get_tenant_id refuses an absent/sentinel tenant with 401, so by
    # here tenant_id is always a real registered tenant. No "default" to strip.
    #
    # WEBSCRAPER_AUTH_MODE=tenant_licence (app.state.webscraper_lane, built at
    # boot): the client sends the bound tenant's OWN minted w2v licence instead,
    # and the shared token never leaves this pod. Default: the shared licence.
    try:
        ws_client = WebscraperClient(
            base_url=service_url("webscraper"),
            license_token=os.environ.get("DOWNSTREAM_LICENSE_TOKEN") or None,
            ssl_context=get_ssl_context(),
            licence_lane=getattr(request.app.state, "webscraper_lane", None),
        )
    except MissingTenantError as e:
        # Shared mode with no DOWNSTREAM_LICENSE_TOKEN: webscraper would bill an
        # unlicensed request to a default tenant, so the client refuses to exist.
        # A deployment fault, not the caller's: fail closed as a 502.
        logger.error("icp-from-url: webscraper client unavailable: %s", e)
        raise HTTPException(
            status_code=502, detail="Failed to fetch URL (scraper not configured)"
        ) from e
    # Close the client's lazily-opened httpx.AsyncClient once the request is
    # served — otherwise every /brand/icp-from-url call leaks a connection pool
    # (sockets/FDs to webscraper) until GC, exhausting the pod under load.
    try:
        scraped = await _scrape_or_raise(ws_client, body.url)

        # Enrich with pricing/about/product pages discovered on the landing page.
        # Best-effort: any failure here degrades to the landing page alone.
        picked = _select_extra_urls(body.url, scraped.links, body.max_pages)
        extra_pages = await _fetch_extra_pages(ws_client, picked)
        pages_fetched = [body.url, *(url for _, url, _ in extra_pages)]

        domain = urlparse(body.url).netloc
        merged_text = merge_page_sections(scraped.text, extra_pages)
        prompt = build_prompt(merged_text, title=scraped.title, domain=domain)
        model_override = body.llm_config.model if body.llm_config else None
        try:
            grounded, confidence, token_usage = await analyze_icp_with_llm(
                wm_client, prompt, model_override=model_override
            )
        except Exception as e:
            # NOT a blanket 502. worker_management answers 402 with a
            # machine-readable ``byo_llm_not_configured`` when the TENANT has not
            # configured an LLM provider — a condition only the caller can fix,
            # and one this route used to launder into "Bad Gateway", i.e. "the
            # upstream is broken, try later". Testers filed infrastructure bugs;
            # users never learned there was a Settings page to click.
            #
            # ``upstream_http_exception`` re-emits ONLY the statuses that still
            # mean the same thing when we say them (today: 402). Everything else
            # — 5xx, timeouts, connect errors, and 401/403/404/429, which are all
            # OUR credential/wiring/quota problems, not the caller's — still maps
            # to the original 502 with the original detail string, so real
            # upstream outages keep looking like outages and keep paging.
            raise upstream_http_exception(
                e, upstream="worker_management", fallback_detail=f"Failed to analyze URL: {e}"
            ) from e
    finally:
        await ws_client.close()

    # A VACUOUS extraction is an upstream failure, not a result. The model can
    # answer ``{}`` — schema-valid, because the ICP output schema marks every
    # key optional (``"required": []``) — and every layer below here reads that
    # as success: worker_management only calls an answer "lost" when the content
    # is None, and inference-router's ``upstream_empty_result`` detector is
    # hardcoded observe-only (``enforced=False``), so a ``{}`` is returned 200
    # with ``validation_passed=true``.
    #
    # Measured on 2026-08-26: ``icp_extractions`` id 63 (merqi.nl) is a 200 with
    # ``completion_tokens=3`` — literally ``{`` ``}`` ``<eos>`` — persisted at
    # ``confidence=0.0`` with zero grounded keys. Two earlier calls (08-23,
    # 08-25) are the same shape. All 42 rows before them carry 0.36-0.91. So the
    # empty answer is not "this site had nothing to say"; it is the extraction
    # not happening, and recon consumed it as a legitimate all-``unknown``
    # brand record.
    #
    # Returning 200 here was doubly harmful, because ``_cache_icp`` then pinned
    # the empty record under ``tenant:domain`` for ``cache.icp_ttl`` (1h): the
    # next caller got the vacuum back WITHOUT a retry, so a single bad
    # generation suppressed the whole domain for an hour. Fail closed instead,
    # exactly as this handler's docstring already promises — no cache write, no
    # audit row, and a 502 that makes the failure visible and retryable.
    if not grounded:
        raise HTTPException(
            status_code=502,
            detail=(
                f"LLM returned no grounded ICP fields for {body.url} (token_usage={token_usage})"
            ),
        )

    result = ICPResponse(
        **grounded,
        source_url=body.url,
        token_usage=token_usage,
        confidence=confidence,
        pages_fetched=pages_fetched,
    )
    await _cache_icp(request, tenant_id, body.url, result)
    # Audit-trail persistence. _get_tenant_id already refused an absent or
    # sentinel tenant with 401, so this is unconditional now.
    await _persist_icp(tenant_id, body.url, result)
    return result
