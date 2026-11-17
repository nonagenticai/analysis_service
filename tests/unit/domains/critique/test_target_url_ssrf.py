"""RED-proof for the tenant-supplied critique **scan target** as an SSRF sink.

WHAT WAS UNGUARDED
------------------
``CritiqueRequest.url`` / ``BatchCritiqueRequest.urls`` are plain ``str`` fields
with no scheme or host validation at the schema layer, and the value flowed
untouched into three live-fetch sinks:

* the axe adapter's headless-Chromium ``page.goto`` and the ZAP adapter's
  spider target, both reached via ``ToolAdapterRegistry.run_adapters``;
* the ``httpx`` GET in ``CritiqueService._fetch_html``.

Only ``callback_url`` was ever validated. So a tenant could not make the service
*call back* to the estate, but could absolutely make it *scan* the estate and
hand the result back in the report — a strictly better exfiltration primitive,
because the response body is returned rather than merely delivered.

WHY THESE ADDRESSES ARE THE ONES THAT MATTER
--------------------------------------------
``ipaddress.ip_address("100.64.0.5").is_private`` is **False**, and so is
``.is_reserved``: CPython classifies RFC 6598 shared address space
(``100.64.0.0/10``) as neither. On this fleet that /10 is the entire Tailscale
tailnet — bare-metal prod Postgres (``100.64.0.5``), the Docker registry
(``100.64.0.6``), the k3s control plane (``100.64.0.7``). Any guard written
as "block the private ranges" declares the whole estate a legitimate scan
target. ``is_global`` is the correct discriminator, and ``100.63.255.255`` /
``100.128.0.0`` bracket the range so a ``startswith("100.")`` implementation is
caught too.

WHAT EACH GROUP LOCKS DOWN
--------------------------
``TestCreateCritiqueTargetUrl``  the route answers **400** and, critically,
    never persists the job — a job whose target is refused later can never reach
    a terminal state.
``TestCreateCritiqueAllowsPublicTargets``  the guard did not degrade into a
    blanket deny. The /10 bracketing literals are what distinguishes "CIDR
    membership" from "prefix match".
``TestDnsAnswerBypass``  a public-looking NAME whose A record points into the
    tailnet — the case a literal-IP-only check misses entirely, and the reason
    the guard resolves rather than pattern-matching.
``TestBatchCritiqueTargetUrls``  all-or-nothing. One bad URL rejects the whole
    batch, because a partial batch's status can never reach ``completed``.
``TestRunAdaptersSsrfGate``  defence-in-depth behind the route: jobs persisted
    before the route check existed, and any future direct caller, are still
    gated. A rejected target is absorbed like a failed adapter (``[]`` +
    WARNING) rather than crashing a whole critique run.
``TestFetchHtmlRedirectHops``  the SECOND hop is validated, not just the first.
``TestFetchHtmlCrossOriginCredentialDrop``  the highest-value case: this client
    carries the *tenant's* credentials for the target site. Auto-following a
    redirect hands them to whatever host the redirect names — including an
    ``https -> http`` downgrade on the same host, which puts a bearer token or
    session cookie on the wire in plaintext.

Every test here is hermetic: DNS comes from the autouse ``dns_stub`` fixture
(``tests/dns_stub.py``) and HTTP from ``httpx.MockTransport``. A real lookup
under this repo's ``-n 4`` xdist is flaky, and ``--reruns 2`` would mask it
rather than surface it.
"""

from __future__ import annotations

import base64
import functools
import logging
from collections.abc import Callable, Coroutine
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from src.domains.critique.routes.batch import create_batch_critique
from src.domains.critique.routes.jobs import create_critique
from src.domains.critique.schemas import (
    BatchCritiqueRequest,
    BatchCritiqueResponse,
    CritiqueRequest,
)
from src.domains.critique.services.critique_service import helpers as helpers_mod
from src.domains.critique.services.critique_service.service import CritiqueService
from src.domains.critique.services.scanners.tool_adapters.registry import ToolAdapterRegistry
from tests.dns_stub import TAILNET_ANSWER, DnsStub

