"""Pins that each repaired ``except`` clause catches what the library really raises.

Every test here would have passed against the *old* tuple only if the old tuple
worked. That is the point: each one raises the concrete class the real dependency
throws -- ``redis.exceptions.ConnectionError``, ``httpx.ConnectError``,
``httpx.ReadTimeout`` -- rather than a builtin chosen to match the handler.

The bug class being pinned is an ``except`` clause naming exceptions the guarded
call can never raise, which makes the guard INERT and lets the real error escape:

* redis-py defines its OWN ``ConnectionError`` and ``TimeoutError`` under
  ``RedisError``. They shadow the builtin names at a call site but are unrelated
  to them, so ``except (OSError, ConnectionError, TimeoutError)`` written next to
  a ``redis`` import catches nothing a Redis outage raises.
* NOTHING in the httpx tree derives from ``OSError`` or ``RuntimeError``.
  ``ConnectError``, ``ReadTimeout``, ``RemoteProtocolError`` and friends all hang
  off ``httpx.HTTPError -> Exception``, and ``httpx.TimeoutException`` is not the
  builtin ``TimeoutError``.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

from src.domains.brand.brands_routes import override_brand
from src.domains.brand.routes import _cache_extraction, _publish_extraction_event
from src.domains.brand.schemas import BrandOverrideRequest, BrandResponse
from src.domains.brand.services.event_publisher import BrandEventPublisher
from src.domains.critique.services.scanners.security_scanner import SecurityScanner
from src.domains.review.routes import request_review_video

#: Public literal, so nothing here needs a name lookup. TEST-NET-3 is avoided
#: deliberately: CPython reports it as private and the scanner's SSRF guard would
#: refuse it before any handler under test could run.
PUBLIC_HOST = "8.8.8.8"

#: The module logger the brand-route guards degrade into. Named, not derived
#: from ``__name__``, so a module move breaks the test loudly instead of
#: silently capturing nothing and letting a re-broken guard read as green.
_BRAND_ROUTES_LOGGER = "src.domains.brand.routes"

Responder = Callable[[httpx.Request], httpx.Response]


def _install_transport(monkeypatch: pytest.MonkeyPatch, responder: Responder) -> None:
    """Route every ``httpx.AsyncClient`` built anywhere through *responder*.

    Mirrors the seam ``test_redirect_chain_stop_is_reported.py`` already uses:
    the scanner modules do ``import httpx`` and construct clients inline, so the
    class on the shared module object is the interception point.

    Args:
        monkeypatch: Undoes the rebind on teardown.
        responder: Handler turning an ``httpx.Request`` into a response, or
            raising to simulate a transport failure.
    """
    transport = httpx.MockTransport(responder)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


def _refuse_connection(request: httpx.Request) -> httpx.Response:
    """Fail every request the way an unreachable origin really does."""
    raise httpx.ConnectError("connection refused", request=request)


def _make_request(
    tenant_id: str = "df5ce213-933c-5747-9b72-840f50559ec6", app_state: dict | None = None
) -> MagicMock:
    """Build a mock ``Request`` with chosen ``app.state`` attributes."""
    request = MagicMock()
    request.state.tenant_id = tenant_id
    for key, val in (app_state or {}).items():
        setattr(request.app.state, key, val)
    return request


def _make_brand_record(**overrides) -> MagicMock:
    """Build a mock DB brand record with sensible defaults."""
    record = MagicMock()
    defaults = {
        "name": "Acme Corp",
        "domain": "acme.com",
        "logo_url": "https://acme.com/logo.png",
        "primary_color": "#112233",
        "secondary_color": "#445566",
        "has_logo": True,
        "tagline": "We make things",
        "brand_voice": "confident",
        "version": 3,
    }
    defaults.update(overrides)
    for key, val in defaults.items():
        setattr(record, key, val)
    return record


# ---------------------------------------------------------------------------
# Redis publish guards -- ``RedisClient.publish`` does not swallow ``RedisError``
# ---------------------------------------------------------------------------


class TestBrandEventPublisherGuard:
    """``BrandEventPublisher.publish`` must degrade, not raise, on a dead Redis."""

    async def test_redis_connection_error_is_reported_not_raised(self) -> None:
        """A redis-py ``ConnectionError`` returns ``False`` instead of escaping.

        Against the old ``(OSError, ConnectionError, TimeoutError)`` tuple the
        ``ConnectionError`` name resolved to the BUILTIN, which
        ``redis.exceptions.ConnectionError`` does not inherit from, so this call
        raised.
        """
        redis = AsyncMock()
        redis.publish = AsyncMock(side_effect=RedisConnectionError("Connection refused"))

        published = await BrandEventPublisher(redis_client=redis).publish_extraction(
            domain="acme.com", tenant_id="t1", brand={"name": "Acme"}
        )

        assert published is False
        redis.publish.assert_awaited_once()

    async def test_unserialisable_payload_is_reported_not_raised(self) -> None:
        """``json.dumps`` failing inside the ``try`` is a cache-shaped failure too."""
        redis = AsyncMock()

        class _Unserialisable:
            def __str__(self) -> str:
                raise TypeError("no string form")

        published = await BrandEventPublisher(redis_client=redis).publish_extraction(
            domain="acme.com", tenant_id="t1", brand={"bad": _Unserialisable()}
        )

        assert published is False

    async def test_programming_error_still_surfaces(self) -> None:
        """The guard is a tuple, not ``except Exception``: bugs still escape."""
        redis = AsyncMock()
        redis.publish = AsyncMock(side_effect=AttributeError("renamed field"))

        with pytest.raises(AttributeError):
            await BrandEventPublisher(redis_client=redis).publish_extraction(
                domain="acme.com", tenant_id="t1", brand={"name": "Acme"}
            )


class TestBrandRoutesEventGuard:
    """``POST /api/brand/from-url`` must not 500 because Pub/Sub is down."""

    async def test_publish_failure_does_not_escape_the_route_helper(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        publisher = AsyncMock()
        publisher.publish_extraction = AsyncMock(
            side_effect=RedisConnectionError("Connection refused")
        )
        request = _make_request(app_state={"event_publisher": publisher})
        result = BrandResponse(name="Acme", domain="acme.com")

        with caplog.at_level(logging.DEBUG, logger=_BRAND_ROUTES_LOGGER):
            await _publish_extraction_event(request, "t1", result)

        # The publish was attempted with the extraction the caller just paid for
        # -- domain, tenant and the serialised brand, with the absent
        # ``metadata`` scoring 0.0 quality -- and the redis-py failure was
        # swallowed into a log line rather than 500ing a request whose
        # extraction had already succeeded.
        publisher.publish_extraction.assert_awaited_once_with(
            "acme.com", "t1", result.model_dump(mode="json"), 0.0
        )
        assert "Connection refused" in caplog.text

    async def test_cache_failure_does_not_escape_the_route_helper(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``brand_cache.set`` guard.

        ``BrandCacheService.set`` already swallows ``RedisError`` internally and
        reports failure as a return value, so this guard is REDUNDANT in
        production rather than dangerous. It is still exercised here because the
        facade is duck-typed on ``app.state`` and a stub that does raise must not
        take the request down.
        """
        cache = AsyncMock()
        cache.set = AsyncMock(side_effect=RedisConnectionError("Connection refused"))
        request = _make_request(app_state={"brand_cache": cache})
        result = BrandResponse(name="Acme", domain="acme.com")

        with caplog.at_level(logging.DEBUG, logger=_BRAND_ROUTES_LOGGER):
            await _cache_extraction(request, "t1", result)

        # A result with no ``extraction_report`` is cacheable, so the write is
        # attempted under the caller's tenant and domain; the raise is absorbed
        # into a log line instead of reaching the route.
        cache.set.assert_awaited_once_with("t1", "acme.com", result)
        assert "Connection refused" in caplog.text


