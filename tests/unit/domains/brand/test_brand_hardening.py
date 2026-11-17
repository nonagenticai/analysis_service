"""Regression tests for the brand-domain hardening batch.

Each class below pins one defect that was live in ``src/domains/brand/``. The
"why it mattered" is recorded here rather than in a commit message because the
failure modes are all silent-until-production ones — every one of them passed
the pre-existing suite.

1. **Cross-tenant blob write.** ``LogoDownloadRequest`` carried a body-settable
   ``tenant_id``. ``LogoService.download_and_store`` writes the blob under
   ``storage.store(tenant_id, ...)`` *before* the route inserts the asset row,
   so RLS refused the row while the bytes had already landed in the victim
   tenant's storage prefix. The DB check was never the only thing guarding that
   path, and treating it as such made it a cross-tenant write primitive. The
   field was removed first, so the key was silently ignored; the model now sets
   ``extra="forbid"``, so the same request is refused out loud with a 422.

2. **Chromium fan-out.** ``/brand/batch-extract`` accepts 50 URLs and every one
   launches a headless Chromium. Ungated ``asyncio.gather`` asked the pod for 50
   browsers at once — hundreds of MB each — and nothing stopped two requests
   overlapping. The pod OOMed before the batch finished, which is strictly worse
   than the batch taking longer. The gate has since moved out of this domain to
   ``src.shared.security.browser_gate`` — critique's ``AxeAdapter`` launches its
   own Chromium in the same pod, and two per-domain ceilings of four are a
   pod-wide ceiling of eight. Its per-loop bookkeeping lives in
   ``src.shared.concurrency``, which is what these tests reset.

3. **Degrade, not hard-fail.** ``BaseStage.execute`` caught a literal tuple that
   missed ``httpx.HTTPError`` and Playwright's ``Error``; both derive straight
   from ``Exception``, not ``OSError``. A connect error on one stylesheet, or a
   navigation timeout in Chromium, escaped the stage, escaped the runner, and
   502'd an extraction that should have returned missing one field.

4. **Redis cache.** The ``redis.get`` sat *outside* the ``try``, and the guarded
   tuple did not include redis-py's own hierarchy. A Redis outage — or a single
   cache entry written by an older build of ``BrandResponse`` — propagated out of
   a best-effort lookup and 500'd the route for the whole TTL.

5. **422, not 500.** ``config`` and ``overrides`` are free-form dicts on the
   wire; the splat that constructs a model from them was the first place their
   contents were checked, and it raised straight out of the handler. A caller's
   typo was reported as "the server is broken".

6. **Dead field.** ``PipelineContext.rendered_html`` was written by nothing and
   read by nothing. Removed; guarded here so it cannot creep back.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from src.domains.brand.brands_routes import override_brand
from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.routes import batch_extract, download_logo, extract_from_url
from src.domains.brand.schemas import (
    BatchExtractRequest,
    BrandFromUrlRequest,
    BrandOverrideRequest,
    BrandResponse,
    LogoDownloadRequest,
)
from src.domains.brand.security import BrandSsrfBlockedError
from src.domains.brand.services.brand_cache_service import BrandCacheService
from src.domains.brand.stages.logo_download import LogoDownloadStage
from src.shared import concurrency
from src.shared.security import browser_gate

if TYPE_CHECKING:
    from src.domains.brand.schemas import BatchExtractResponse

# BrandRepository is imported *inside* the route functions, so it must be
# patched where it lives, not where it is used.
_REPO = "src.domains.brand.services.brand_repository.BrandRepository"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_request(
    tenant_id: str = "2dc05ea5-72a3-582c-adcf-71dd7954febb",
    pipeline_runner: object | None = None,
    logo_service: AsyncMock | None = None,
) -> MagicMock:
    """Build a mock ``Request`` whose ``app.state`` services are all explicit.

    Every optional service is set to ``None`` unless supplied. A bare
    ``MagicMock`` would hand the route a truthy non-awaitable for
    ``brand_cache`` / ``event_publisher``, and the resulting ``TypeError`` would
    be absorbed as a per-URL extraction failure — a test that fails for a reason
    the test is not about.

    Args:
        tenant_id: Value placed on ``request.state.tenant_id``.
        pipeline_runner: Object exposing ``run(ctx)``; a bare mock if omitted.
        logo_service: Optional logo service double.

    Returns:
        A mock request suitable for calling a brand route handler directly.
    """
    request = MagicMock()
    request.state.tenant_id = tenant_id
    request.app.state.pipeline_runner = pipeline_runner or MagicMock()
    request.app.state.http_client = None
    request.app.state.wm_client = None
    request.app.state.logo_service = logo_service
    request.app.state.brand_cache = None
    request.app.state.event_publisher = None
    return request


@contextlib.contextmanager
def _batch_persistence_patched() -> Iterator[None]:
    """Neutralise the per-URL DB write inside ``batch_extract``.

    ``_extract_one`` opens its own session per URL. These tests are about the
    concurrency gate and the error contract, not persistence, and the suite must
    stay hermetic.

    Yields:
        Nothing; the patches are active for the duration of the block.
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