_REGISTRY_LOGGER = "src.domains.critique.services.scanners.tool_adapters.registry"

Handler = Callable[[httpx.Request], httpx.Response]

# URLs a tenant could put in ``CritiqueRequest.url`` today, each reaching
# something it must never reach.
BLOCKED_TARGET_URLS = (
    # Bare-metal prod Postgres on the tailnet. ``is_private`` is False.
    "http://100.64.0.5/",
    # ...the same address wearing an IPv6 hat. Matches no IPv6 network and is
    # not ``is_private``; the embedded v4 must be unwrapped and re-checked.
    "http://[::ffff:100.64.0.5]/",
    # Cloud metadata. The canonical SSRF payload.
    "http://169.254.169.254/latest/meta-data/",
    # Estate-internal name. Split-DNS answers it with a public-LOOKING CGNAT
    # address, so it has to die on the NAME, before the resolver is consulted.
    "http://registry.internal/v2/_catalog",
    # Loopback, naming a port only the pod itself can reach.
    "http://localhost:5432/",
    # Non-HTTP scheme: a local file read rather than a network fetch.
    "file:///etc/passwd",
)

# Public literals that must keep working. ``100.63.255.255`` sits immediately
# below the CGNAT /10 and ``100.128.0.0`` immediately above it: if either is
# rejected, the implementation is prefix-matching ``100.`` rather than testing
# membership of ``100.64.0.0/10``.
ALLOWED_TARGET_URLS = (
    "https://public.example.com/",
    "https://8.8.8.8/",
    "https://100.63.255.255/",
    "https://100.128.0.0/",
)


class StubAdapter:
    """A tool adapter that records whether it was ever dispatched.

    The recording IS the assertion. ``run_adapters`` returning ``[]`` proves
    nothing on its own — an adapter that merely found nothing returns ``[]``
    too. ``scanned`` staying empty proves the SSRF gate short-circuited *before*
    dispatch, so no headless browser or spider was ever pointed at the target.
    """

    def __init__(self) -> None:
        """Start un-dispatched."""
        self.scanned: list[str] = []

    @property
    def tool_name(self) -> str:
        """Identify this adapter in registry log lines."""
        return "stub"

    @property
    def category(self) -> str:
        """Satisfy the ``ToolAdapter`` protocol."""
        return "test"

    async def is_available(self) -> bool:
        """Always available, so availability never masks the gate."""
        return True

    async def scan(self, url: str, html: str | None = None) -> list[dict]:
        """Record the dispatch and return one recognisable finding."""
        self.scanned.append(url)
        return [{"title": "dispatched"}]


def _mock_request() -> MagicMock:
    """Build a request whose ``critique_service`` is an un-called AsyncMock.

    Matches the direct-coroutine-invocation style already used throughout
    ``test_routes.py``: this repo has no ``TestClient``/``app`` fixture, and the
    guard under test lives in the handler, not in middleware.

    Returns:
        A ``MagicMock`` request carrying a tenant id and a stub service.
    """
    request = MagicMock()
    request.state.tenant_id = "e35ed373-d8e0-5b88-aa83-e4ec10d4d463"
    service = AsyncMock()
    service.create_job = AsyncMock(return_value="job-should-not-exist")
    service.create_batch = AsyncMock(return_value=("batch-should-not-exist", []))
    request.app.state.critique_service = service
    return request


def _swallow_task(coro: Coroutine[object, object, None], **_kwargs: object) -> MagicMock:
    """Close the detached background coroutine instead of scheduling it.

    Args:
        coro: The coroutine ``asyncio.create_task`` was called with.
        **_kwargs: Ignored ``create_task`` keyword arguments.

    Returns:
        A stand-in for the task object the route stores and discards.
    """
    coro.close()
    return MagicMock()