class TestBrandOverridePublishGuard:
    """The worst of the three: the DB upsert has ALREADY committed."""

    async def test_override_returns_200_when_pubsub_is_down(self) -> None:
        """A dead Redis must not turn a landed write into a 500 for the caller."""
        publisher = AsyncMock()
        publisher.publish_override = AsyncMock(
            side_effect=RedisConnectionError("Connection refused")
        )

        with (
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand",
                new_callable=AsyncMock,
                return_value=_make_brand_record(),
            ),
            patch(
                "src.domains.brand.brands_routes.BrandRepository.upsert_brand",
                new_callable=AsyncMock,
                return_value=_make_brand_record(version=4),
            ),
        ):
            request = _make_request(app_state={"event_publisher": publisher})
            body = BrandOverrideRequest(domain="acme.com", overrides={"name": "New Acme"})
            result = await override_brand(
                domain="acme.com", body=body, request=request, db=AsyncMock()
            )

        assert result.new_version == 4
        publisher.publish_override.assert_awaited_once()


# ---------------------------------------------------------------------------
# httpx guards
# ---------------------------------------------------------------------------


class TestReviewVideoCompilerGuard:
    """An unreachable video_compiler is a 503, which is what the docstring says."""

    async def _call(self, exc: Exception) -> HTTPException:
        job = MagicMock()
        job.status = "completed"
        job.results = {"design_review": {"overall_score": 7.5}}
        job.domain = "example.com"
        job.job_id = "j1"

        client = AsyncMock()
        client.request_findings_video = AsyncMock(side_effect=exc)
        client.close = AsyncMock()

        with (
            patch("src.domains.review.routes.JobManager") as job_manager,
            patch("src.domains.review.routes.get_settings") as get_settings,
            patch(
                "src.domains.review.services.findings.extract_findings",
                return_value=[{"severity": "high", "description": "Low contrast"}],
            ),
            patch(
                "src.domains.review.services.video_client.VideoCompilerClient",
                return_value=client,
            ),
            patch("w2v_common.discovery.service_url", return_value="http://video:6309"),
        ):
            job_manager.get_job = AsyncMock(return_value=job)
            get_settings.return_value = MagicMock(
                video_compiler=MagicMock(enabled=True, timeout=300)
            )
            with pytest.raises(HTTPException) as exc_info:
                await request_review_video(job_id="j1", request=_make_request(), db=AsyncMock())
        return exc_info.value

    async def test_transport_error_is_a_503(self) -> None:
        """``httpx.ConnectError`` is a ``TransportError``, documented as raised.

        It derives from ``httpx.HTTPError``, NOT from ``OSError`` or
        ``RuntimeError``, so the old builtin-only tuple let it escape as a 500.
        """
        raised = await self._call(
            httpx.ConnectError("connection refused", request=httpx.Request("POST", "http://v/"))
        )
        assert raised.status_code == 503
        assert "Video compiler unavailable" in str(raised.detail)

    async def test_http_status_error_is_a_503(self) -> None:
        """The other class the client's docstring names."""
        request = httpx.Request("POST", "http://v/")
        raised = await self._call(
            httpx.HTTPStatusError(
                "500 Server Error", request=request, response=httpx.Response(500, request=request)
            )
        )
        assert raised.status_code == 503

    async def test_timeout_is_a_503(self) -> None:
        """``httpx.ReadTimeout`` is NOT the builtin ``TimeoutError``."""
        raised = await self._call(
            httpx.ReadTimeout("timed out", request=httpx.Request("POST", "http://v/"))
        )
        assert raised.status_code == 503