# ---------------------------------------------------------------------------
# 1. Tenant isolation on the logo-download path
# ---------------------------------------------------------------------------


class TestLogoDownloadTenantIsolation:
    """The logo blob's storage prefix is not negotiable from the request body."""

    def test_request_model_has_no_tenant_field(self) -> None:
        """The field is gone from the model, not merely ignored by the route."""
        assert "tenant_id" not in LogoDownloadRequest.model_fields

    def test_body_supplied_tenant_is_rejected_not_silently_dropped(self) -> None:
        """A foreign ``tenant_id`` in the body is now a 422, not a silent drop.

        The invariant has never changed: **a stale or hostile client cannot
        smuggle a tenant through this body.** What changed is how loudly that is
        enforced. The model used to inherit pydantic's default
        ``extra="ignore"``, so the key was discarded and the request otherwise
        succeeded — correct, but indistinguishable from a request that never
        sent it, which is exactly the shape you cannot see in a log. With
        ``model_config = ConfigDict(extra="forbid")`` the same request fails
        validation, FastAPI answers 422, and the caller is told its field is
        gone instead of quietly having it ignored.

        Asserted rather than assumed because the failure mode is one edit away
        in either direction: dropping the ``model_config`` restores the silent
        path, and ``extra="allow"`` would bring the attribute back into
        existence and put the original cross-tenant write one ``getattr`` away.
        """
        # Built through ``model_validate`` so the *extra* key is genuinely extra —
        # passing it as a kwarg would be a static type error, not the runtime
        # attack shape an old/hostile client actually sends.
        with pytest.raises(ValidationError) as exc_info:
            LogoDownloadRequest.model_validate(
                {
                    "url": "https://example.com/logo.png",
                    "tenant_id": "f79ccddd-b28b-53fd-b848-d83ac2511784",
                }
            )

        errors = exc_info.value.errors()
        assert [error["type"] for error in errors] == ["extra_forbidden"]
        assert errors[0]["loc"] == ("tenant_id",)

    def test_the_legitimate_body_still_validates(self) -> None:
        """``extra="forbid"`` must reject the smuggled key, not the whole model.

        Without this control the test above would still pass if someone made
        ``LogoDownloadRequest`` reject everything.
        """
        body = LogoDownloadRequest.model_validate({"url": "https://example.com/logo.png"})

        assert not hasattr(body, "tenant_id")
        assert "tenant_id" not in body.model_dump()

    @pytest.mark.asyncio
    async def test_download_stores_under_authenticated_tenant(self) -> None:
        """The storage write sources its tenant from the request identity.

        This asserts the end-to-end outcome — which tenant's prefix the blob
        lands in — rather than the shape of the model. The body deliberately
        carries no tenant at all: with ``extra="forbid"`` the smuggling shape
        can no longer even be constructed (that is
        ``test_body_supplied_tenant_is_rejected_not_silently_dropped``), so the
        remaining question is whether the route reads the tenant from
        ``request.state`` or from somewhere a caller can reach. Only the former
        keeps the write inside the caller's own prefix.
        """
        asset = MagicMock()
        asset.asset_id = "asset-1"
        asset.stored_path = "/blobs/2dc05ea5-72a3-582c-adcf-71dd7954febb/asset-1.png"
        asset.content_type = "image/png"
        asset.size_bytes = 128
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(return_value=asset)

        request = _make_request(logo_service=logo_service)
        body = LogoDownloadRequest.model_validate({"url": "https://example.com/logo.png"})

        with patch(f"{_REPO}.save_logo_asset", new_callable=AsyncMock):
            await download_logo(body=body, request=request, db=AsyncMock())

        logo_service.download_and_store.assert_called_once_with(
            "https://example.com/logo.png", "2dc05ea5-72a3-582c-adcf-71dd7954febb"
        )
        _, called_tenant = logo_service.download_and_store.call_args[0]
        assert called_tenant != "f79ccddd-b28b-53fd-b848-d83ac2511784"