def _install_mock_client(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """Point ``_fetch_html``'s httpx clients at a ``MockTransport``.

    Patching the CLASS (rather than passing a transport) is load-bearing:
    ``_fetch_html`` constructs a SECOND client when it drops credentials on a
    cross-origin hop, and that one must hit the mock as well.

    Args:
        monkeypatch: Undoes the patch on teardown.
        handler: Called with each outgoing request; returns the response.
    """
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        helpers_mod.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=transport),
    )


class TestCreateCritiqueTargetUrl:
    """``POST /api/critique`` must refuse an estate-reachable scan target."""

    @pytest.mark.parametrize("url", BLOCKED_TARGET_URLS)
    async def test_blocked_target_is_400_and_not_persisted(self, url: str) -> None:
        """Reject with 400 *before* ``create_job``.

        Pre-persistence is the only point at which the request is still an HTTP
        request and can be answered with a 4xx, instead of a job row that fails
        asynchronously with a reason the tenant never sees.
        """
        request = _mock_request()
        with pytest.raises(HTTPException) as exc:
            await create_critique(CritiqueRequest(url=url), request, AsyncMock())
        assert exc.value.status_code == 400
        request.app.state.critique_service.create_job.assert_not_called()

    async def test_test_net_3_is_also_refused(self) -> None:
        """``203.0.113.0/24`` (RFC 5737 TEST-NET-3) is refused by THIS guard.

        Called out explicitly because it is a deliberate divergence from the
        fleet convention in ``w2v_common.net``, which keeps the TEST-NETs
        allowed so fixtures can use documentation addresses. ``url_validator``
        lists all three TEST-NETs in ``_BLOCKED_NETWORKS`` *and* backstops on
        ``is_global``, which is ``False`` for every one of them — so no
        implementation of this guard can admit them. Asserting the real
        behaviour keeps this file honest; if the divergence is ever resolved,
        this is the test that has to change.
        """
        request = _mock_request()
        with pytest.raises(HTTPException) as exc:
            await create_critique(CritiqueRequest(url="http://203.0.113.10/"), request, AsyncMock())
        assert exc.value.status_code == 400
        request.app.state.critique_service.create_job.assert_not_called()


class TestCreateCritiqueAllowsPublicTargets:
    """The guard must not degrade into a blanket deny."""

    @pytest.mark.parametrize("url", ALLOWED_TARGET_URLS)
    async def test_public_target_still_creates_a_job(self, url: str) -> None:
        """A public target reaches ``create_job`` unchanged."""
        request = _mock_request()
        service = request.app.state.critique_service
        service.create_job = AsyncMock(return_value="job-ok")

        with patch("src.domains.critique.routes.jobs.asyncio") as mock_asyncio:
            mock_asyncio.create_task = MagicMock(side_effect=_swallow_task)
            result = await create_critique(CritiqueRequest(url=url), request, AsyncMock())

        assert result.job_id == "job-ok"
        await_args = service.create_job.await_args
        assert await_args is not None
        assert await_args.kwargs["url"] == url