class TestRedirectChainGuard:
    """``_check_redirect_chain`` drives raw httpx, so httpx errors land on it."""

    async def test_connect_error_degrades_to_no_findings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused origin yields ``[]`` rather than escaping the coroutine."""
        _install_transport(monkeypatch, _refuse_connection)

        findings = await SecurityScanner()._check_redirect_chain(f"http://{PUBLIC_HOST}/")

        assert findings == []


class TestScannerGatherIsResilient:
    """One failing sub-check must not erase the other three."""

    @staticmethod
    def _marker(name: str) -> Callable[..., object]:
        async def _check(*_args, **_kwargs) -> list[dict]:
            return [{"check_id": f"marker-{name}", "category": "security"}]

        return _check

    @staticmethod
    async def _explode(*_args, **_kwargs) -> list[dict]:
        raise httpx.ConnectError("connection refused", request=httpx.Request("GET", "http://x/"))

    async def test_failed_check_is_recorded_and_peers_survive(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The three healthy checks still contribute; the fourth is reported.

        Without ``return_exceptions=True`` the ``httpx.ConnectError`` propagated
        out of ``gather`` into ``scan``'s outer handler, which appends ONE
        generic "Connection failed" finding -- so all three marker findings were
        lost and the job was scored as if the site had no security findings.
        """
        monkeypatch.setattr(
            "src.domains.critique.services.scanners.security_scanner.scanner."
            "_is_ssrf_safe_resolved",
            AsyncMock(return_value=True),
        )
        _install_transport(monkeypatch, lambda request: httpx.Response(200, text="<html></html>"))
        monkeypatch.setattr(SecurityScanner, "_check_exposed_files", self._marker("exposed_files"))
        monkeypatch.setattr(SecurityScanner, "_check_cors", self._marker("cors"))
        monkeypatch.setattr(
            SecurityScanner, "_check_directory_listing", self._marker("directory_listing")
        )
        monkeypatch.setattr(SecurityScanner, "_check_redirect_chain", self._explode)

        with caplog.at_level(logging.WARNING):
            findings = await SecurityScanner().scan(f"http://{PUBLIC_HOST}/", html="<html></html>")

        check_ids = [f.get("check_id") for f in findings]
        assert "marker-exposed_files" in check_ids
        assert "marker-cors" in check_ids
        assert "marker-directory_listing" in check_ids

        errored = [f for f in findings if f.get("check_id") == "sec-check-errored"]
        assert len(errored) == 1, "a check that could not run must be visibly distinct"
        assert "redirect_chain" in errored[0]["evidence"]
        assert "ConnectError" in errored[0]["evidence"]

    async def test_all_checks_healthy_records_no_error_finding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A check that ran and found nothing must NOT look like a failure."""
        monkeypatch.setattr(
            "src.domains.critique.services.scanners.security_scanner.scanner."
            "_is_ssrf_safe_resolved",
            AsyncMock(return_value=True),
        )
        _install_transport(monkeypatch, lambda request: httpx.Response(200, text="<html></html>"))
        for name in (
            "_check_exposed_files",
            "_check_cors",
            "_check_directory_listing",
            "_check_redirect_chain",
        ):
            monkeypatch.setattr(SecurityScanner, name, self._marker(name))

        findings = await SecurityScanner().scan(f"http://{PUBLIC_HOST}/", html="<html></html>")

        assert not [f for f in findings if f.get("check_id") == "sec-check-errored"]


class TestZapAdapterTimeoutClause:
    """The distinct "timed out" log must actually be reachable."""

    async def test_httpx_timeout_takes_the_timeout_branch(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """httpx raises ``ReadTimeout``, never the builtin ``TimeoutError``.

        The old ``except TimeoutError`` was unreachable (nothing in the guarded
        block raises it), so a ZAP timeout was logged as a generic scan failure.
        """
        from src.domains.critique.services.scanners.tool_adapters.zap_adapter import ZapAdapter

        def _timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        _install_transport(monkeypatch, _timeout)
        target = MagicMock()
        target.url = f"http://{PUBLIC_HOST}/"
        monkeypatch.setattr(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter."
            "resolve_external_scan_target",
            AsyncMock(return_value=target),
        )

        with caplog.at_level(logging.WARNING):
            findings = await ZapAdapter().scan(f"http://{PUBLIC_HOST}/")

        assert findings == []
        assert any("timed out" in record.getMessage() for record in caplog.records)