# ---------------------------------------------------------------------------
# 2. Batch concurrency gate
# ---------------------------------------------------------------------------


class _ConcurrencyProbe:
    """A ``runner.run`` stand-in that takes a real browser slot and records peak.

    It acquires ``browser_gate.browser_slot()`` itself, which is precisely what
    ``FetchStage.run`` does around ``pw.chromium.launch``. That is what makes
    this a test of the shipped gate rather than of a mock: the route is not
    where the bound lives any more, so a probe that skipped the slot would
    measure nothing.

    The gate bounds work-in-flight, not elapsed time, so that is what is
    measured: a counter incremented on entry, decremented on exit, with a
    high-water mark. No wall-clock assertion is derived from it — timing would
    be flaky on a loaded runner and would not describe the invariant anyway.
    """

    def __init__(self, fail_on: str = "") -> None:
        self.active = 0
        self.peak = 0
        self.started: list[str] = []
        self._fail_on = fail_on

    async def run(self, ctx: PipelineContext) -> tuple[BrandResponse, list[object]]:
        """Occupy a slot across several event-loop yields, then return a brand.

        Args:
            ctx: The pipeline context the route built for this URL.

        Returns:
            A minimal successful extraction result plus an empty stage list.

        Raises:
            RuntimeError: when the URL matches ``fail_on`` — an off-tuple error,
                used to prove a failing URL still releases its slot.
        """
        async with browser_gate.browser_slot():
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.append(ctx.url)
            try:
                # Yield repeatedly so siblings genuinely interleave; without a
                # yield the coroutine would run to completion and every peak
                # would be 1, making the bound look respected for the wrong
                # reason.
                for _ in range(5):
                    await asyncio.sleep(0)
                if self._fail_on and self._fail_on in ctx.url:
                    raise RuntimeError("off-tuple stage explosion")
            finally:
                self.active -= 1
        return BrandResponse(name="ok", domain=ctx.url.removeprefix("https://")), []


async def _run_batch(
    monkeypatch: pytest.MonkeyPatch,
    probe: _ConcurrencyProbe,
    urls: list[str],
    bound: int,
) -> BatchExtractResponse:
    """Run ``batch_extract`` with the process-wide gate pinned to *bound*.

    Both the constant and the cached semaphore must be dealt with.
    ``loop_semaphore`` reads its *limit* only when it builds the semaphore, and
    caches it per running loop — so lowering the constant alone would be
    silently ignored on a loop that had already built one at the real default,
    and the test would measure the shipped bound while claiming to measure
    *bound*. ``reset_for_testing()`` exists precisely for this and replaces the
    old "swap the map object" trick, which reached into a private the gate no
    longer owns (the bookkeeping moved to ``src.shared.concurrency``).

    Args:
        monkeypatch: pytest patcher.
        probe: The instrumented ``runner.run`` replacement.
        urls: URLs to submit.
        bound: Value to force into ``MAX_CONCURRENT_BROWSERS``.

    Returns:
        The batch response the route produced.
    """
    monkeypatch.setattr(browser_gate, "MAX_CONCURRENT_BROWSERS", bound)
    concurrency.reset_for_testing()

    runner = MagicMock()
    runner.run = probe.run
    request = _make_request(pipeline_runner=runner)

    with _batch_persistence_patched():
        return await batch_extract(body=BatchExtractRequest(urls=urls), request=request)