class TestDnsAnswerBypass:
    """A public-looking NAME whose A record points into the tailnet.

    Nothing about ``customer-site.example.com`` looks internal; only resolving
    it reveals that traffic would land on prod Postgres. This is precisely the
    bypass a literal-IP-only check misses.
    """

    async def test_name_resolving_into_the_tailnet_is_400(self, dns_stub: DnsStub) -> None:
        """The route rejects on the resolved address, not on the spelling."""
        dns_stub.map("customer-site.example.com", TAILNET_ANSWER)
        request = _mock_request()

        with pytest.raises(HTTPException) as exc:
            await create_critique(
                CritiqueRequest(url="https://customer-site.example.com/"), request, AsyncMock()
            )

        assert exc.value.status_code == 400
        request.app.state.critique_service.create_job.assert_not_called()

    async def test_sibling_name_resolving_publicly_still_allowed(self) -> None:
        """Control: the rejection above is about the ANSWER, not the domain."""
        request = _mock_request()
        request.app.state.critique_service.create_job = AsyncMock(return_value="job-ok")

        with patch("src.domains.critique.routes.jobs.asyncio") as mock_asyncio:
            mock_asyncio.create_task = MagicMock(side_effect=_swallow_task)
            result = await create_critique(
                CritiqueRequest(url="https://other-site.example.com/"), request, AsyncMock()
            )

        assert result.job_id == "job-ok"

    async def test_unresolvable_target_fails_closed(self, dns_stub: DnsStub) -> None:
        """A name we cannot resolve is a name we cannot vouch for.

        The scan runs later and from inside the cluster, where split-DNS answers
        names that do not resolve from the admitting process — so "it will fail
        naturally at request time" was never true.
        """
        dns_stub.fail("nxdomain.example.com")
        request = _mock_request()

        with pytest.raises(HTTPException) as exc:
            await create_critique(
                CritiqueRequest(url="https://nxdomain.example.com/"), request, AsyncMock()
            )

        assert exc.value.status_code == 400
        request.app.state.critique_service.create_job.assert_not_called()


class TestBatchCritiqueTargetUrls:
    """Batch validation is all-or-nothing."""

    @staticmethod
    async def _call(request: MagicMock, urls: list[str]) -> BatchCritiqueResponse:
        """Invoke the batch route with the feature flag forced on.

        Args:
            request: Stub request carrying the stub critique service.
            urls: Batch payload.

        Returns:
            The route's response, for the success cases.
        """
        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value.features.batch_scan = True
            with patch("src.domains.critique.routes.batch.asyncio") as mock_asyncio:
                mock_asyncio.create_task = MagicMock(side_effect=_swallow_task)
                return await create_batch_critique(
                    BatchCritiqueRequest(urls=urls), request, AsyncMock()
                )

    async def test_one_bad_url_rejects_the_whole_batch(self) -> None:
        """No partial batch: a rejected entry means zero jobs created.

        A batch whose ``total`` counts a job that was never created can never
        reach ``completed``; it would sit in ``pending`` until reaped.
        """
        request = _mock_request()

        with pytest.raises(HTTPException) as exc:
            await self._call(
                request,
                [
                    "https://good-one.example.com/",
                    "http://100.64.0.5/",
                    "https://good-two.example.com/",
                ],
            )

        assert exc.value.status_code == 400
        # The error names the offending URL so the tenant can fix the input.
        assert "100.64.0.5" in str(exc.value.detail)
        request.app.state.critique_service.create_batch.assert_not_called()

    async def test_last_entry_is_validated_too(self) -> None:
        """Guard against a loop that only ever checks ``urls[0]``."""
        request = _mock_request()

        with pytest.raises(HTTPException) as exc:
            await self._call(request, ["https://good-one.example.com/", "http://169.254.169.254/"])

        assert exc.value.status_code == 400
        request.app.state.critique_service.create_batch.assert_not_called()

    async def test_all_public_batch_is_created(self) -> None:
        """Control: an all-public batch still creates its jobs."""
        request = _mock_request()
        request.app.state.critique_service.create_batch = AsyncMock(
            return_value=("batch-1", ["j1", "j2"])
        )

        result = await self._call(
            request, ["https://good-one.example.com/", "https://good-two.example.com/"]
        )

        assert result.batch_id == "batch-1"
        assert result.total == 2


