"""Which upstream statuses escape the brand pipeline vs. degrade one stage.

Background — the production defect these tests lock down::

    ERROR w2v_common.clients.base: WM worker 'brand-analysis' returned 402:
      {"error":{"code":"ERROR","message":{"code":"byo_llm_not_configured",
       "message":"Configure your LLM provider in Settings before running agents."},
       "status":402}}
    INFO: "POST /api/brand/from-url HTTP/1.1" 200 OK

Worse than the sibling ``icp-from-url`` defect (which at least answered 502): the
brand pipeline's degrade-one-stage contract swallowed the 402 inside
``BaseStage.execute``, so the caller got a **200** with a silently
brand-analysis-free brand — no tagline, no voice, no industry, no error field —
and the result was then cached for an hour. Nothing anywhere said "configure
your LLM provider".

Half of this file proves that specific case is fixed; the other half proves the
fix did NOT become "let every upstream status escape the pipeline", which would
throw away the degrade contract that keeps a stylesheet 404 or a WM outage from
502'ing an extraction that is otherwise complete.

Mirrors ``tests/unit/domains/icp/test_icp_upstream_status.py``.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from src.domains.brand.brands_routes import _extract_and_persist, compare_brands
from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.routes import (
    _CACHE_CRITICAL_STAGES,
    _cache_extraction,
    _is_cacheable,
    batch_extract,
    extract_from_url,
)
from src.domains.brand.schemas import (
    BatchExtractRequest,
    BrandComparisonRequest,
    BrandFromUrlRequest,
    BrandResponse,
    ExtractionReport,
)
from src.shared.upstream_errors import UpstreamStatusError

_REPO = "src.domains.brand.services.brand_repository.BrandRepository"

# The real worker_management body, double-wrapped exactly as production logged it.
_BYO_LLM_BODY: dict[str, Any] = {
    "error": {
        "code": "ERROR",
        "message": {
            "code": "byo_llm_not_configured",
            "message": "Configure your LLM provider in Settings before running agents.",
        },
        "status": 402,
    }
}


def _wm_status_error(status: int, body: Any = None) -> httpx.HTTPStatusError:
    """Build the ``httpx.HTTPStatusError`` ``w2v_common`` raises on a non-2xx.

    Mirrors ``w2v_common.clients.base.raise_for_status_with_body``, which attaches
    the live response — that attachment is the only reason anything downstream can
    recover the upstream's status or code.
    """
    request = httpx.Request("POST", "http://wm.test.invalid:8887/workers/brand-analysis/message")
    response = httpx.Response(status_code=status, json=body or {}, request=request)
    return httpx.HTTPStatusError(
        f"WM worker 'brand-analysis' returned {status}", request=request, response=response
    )


class _RaisingStage(BaseStage):
    """A stage whose ``run`` raises whatever it was handed."""

    name = "llm_analysis"

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        raise self._exc


async def _execute_stage(exc: BaseException) -> StageResult:
    """Run ``BaseStage.execute`` over a stage that raises *exc*."""
    ctx = PipelineContext(url="https://merqi.nl", tenant_id="df5ce213-933c-5747-9b72-840f50559ec6")
    return await _RaisingStage(exc).execute(ctx, BrandResponse())


def _make_request(brand_cache: AsyncMock, pipeline_runner: object) -> MagicMock:
    """Mock Request exposing the runner + cache the route reads off app.state."""
    request = MagicMock()
    request.state.tenant_id = "df5ce213-933c-5747-9b72-840f50559ec6"
    request.app.state.pipeline_runner = pipeline_runner
    request.app.state.brand_cache = brand_cache
    request.app.state.http_client = None
    request.app.state.logo_service = None
    request.app.state.wm_client = MagicMock()
    request.app.state.event_publisher = None
    return request


# ---------------------------------------------------------------------------
# BaseStage.execute — the escape hatch, and its fence
# ---------------------------------------------------------------------------


class TestStageExecutePassthroughEscape:
    async def test_upstream_402_escapes_the_stage(self):
        """REVERTED ⇒ the exact production defect returns.

        The degrade contract turns a caller-fixable "you have no LLM provider"
        into a 200 with a hollow brand. Nothing else in the request path can
        recover it once ``execute`` has absorbed it, so this re-raise is the
        single load-bearing line of the fix.
        """
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _execute_stage(_wm_status_error(402, _BYO_LLM_BODY))
        assert exc_info.value.response.status_code == 402


class TestStageExecuteStillDegrades:
    """The fence: everything that is not a passthrough status keeps degrading."""

    # Each of these is a status a "just let 4xx escape" refactor would wrongly
    # propagate, collapsing a 12-stage extraction into a 502 over one stage. The
    # per-status reasoning lives in the ``src.shared.upstream_errors`` module
    # docstring; this pins it so widening the set is a conscious act.
    @pytest.mark.parametrize(
        ("status", "why"),
        [
            (400, "we built the request body; losing the extraction over it is absurd"),
            (401, "OUR S2S license was rejected — the caller cannot act, keep the brand"),
            (403, "OUR identity lacks the grant — the caller cannot act, keep the brand"),
            (404, "OUR worker name is wrong; re-emitting claims the CALLER's page is missing"),
            (422, "we built the request body"),
            (429, "ambiguous tenant-quota vs shared gateway limit, and we have no Retry-After"),
            (500, "a real WM fault must not cost the caller the other 11 stages"),
            (503, "WM restarting is the textbook degrade case"),
        ],
    )
    async def test_non_passthrough_status_is_absorbed(self, status: int, why: str):
        """REVERTED ⇒ one flaky upstream call 502s an otherwise complete extraction.

        The pipeline's whole contract is *degrade one stage, keep the brand*. A
        WM 500 or 503 is an outage the caller can do nothing about; answering
        with colors, fonts, logo and no tagline is strictly better than nothing.
        """
        result = await _execute_stage(_wm_status_error(status, {"error": "nope"}))
        assert result.success is False, why
        assert result.errors, why

    async def test_transport_error_is_absorbed(self):
        """REVERTED ⇒ a connect error carries no ``.response`` and would crash.

        ``httpx.ConnectError`` has no status at all; the check must not assume a
        response is attached, and a transport fault is the canonical degrade case.
        """
        result = await _execute_stage(httpx.ConnectError("connection refused"))
        assert result.success is False
        assert result.errors

    async def test_ordinary_stage_failure_is_absorbed(self):
        """REVERTED ⇒ the fix leaked outside httpx entirely.

        ``ValueError`` covers the SSRF/size guards that MUST keep degrading (see
        ``_stage_failure_types``); if this raises, the carve-out was written as
        something broader than "an httpx status in PASSTHROUGH_STATUSES".
        """
        result = await _execute_stage(ValueError("bad colour"))
        assert result.success is False


# ---------------------------------------------------------------------------
# The route: the escaped 402 reaches the caller, and is not cached
# ---------------------------------------------------------------------------


class TestExtractFromUrlUpstreamStatus:
    @staticmethod
    async def _call_with_pipeline_error(exc: BaseException) -> tuple[HTTPException, AsyncMock]:
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=None)
        brand_cache.set = AsyncMock()
        runner = AsyncMock()
        runner.run = AsyncMock(side_effect=exc)
        request = _make_request(brand_cache, runner)
        body = BrandFromUrlRequest(url="https://merqi.nl")

        with pytest.raises(HTTPException) as exc_info:
            await extract_from_url(body=body, request=request, db=AsyncMock())
        return exc_info.value, brand_cache

    async def test_upstream_402_surfaces_as_402(self):
        """REVERTED ⇒ the escaped 402 is laundered into "Bad Gateway" at the door.

        The stage re-raise is worthless if the route's blanket ``except
        Exception`` still maps it to 502 — the tenant would once again read
        "the upstream is broken" for a problem they fix in Settings.
        """
        exc, _ = await self._call_with_pipeline_error(_wm_status_error(402, _BYO_LLM_BODY))
        assert exc.status_code == 402

    async def test_upstream_402_carries_the_actionable_code_and_message(self):
        """REVERTED ⇒ the SPA has nothing to branch on and no sentence to show.

        ``byo_llm_not_configured`` is the key that renders an "Open Settings"
        call-to-action; the message is what a human reads if it does not.
        """
        exc, _ = await self._call_with_pipeline_error(_wm_status_error(402, _BYO_LLM_BODY))
        assert isinstance(exc, UpstreamStatusError)
        assert exc.upstream_code == "byo_llm_not_configured"
        assert exc.upstream == "worker_management"
        assert "Configure your LLM provider in Settings" in str(exc.detail)

    async def test_402_is_not_cached(self):
        """REVERTED ⇒ a failed run poisons the cache.

        The route must not write anything for a run that never produced a brand.
        (The degraded-but-returned case is covered by ``TestCacheWriteGuard``.)
        """
        _, brand_cache = await self._call_with_pipeline_error(_wm_status_error(402, _BYO_LLM_BODY))
        brand_cache.set.assert_not_called()

    async def test_upstream_500_is_still_502(self):
        """REVERTED ⇒ real WM outages stop reading as gateway faults.

        Belt-and-braces at the route boundary: even if a 500 somehow escaped the
        pipeline, it must not masquerade as this service's own status.
        """
        exc, _ = await self._call_with_pipeline_error(_wm_status_error(500, {"error": "boom"}))
        assert exc.status_code == 502
        assert not isinstance(exc, UpstreamStatusError)
        assert "Failed to extract brand" in str(exc.detail)


# ---------------------------------------------------------------------------
# The cache write guard
# ---------------------------------------------------------------------------


class TestCacheWriteGuard:
    @staticmethod
    def _result(*, failed: list[str]) -> BrandResponse:
        return BrandResponse(
            name="Merqi",
            domain="merqi.nl",
            extraction_report=ExtractionReport(
                stages_run=["fetch", "llm_analysis"],
                stages_succeeded=[s for s in ("fetch", "llm_analysis") if s not in failed],
                stages_failed=failed,
            ),
        )

    async def test_degraded_llm_analysis_is_not_cached(self):
        """REVERTED ⇒ one transient WM failure serves a hollow brand for an HOUR.

        ``brand_ttl`` is 3600s. Caching a run whose ``llm_analysis`` stage failed
        means every later caller gets the tagline-less, voice-less brand from
        Redis — with the pipeline never re-run and no error to notice.
        """
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache, AsyncMock())

        await _cache_extraction(
            request, "df5ce213-933c-5747-9b72-840f50559ec6", self._result(failed=["llm_analysis"])
        )

        brand_cache.set.assert_not_called()

    async def test_clean_result_is_still_cached(self):
        """REVERTED ⇒ the guard over-fires and the cache never fills.

        Every from-url call would then pay a full Playwright fetch, which is the
        cost the cache exists to avoid. The guard is narrow on purpose.
        """
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache, AsyncMock())

        await _cache_extraction(
            request, "df5ce213-933c-5747-9b72-840f50559ec6", self._result(failed=[])
        )

        brand_cache.set.assert_called_once()

    async def test_result_without_a_report_is_still_cached(self):
        """REVERTED ⇒ the from-analysis path (``/brand/extract``) stops caching.

        ``extract_brand`` builds a ``BrandResponse`` with no pipeline and hence no
        ``extraction_report``; ``None`` is not evidence of degradation.
        """
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache, AsyncMock())

        await _cache_extraction(
            request,
            "df5ce213-933c-5747-9b72-840f50559ec6",
            BrandResponse(name="Merqi", domain="merqi.nl"),
        )

        brand_cache.set.assert_called_once()

    async def test_unrelated_failed_stage_is_still_cached(self):
        """REVERTED ⇒ the guard keys off "any failure" and empties the cache.

        A failed ``logo`` or ``fonts`` stage is the ordinary partial extraction
        this pipeline is designed to return and cache. Only the LLM stage carries
        the caller-fixable condition worth re-running for.
        """
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache, AsyncMock())

        await _cache_extraction(
            request, "df5ce213-933c-5747-9b72-840f50559ec6", self._result(failed=["logo"])
        )

        brand_cache.set.assert_called_once()


# ---------------------------------------------------------------------------
# End-to-end through the real runner: stage → runner → route
# ---------------------------------------------------------------------------


class TestPipelineRunnerDoesNotAbsorbIt:
    async def test_402_from_a_stage_reaches_the_route_as_402(self):
        """REVERTED ⇒ some layer between the stage and the route re-swallows it.

        ``PipelineRunner._execute_stages`` has its own ``result.success`` handling;
        this drives a REAL runner (not an ``AsyncMock``) so the escape is proven
        across the whole chain rather than at two mocked ends.
        """
        from src.domains.brand.pipeline.runner import PipelineRunner

        runner = PipelineRunner([_RaisingStage(_wm_status_error(402, _BYO_LLM_BODY))])
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=None)
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache, runner)
        body = BrandFromUrlRequest(url="https://merqi.nl")

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
            pytest.raises(HTTPException) as exc_info,
        ):
            await extract_from_url(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 402
        brand_cache.set.assert_not_called()


# ---------------------------------------------------------------------------
# The cache guard generalised: fetch is as fatal as llm_analysis
# ---------------------------------------------------------------------------


class TestFetchFailureIsNotCached:
    """``fetch`` failing does not cost one field — it costs the whole brand."""

    @staticmethod
    def _gutted() -> BrandResponse:
        """What ``PipelineRunner._execute_stages`` returns when ``fetch`` fails.

        It breaks out of the stage loop immediately, so the brand carries only
        ``domain`` (and ``name`` defaulted to it). Nothing else ever ran.
        """
        return BrandResponse(
            name="merqi.nl",
            domain="merqi.nl",
            extraction_report=ExtractionReport(
                stages_run=["fetch"],
                stages_succeeded=[],
                stages_failed=["fetch"],
            ),
        )

    async def test_failed_fetch_is_not_cached(self):
        """REVERTED ⇒ one Playwright timeout serves an EMPTY brand for an hour.

        The original guard only inspected ``llm_analysis``, so a domain-only
        husk went into Redis under the 3600s TTL and ``_check_url_cache`` then
        handed it to every ``/brand/from-url`` caller — the exact failure class
        the guard was written to stop, one stage to the left.
        """
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache, AsyncMock())

        await _cache_extraction(request, "df5ce213-933c-5747-9b72-840f50559ec6", self._gutted())

        brand_cache.set.assert_not_called()

    def test_the_critical_set_is_exactly_fetch_and_llm_analysis(self):
        """REVERTED ⇒ the predicate silently widened or narrowed.

        Adding a name here is meant to be a deliberate act with an argument
        attached (see the constant's comment): too narrow re-opens this bug,
        too wide empties the cache and pays a browser launch per request.
        """
        assert frozenset({"fetch", "llm_analysis"}) == _CACHE_CRITICAL_STAGES

    def test_a_one_field_stage_failure_stays_cacheable(self):
        """REVERTED ⇒ the guard keys off "any failure" and the cache never fills."""
        result = self._gutted()
        assert result.extraction_report is not None
        result.extraction_report.stages_failed = ["logo", "fonts", "social"]
        assert _is_cacheable(result) is True


# ---------------------------------------------------------------------------
# /api/brands/compare — the escaping 402 must not become a 500
# ---------------------------------------------------------------------------

_BRANDS_REPO = "src.domains.brand.brands_routes.BrandRepository"


class TestCompareRouteUpstreamStatus:
    """``httpx.HTTPStatusError`` is not an OSError/ValueError/RuntimeError.

    Its MRO is ``HTTPStatusError -> HTTPError -> Exception``, so the moment
    ``BaseStage.execute`` started re-raising a 402 instead of absorbing it, the
    ``except (OSError, ValueError, RuntimeError, TimeoutError)`` in
    ``_extract_and_persist`` stopped covering the only exception that can now
    escape ``runner.run`` — and ``compare_brands`` answered **500**.
    """

    @staticmethod
    def _request(exc: BaseException) -> MagicMock:
        runner = AsyncMock()
        runner.run = AsyncMock(side_effect=exc)
        return _make_request(AsyncMock(), runner)

    async def test_extract_and_persist_reraises_the_402_as_an_http_exception(self):
        """REVERTED ⇒ a raw ``httpx.HTTPStatusError`` escapes the route → 500."""
        with pytest.raises(HTTPException) as exc_info:
            await _extract_and_persist(
                self._request(_wm_status_error(402, _BYO_LLM_BODY)),
                AsyncMock(),
                "df5ce213-933c-5747-9b72-840f50559ec6",
                "merqi.nl",
            )

        assert exc_info.value.status_code == 402
        assert isinstance(exc_info.value, UpstreamStatusError)
        assert exc_info.value.upstream_code == "byo_llm_not_configured"

    async def test_compare_answers_402_not_500(self):
        """REVERTED ⇒ the BLOCKER returns: a BYO tenant's compare 500s.

        Repro: no cached brand for either domain, ``extract_if_missing=True``,
        tenant has no LLM provider. Was 200-with-a-degraded-brand, then 500;
        it must be the same actionable 402 ``/brand/from-url`` answers.
        """
        request = self._request(_wm_status_error(402, _BYO_LLM_BODY))
        body = BrandComparisonRequest(domain_a="a.com", domain_b="b.com", extract_if_missing=True)

        with (
            patch(f"{_BRANDS_REPO}.get_brand", new_callable=AsyncMock, return_value=None),
            patch(f"{_BRANDS_REPO}.upsert_brand", new_callable=AsyncMock),
            pytest.raises(HTTPException) as exc_info,
        ):
            await compare_brands(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 402
        assert isinstance(exc_info.value, UpstreamStatusError)
        assert "Configure your LLM provider in Settings" in str(exc_info.value.detail)

    async def test_compare_does_not_swallow_it_back_into_a_404(self):
        """REVERTED ⇒ the fix was "re-widen the except", which lies to the tenant.

        Absorbing the 402 makes ``_extract_and_persist`` return ``None`` and the
        route answer ``404 Brand not found`` — sending the tenant to look for a
        missing brand instead of to the Settings page that fixes it.
        """
        request = self._request(_wm_status_error(402, _BYO_LLM_BODY))
        body = BrandComparisonRequest(domain_a="a.com", domain_b="b.com", extract_if_missing=True)

        with (
            patch(f"{_BRANDS_REPO}.get_brand", new_callable=AsyncMock, return_value=None),
            patch(f"{_BRANDS_REPO}.upsert_brand", new_callable=AsyncMock),
            pytest.raises(HTTPException) as exc_info,
        ):
            await compare_brands(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code != 404

    async def test_non_passthrough_upstream_status_still_degrades(self):
        """REVERTED ⇒ a WM outage starts 402'ing/500'ing compares.

        A 500 from WM is not the caller's to fix; ``_extract_and_persist`` keeps
        returning ``None`` and the route keeps answering its ordinary 404.
        """
        request = self._request(_wm_status_error(500, {"error": "boom"}))
        body = BrandComparisonRequest(domain_a="a.com", domain_b="b.com", extract_if_missing=True)

        with (
            patch(f"{_BRANDS_REPO}.get_brand", new_callable=AsyncMock, return_value=None),
            patch(f"{_BRANDS_REPO}.upsert_brand", new_callable=AsyncMock),
            pytest.raises(HTTPException) as exc_info,
        ):
            await compare_brands(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# /api/brand/batch-extract — partial success survives, and nothing leaks
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _batch_persistence_patched() -> Iterator[None]:
    """Neutralise ``batch_extract``'s per-URL DB session so the suite is hermetic.

    Yields:
        Nothing; the patches are active for the block.
    """
    session = AsyncMock()
    session.commit = AsyncMock()
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_maker = MagicMock(return_value=session_cm)
    with (
        patch(
            "src.shared.database.session.get_session_maker",
            new_callable=AsyncMock,
            return_value=session_maker,
        ),
        patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
        patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
    ):
        yield


def _llm_gated_runner(exc: BaseException) -> AsyncMock:
    """A runner that raises *exc* when the LLM stage is enabled, else succeeds.

    Models the BYO tenant exactly: the page fetch, colors, fonts and logo all
    work; only ``llm_analysis`` is impossible.
    """

    async def _run(ctx: PipelineContext) -> tuple[BrandResponse, list[StageResult]]:
        if ctx.config.should_run("llm_analysis"):
            raise exc
        domain = ctx.url.removeprefix("https://")
        return BrandResponse(name=domain, domain=domain, primary_color="#123456"), []

    runner = AsyncMock()
    runner.run = _run
    return runner


class TestBatchUpstreamStatus:
    @staticmethod
    async def _run_batch(exc: BaseException, urls: list[str], brand_cache: AsyncMock):
        request = _make_request(brand_cache, _llm_gated_runner(exc))
        with _batch_persistence_patched():
            return await batch_extract(body=BatchExtractRequest(urls=urls), request=request)

    async def test_batch_keeps_the_non_llm_work(self):
        """REVERTED ⇒ a 50-URL batch goes from 50 usable brands to 0.

        ``byo_llm_not_configured`` costs a brand its tagline/voice/industry and
        nothing else — colors, fonts and logos come from the page. Turning that
        into 50 errors throws away 50 completed Playwright fetches for a
        condition that removed a handful of fields.
        """
        urls = ["https://a.com", "https://b.com", "https://c.com"]
        response = await self._run_batch(_wm_status_error(402, _BYO_LLM_BODY), urls, AsyncMock())

        assert response.succeeded == 3
        assert response.failed == 0
        assert set(response.results) == set(urls)
        assert response.results["https://a.com"].primary_color == "#123456"

    async def test_batch_reports_the_condition_once_and_structured(self):
        """REVERTED ⇒ the caller gets 50 opaque strings and never learns it was a 402.

        The batch still answers 200 (partial success is its contract), so the
        ONLY channel that can say "your LLM is unconfigured" is this field — and
        it has to carry the same machine-readable code the ``/brand/from-url``
        402 envelope carries, or the SPA cannot render one shared call-to-action.
        """
        response = await self._run_batch(
            _wm_status_error(402, _BYO_LLM_BODY),
            ["https://a.com", "https://b.com", "https://c.com"],
            AsyncMock(),
        )

        assert response.upstream_error is not None
        assert response.upstream_error.code == "byo_llm_not_configured"
        assert response.upstream_error.status == 402
        assert response.upstream_error.upstream == "worker_management"
        assert "Configure your LLM provider in Settings" in response.upstream_error.message

    async def test_batch_marks_each_degraded_result_and_does_not_cache_it(self):
        """REVERTED ⇒ LLM-less brands are cached for an hour and look complete.

        The retry SKIPS the stage, so nothing would otherwise record that it did
        not run; the result would read as a clean extraction to both the caller
        and the cache guard. One predicate decides cacheability — so the retry
        has to tell the truth on the report rather than special-case the cache.
        """
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        response = await self._run_batch(
            _wm_status_error(402, _BYO_LLM_BODY), ["https://a.com"], brand_cache
        )

        report = response.results["https://a.com"].extraction_report
        assert report is not None
        assert "llm_analysis" in report.stages_failed
        brand_cache.set.assert_not_called()

    async def test_batch_does_not_leak_the_internal_worker_url(self):
        """REVERTED ⇒ ``str(HTTPStatusError)`` puts the WM endpoint in the response.

        ``errors`` crosses a trust boundary to the caller and nothing redacts it
        on the way out, unlike the log line ``w2v_common`` writes.
        """
        response = await self._run_batch(
            _wm_status_error(500, {"error": "boom"}), ["https://a.com"], AsyncMock()
        )

        assert response.failed == 1
        blob = " ".join(response.errors.values())
        assert "wm.test.invalid" not in blob
        assert "/workers/" not in blob
        assert "500" in blob

    async def test_batch_still_absorbs_ordinary_per_url_failures(self):
        """REVERTED ⇒ the new except arm swallowed the partial-success contract."""
        response = await self._run_batch(ValueError("bad colour"), ["https://a.com"], AsyncMock())

        assert response.failed == 1
        assert response.upstream_error is None
        assert "bad colour" in response.errors["https://a.com"]