class TestBatchConcurrencyGate:
    """More URLs than slots must never mean more browsers than slots."""

    @pytest.mark.asyncio
    async def test_peak_concurrency_never_exceeds_the_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Eight URLs through a gate of two never put three pipelines in flight."""
        urls = [f"https://site-{i}.com" for i in range(8)]
        probe = _ConcurrencyProbe()

        result = await _run_batch(monkeypatch, probe, urls, bound=2)

        assert probe.peak <= 2
        # ...and concurrency really happened: a peak of 1 would satisfy the
        # bound trivially and prove nothing about the semaphore.
        assert probe.peak > 1
        assert result.succeeded == 8
        assert len(probe.started) == 8

    @pytest.mark.asyncio
    async def test_probe_observes_a_higher_peak_when_the_bound_is_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control for the instrument itself.

        The same workload through a wide gate must show a peak the narrow gate
        forbade. Without this, a probe that could only ever report 2 would make
        the test above pass no matter what the semaphore did.
        """
        urls = [f"https://site-{i}.com" for i in range(8)]
        probe = _ConcurrencyProbe()

        await _run_batch(monkeypatch, probe, urls, bound=8)

        assert probe.peak > 2

    @pytest.mark.asyncio
    async def test_gate_preserves_per_url_failure_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One bad URL is recorded as an error; the rest still complete.

        The gate wraps ``_extract_one`` from the outside precisely so the
        per-URL error contract is unchanged, and ``return_exceptions=True``
        remains the backstop. A failing URL must also release its slot — if it
        did not, the batch would deadlock rather than fail, so the fact that
        every remaining URL runs is the release assertion.
        """
        urls = [
            "https://good-0.com",
            "https://good-1.com",
            "https://bad.com",
            "https://good-2.com",
            "https://good-3.com",
            "https://good-4.com",
        ]
        probe = _ConcurrencyProbe(fail_on="bad")

        result = await _run_batch(monkeypatch, probe, urls, bound=2)

        assert probe.peak <= 2
        assert result.total == 6
        assert result.succeeded == 5
        assert result.failed == 1
        assert "https://bad.com" in result.errors
        assert "off-tuple stage explosion" in result.errors["https://bad.com"]
        for url in urls:
            if url != "https://bad.com":
                assert url in result.results

    def test_gate_is_process_wide_not_per_request(self) -> None:
        """Slots taken by one caller are unavailable to the next one.

        A per-request gate caps one caller at four browsers and ten callers at
        forty — the same OOM with more steps. Asserted through ``browser_slot``
        itself rather than by comparing two ``loop_semaphore`` lookups for
        identity: the identity check passes for any registry keyed the way this
        one is, including a hypothetical one whose slots were somehow not shared,
        whereas saturating the gate and then failing to enter it once more is the
        property callers actually depend on.

        The timeout only ever decides the *passing* direction. A broken,
        per-request gate hands the extra slot over immediately and the probe
        returns ``False`` without waiting, so a loaded runner cannot turn a real
        failure into a pass.

        The gate lives at the browser launch, not on the batch route: every
        entry point (``/brand/from-url``, ``/brands/compare``) runs the same
        ``FetchStage``, and critique's ``AxeAdapter`` is not a brand route at
        all, so gating one route would leave the rest unbounded.
        """

        async def _saturate_then_probe() -> bool:
            """Fill every slot, then try to take one more.

            Returns:
                ``True`` if the extra acquisition blocked, i.e. the gate is
                shared rather than per-caller.
            """
            async with contextlib.AsyncExitStack() as stack:
                for _ in range(browser_gate.MAX_CONCURRENT_BROWSERS):
                    await stack.enter_async_context(browser_gate.browser_slot())
                try:
                    async with asyncio.timeout(0.25), browser_gate.browser_slot():
                        return False
                except TimeoutError:
                    return True
            return False  # pragma: no cover - the block above always returns

        concurrency.reset_for_testing()
        assert asyncio.run(_saturate_then_probe()) is True


# ---------------------------------------------------------------------------
# 3. Stage failures degrade instead of failing the run
# ---------------------------------------------------------------------------


class _RaisingStage(BaseStage):
    """A stage whose only behaviour is to raise a caller-chosen exception."""

    name = "raiser"

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def run(self, ctx: PipelineContext, brand: BrandResponse, result: StageResult) -> None:
        """Raise the configured exception.

        Args:
            ctx: Unused pipeline context.
            brand: Unused brand accumulator.
            result: Unused stage result.

        Raises:
            Exception: Always — whatever was passed to ``__init__``.
        """
        raise self._exc


class TestStageFailuresDegrade:
    """``BaseStage.execute`` converts library errors into a failed stage."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("stylesheet host unreachable"),
            httpx.ReadTimeout("stylesheet read timed out"),
            PlaywrightError("Timeout 30000ms exceeded navigating to url"),
            TimeoutError("builtin timeout"),
        ],
        ids=["httpx-connect", "httpx-timeout", "playwright", "builtin-timeout"],
    )
    async def test_library_errors_become_a_failed_stage_result(self, exc: Exception) -> None:
        """None of these propagate; each is recorded on the stage result.

        ``httpx.HTTPError`` and Playwright's ``Error`` both derive from
        ``Exception`` and NOT from ``OSError``, which is exactly why the old
        literal tuple missed them. The Playwright class is imported from the
        real package (a hard dependency in ``pyproject.toml``) rather than
        stood in for — a stand-in subclass would still pass if the production
        code stopped naming Playwright's class at all.
        """
        stage = _RaisingStage(exc)

        result = await stage.execute(PipelineContext(url="https://example.com"), BrandResponse())

        assert result.success is False
        assert len(result.errors) == 1
        assert result.errors[0].stage == "raiser"

    @pytest.mark.asyncio
    async def test_ssrf_block_is_still_absorbed(self) -> None:
        """``BrandSsrfBlockedError`` must keep degrading — that is deliberate.

        It subclasses ``ValueError`` so a second-order URL harvested from a
        fetched page (a stylesheet ``href``, a logo ``src``) fails the stage that
        found it rather than the whole extraction. Widening the net must not
        have narrowed this.
        """
        stage = _RaisingStage(BrandSsrfBlockedError("refusing to fetch 169.254.169.254"))

        result = await stage.execute(PipelineContext(url="https://example.com"), BrandResponse())

        assert result.success is False
        assert isinstance(BrandSsrfBlockedError("x"), ValueError)

    @pytest.mark.asyncio
    async def test_programming_errors_still_propagate(self) -> None:
        """The net is a named tuple, not ``except Exception``.

        A typo'd attribute is a bug in this repo and must fail loudly in CI
        rather than be filed as a degraded stage forever.
        """
        stage = _RaisingStage(AttributeError("BrandResponse has no attribute 'colour'"))

        with pytest.raises(AttributeError):
            await stage.execute(PipelineContext(url="https://example.com"), BrandResponse())

    @pytest.mark.asyncio
    async def test_logo_download_stage_warns_on_http_error(self) -> None:
        """A transport failure on the logo URL is a warning, not a stage error.

        A logo is a nice-to-have on a brand record; the extraction still has
        colours, fonts and metadata. Before ``httpx.HTTPError`` joined this
        stage's tuple, a connect error on the logo host failed the extraction.
        """
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(
            side_effect=httpx.ConnectError("logo host unreachable")
        )
        ctx = PipelineContext(
            url="https://example.com",
            tenant_id="748b2bfc-a2b7-51e3-8ee8-cfab0ec02ef7",
            logo_service=logo_service,
            download_logo=True,
        )
        brand = BrandResponse(logo_url="https://cdn.example.com/logo.png")
        result = StageResult(stage_name="logo_download")

        await LogoDownloadStage().run(ctx, brand, result)

        assert result.success is True
        assert len(result.warnings) == 1
        assert result.errors == []