class TestRunAdaptersSsrfGate:
    """``run_adapters`` is the one chokepoint in front of axe/zap/lighthouse/nuclei."""

    async def test_blocked_url_returns_empty_and_never_dispatches(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Rejection is absorbed like an adapter failure, not raised.

        Raising here would take down a whole critique run over one bad target,
        which is the opposite of the per-adapter isolation contract the rest of
        the method implements. So the observable signals are ``[]`` plus a
        WARNING — and the proof the gate fired is that ``scan`` never ran.
        """
        registry = ToolAdapterRegistry()
        adapter = StubAdapter()
        registry.register(adapter)

        with caplog.at_level(logging.WARNING, logger=_REGISTRY_LOGGER):
            findings = await registry.run_adapters("http://100.64.0.5/")

        assert findings == []
        assert adapter.scanned == []
        assert any(
            record.levelno >= logging.WARNING and "unsafe URL" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        "url",
        ["http://169.254.169.254/", "http://registry.internal/v2/_catalog", "file:///etc/passwd"],
    )
    async def test_other_blocked_targets_never_dispatch(self, url: str) -> None:
        """Same verdict for metadata, estate names and non-HTTP schemes."""
        registry = ToolAdapterRegistry()
        adapter = StubAdapter()
        registry.register(adapter)

        assert await registry.run_adapters(url) == []
        assert adapter.scanned == []

    async def test_public_url_still_dispatches(self) -> None:
        """Control: the gate must not silently disable every external tool."""
        registry = ToolAdapterRegistry()
        adapter = StubAdapter()
        registry.register(adapter)

        findings = await registry.run_adapters("https://public.example.com/")

        assert findings == [{"title": "dispatched"}]
        assert adapter.scanned == ["https://public.example.com/"]


class TestFetchHtmlRedirectHops:
    """Every hop is re-validated, not just the initial URL."""

    async def test_redirect_into_the_tailnet_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A public URL that 302s to prod Postgres must not be followed.

        Fail-closed and LOUD: the ``ValueError`` propagates to ``process_job``,
        which finalizes the job as failed. There is deliberately no empty-HTML
        fallback — scoring an empty document would report a scan that never
        happened.
        """
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://100.64.0.5/"})

        _install_mock_client(monkeypatch, handler)
        svc = CritiqueService.__new__(CritiqueService)

        with pytest.raises(ValueError, match="not allowed"):
            await svc._fetch_html("https://public.example.com/")

        # Hop 1 was made; hop 2 was refused before a socket was opened.
        assert seen == ["https://public.example.com/"]

    async def test_redirect_to_metadata_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same for the cloud-metadata endpoint."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

        _install_mock_client(monkeypatch, handler)
        svc = CritiqueService.__new__(CritiqueService)

        with pytest.raises(ValueError, match="not allowed"):
            await svc._fetch_html("https://public.example.com/")

    async def test_public_to_public_redirect_is_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the guard blocks non-public hops, not redirects as such."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "start.example.com":
                return httpx.Response(302, headers={"location": "https://final.example.com/page"})
            return httpx.Response(200, text="<html><body>final</body></html>")

        _install_mock_client(monkeypatch, handler)
        svc = CritiqueService.__new__(CritiqueService)

        html = await svc._fetch_html("https://start.example.com/")

        assert "final" in html


class TestFetchHtmlCrossOriginCredentialDrop:
    """The tenant's credentials must not survive a hop to another origin.

    ``_fetch_html`` carries whatever the tenant configured for the target site:
    an ``httpx.BasicAuth``, an ``Authorization: Bearer`` header, or a ``Cookie``
    header. Auto-following redirects forwards all of it to whatever host the
    redirect names.

    Each case asserts on the ACTUAL outgoing ``httpx.Request`` headers, because
    a credential is only attached at send time — ``BasicAuth`` in particular is
    applied by the auth flow, not by the client constructor — so inspecting the
    client's configuration would prove nothing.
    """

    _BASIC: ClassVar[dict[str, str]] = {
        "type": "basic",
        "username": "alice",
        "password": "hunter2",  # pragma: allowlist secret
    }
    _BEARER: ClassVar[dict[str, str]] = {
        "type": "bearer",
        "token": "tok-abc123",
    }  # pragma: allowlist secret
    _COOKIES: ClassVar[dict[str, str]] = {
        "type": "cookies",
        "cookie_header": "sess=abc123; remember=1",  # pragma: allowlist secret
    }

    @staticmethod
    def _expected_header(auth_info: dict[str, str]) -> tuple[str, str]:
        """Return the ``(name, value)`` header the first hop must carry.

        Args:
            auth_info: One of the three credential shapes above.

        Returns:
            The lower-cased header name and its exact expected value.
        """
        if auth_info["type"] == "basic":
            raw = f"{auth_info['username']}:{auth_info['password']}".encode()
            return ("authorization", "Basic " + base64.b64encode(raw).decode("ascii"))
        if auth_info["type"] == "bearer":
            return ("authorization", f"Bearer {auth_info['token']}")
        return ("cookie", auth_info["cookie_header"])

    @staticmethod
    async def _run(
        monkeypatch: pytest.MonkeyPatch, auth_info: dict[str, str], location: str
    ) -> tuple[list[httpx.Request], str]:
        """Fetch ``https://good.test/``, 302 once to *location*, capture requests.

        Args:
            monkeypatch: Used to install the mock transport.
            auth_info: Credential shape to apply to the first hop.
            location: ``Location`` header returned by the first response.

        Returns:
            Every outgoing request in order, and the final HTML.
        """
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"location": location})
            return httpx.Response(200, text="<html><body>landed</body></html>")

        _install_mock_client(monkeypatch, handler)
        svc = CritiqueService.__new__(CritiqueService)
        html = await svc._fetch_html("https://good.test/", auth_info=auth_info)
        return requests, html

    @pytest.mark.parametrize("auth_info", [_BASIC, _BEARER, _COOKIES])
    async def test_credentials_dropped_on_cross_host_redirect(
        self, monkeypatch: pytest.MonkeyPatch, auth_info: dict[str, str]
    ) -> None:
        """A hop to a different HOST must arrive unauthenticated."""
        requests, html = await self._run(monkeypatch, auth_info, "https://evil.test/")
        name, value = self._expected_header(auth_info)

        assert requests[0].headers.get(name) == value, "hop 1 should still be credentialed"
        assert len(requests) == 2
        assert requests[1].url.host == "evil.test"
        assert "authorization" not in requests[1].headers
        assert "cookie" not in requests[1].headers
        assert "landed" in html

    @pytest.mark.parametrize("auth_info", [_BASIC, _BEARER, _COOKIES])
    async def test_credentials_dropped_on_https_to_http_downgrade(
        self, monkeypatch: pytest.MonkeyPatch, auth_info: dict[str, str]
    ) -> None:
        """Same HOST, downgraded SCHEME — still a different origin.

        Comparing only the host would keep the credential and put the tenant's
        bearer token or session cookie on a plaintext connection. That downgrade
        is the exact leak fixed in ``w2v_common`` 0.53.0, and is why
        ``_url_origin`` compares ``(scheme, host, port)`` rather than the host
        alone.
        """
        requests, _html = await self._run(monkeypatch, auth_info, "http://good.test/")

        assert len(requests) == 2
        assert requests[1].url.scheme == "http"
        assert requests[1].url.host == "good.test"
        assert "authorization" not in requests[1].headers
        assert "cookie" not in requests[1].headers

    @pytest.mark.parametrize("auth_info", [_BASIC, _BEARER, _COOKIES])
    async def test_credentials_kept_on_same_origin_redirect(
        self, monkeypatch: pytest.MonkeyPatch, auth_info: dict[str, str]
    ) -> None:
        """Control: a same-origin hop must KEEP the credential.

        Dropping here would silently break authenticated critiques of any site
        that redirects ``/`` to its real entry point — the common case.
        """
        requests, _html = await self._run(monkeypatch, auth_info, "https://good.test/other")
        name, value = self._expected_header(auth_info)

        assert len(requests) == 2
        assert requests[1].url.path == "/other"
        assert requests[1].headers.get(name) == value

    async def test_default_port_is_not_a_different_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``https://good.test`` and ``https://good.test:443`` are one origin."""
        requests, _html = await self._run(monkeypatch, self._BEARER, "https://good.test:443/other")
        name, value = self._expected_header(self._BEARER)

        assert len(requests) == 2
        assert requests[1].headers.get(name) == value