# ---------------------------------------------------------------------------
# 4. Redis cache degrades to a miss
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_double() -> AsyncMock:
    """A redis client double with the four methods the brand cache calls.

    Returns:
        An ``AsyncMock`` pre-wired for the healthy path; individual tests
        override one method with a failure.
    """
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=True)
    redis.exists = AsyncMock(return_value=True)
    return redis


@pytest.fixture
def enabled_cache(redis_double: AsyncMock) -> BrandCacheService:
    """A ``BrandCacheService`` with caching explicitly enabled.

    The suite's global env sets ``CACHE_ENABLED=false``, which would short-
    circuit every method before the guarded block and make these tests pass
    without ever touching the code under test.

    Args:
        redis_double: The client double to wrap.

    Returns:
        A cache service whose settings report the cache as enabled.
    """
    with patch("src.domains.brand.services.brand_cache_service.get_settings") as settings:
        cache_settings = type(
            "CacheSettings", (), {"brand_ttl": 3600, "enabled": True, "key_prefix": "brand"}
        )()
        settings.return_value = type("Settings", (), {"cache": cache_settings})()
        return BrandCacheService(redis_client=redis_double)


class TestBrandCacheFailSoft:
    """Every cache round-trip degrades; none of them reach the caller."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            RedisError("generic redis failure"),
            RedisConnectionError("Error 111 connecting to redis:6379"),
            OSError("socket closed under redis-py"),
            TypeError("expected bytes-like, got MagicMock"),
        ],
        ids=["redis-error", "redis-connection-error", "oserror", "typeerror"],
    )
    async def test_get_returns_none_when_the_client_raises(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock, failure: Exception
    ) -> None:
        """The read itself is inside the ``try`` now — it used to sit outside it.

        ``redis.exceptions.ConnectionError`` is a ``RedisError`` and NOT the
        builtin ``ConnectionError``, so the pre-fix handler caught nothing a real
        outage raises. That is the case pinned explicitly here.
        """
        redis_double.get = AsyncMock(side_effect=failure)

        assert (
            await enabled_cache.get("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com") is None
        )

    @pytest.mark.asyncio
    async def test_get_returns_none_on_schema_drifted_entry(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock
    ) -> None:
        """An entry an older build wrote must expire, not become an outage.

        One un-parseable key used to 500 the route for the whole TTL.
        """
        redis_double.get = AsyncMock(return_value={"fonts": "not-a-list", "favicon_urls": 7})

        assert (
            await enabled_cache.get("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com") is None
        )

    @pytest.mark.asyncio
    async def test_get_returns_none_on_wholly_wrong_payload_type(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock
    ) -> None:
        """A payload that is not a mapping at all is a miss, not a crash.

        ``model_validate`` reports a non-mapping input as a ``ValidationError``
        rather than a ``TypeError``; ``TypeError`` is covered separately at the
        client boundary above. Both are in the guarded tuple, and the observable
        contract here is the same either way: ``None``.
        """
        redis_double.get = AsyncMock(return_value=["not", "a", "brand"])

        assert (
            await enabled_cache.get("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com") is None
        )

    @pytest.mark.asyncio
    async def test_get_by_url_degrades_too(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock
    ) -> None:
        """The URL-keyed wrapper inherits the same fail-soft behaviour."""
        redis_double.get = AsyncMock(side_effect=RedisError("down"))

        assert (
            await enabled_cache.get_by_url(
                "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "https://www.example.com/page"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_set_returns_false_when_redis_is_down(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock
    ) -> None:
        """Failing to populate a cache is never a reason to fail the request.

        The value has already been computed by the time ``set`` is reached.
        """
        redis_double.set = AsyncMock(side_effect=RedisError("read-only replica"))

        assert (
            await enabled_cache.set(
                "2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com", BrandResponse()
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_invalidate_returns_false_when_redis_is_down(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock
    ) -> None:
        """The triggering write is already committed; raising would misreport it.

        The stale entry still expires on its own TTL.
        """
        redis_double.delete = AsyncMock(side_effect=RedisError("down"))

        assert (
            await enabled_cache.invalidate("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
            is False
        )

    @pytest.mark.asyncio
    async def test_exists_returns_false_when_redis_is_down(
        self, enabled_cache: BrandCacheService, redis_double: AsyncMock
    ) -> None:
        """Cannot-tell and not-cached lead the caller to the same place: recompute."""
        redis_double.exists = AsyncMock(side_effect=OSError("socket closed"))

        assert (
            await enabled_cache.exists("2e960681-0187-5dad-ba0a-2b95a67ecfaa", "example.com")
            is False
        )


# ---------------------------------------------------------------------------
# 5. Bad free-form dicts are 422, not 500
# ---------------------------------------------------------------------------


class TestFreeFormDictValidationMapping:
    """A caller's typo must not be reported as a server fault."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "config",
        [
            {"timeout": -5.0},
            {"confidence_threshold": 4.2},
            {"max_css_stylesheets": "ten"},
            {"methods": "html_meta"},
        ],
        ids=["timeout-below-gt", "threshold-above-le", "int-field-is-str", "list-field-is-str"],
    )
    async def test_bad_from_url_config_is_422(self, config: dict[str, object]) -> None:
        """``BrandExtractConfig(**body.config)`` is the first check of the dict.

        ``config`` is free-form on the wire, so the splat is where its contents
        are first validated; unguarded it raised ``ValidationError`` straight out
        of the handler and FastAPI turned that into a 500.
        """
        runner = AsyncMock()
        runner.run = AsyncMock(return_value=(BrandResponse(domain="x.com"), []))
        request = _make_request(pipeline_runner=runner)
        body = BrandFromUrlRequest(url="https://example.com", config=config)

        with pytest.raises(HTTPException) as exc_info:
            await extract_from_url(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_config_still_runs(self) -> None:
        """The guard must not have turned every config into a rejection."""
        runner = AsyncMock()
        runner.run = AsyncMock(return_value=(BrandResponse(name="Ok", domain="example.com"), []))
        request = _make_request(pipeline_runner=runner)
        body = BrandFromUrlRequest(url="https://example.com", config={"max_css_stylesheets": 3})

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await extract_from_url(body=body, request=request, db=AsyncMock())

        assert result.name == "Ok"

    @pytest.mark.asyncio
    async def test_bad_override_value_type_is_422(self) -> None:
        """``BrandOverride(**body.overrides)`` rejects a wrongly-typed field."""
        body = BrandOverrideRequest(domain="example.com", overrides={"name": 123})

        with pytest.raises(HTTPException) as exc_info:
            await override_brand(
                domain="example.com", body=body, request=_make_request(), db=AsyncMock()
            )

        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_non_str_override_key_is_422_not_500(self) -> None:
        """A non-string key raises ``TypeError`` from the splat, before pydantic.

        ``BrandOverrideRequest.overrides`` is an unparameterised ``dict``, so a
        JSON object with a non-string key survives request validation and
        reaches the splat — which raises ``TypeError: keywords must be strings``
        rather than a ``ValidationError``. Catching only ``ValidationError`` here
        would still have produced a 500, which is why the handler catches both.
        """
        body = BrandOverrideRequest(domain="example.com", overrides={1: "acme"})

        with pytest.raises(HTTPException) as exc_info:
            await override_brand(
                domain="example.com", body=body, request=_make_request(), db=AsyncMock()
            )

        assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# 6. Dead field stays dead
# ---------------------------------------------------------------------------


class TestPipelineContextHasNoDeadField:
    """``rendered_html`` was written by nothing and read by nothing."""

    def test_rendered_html_is_not_an_attribute(self) -> None:
        """An always-empty field on a shared state bag reads as available data.

        Every stage that could have consumed it instead used ``ctx.html``, so
        the field's only effect was to invite a future stage to read a value
        that is never populated.
        """
        ctx = PipelineContext(url="https://example.com")

        assert not hasattr(ctx, "rendered_html")

    def test_rendered_html_is_not_a_dataclass_field(self) -> None:
        """Belt-and-braces: absent from the declaration, not just from a default."""
        assert "rendered_html" not in {f.name for f in dataclasses.fields(PipelineContext)}
