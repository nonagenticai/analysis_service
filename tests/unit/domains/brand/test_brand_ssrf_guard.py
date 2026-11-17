"""SSRF regression tests for every outbound fetch in the brand domain.

Each sink gets the same battery: a non-HTTP scheme, an estate-internal name, the
cloud metadata address, an RFC 1918 literal and an RFC 6598 / CGNAT (tailnet)
literal. Where the sink follows redirects there is an extra case proving the
second hop is re-checked -- a first-hop-only guard passes every other test in
this file and still ships the hole.

Each test asserts the *side effect* (no navigation, no HTTP request issued), not
just the exception, so a guard that raises after the request has already gone
out cannot pass.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.security import BrandSsrfBlockedError, ensure_public_url, get_guarded
from src.domains.brand.services.css_parser import CSSBrandParser
from src.domains.brand.services.logo_service import LogoService
from src.domains.brand.services.metadata_extractor import MetadataExtractor
from src.domains.brand.services.storage_service import StorageService
from src.domains.brand.stages.fetch import FetchStage

# The vectors. Names are used as pytest ids.
BLOCKED_URLS = [
    pytest.param("file:///etc/passwd", id="file-scheme"),
    pytest.param("http://registry.internal/v2/_catalog", id="estate-internal-name"),
    pytest.param("http://169.254.169.254/latest/meta-data/", id="cloud-metadata"),
    pytest.param("http://10.0.0.1/admin", id="rfc1918"),
    pytest.param("http://100.64.0.5:5432/", id="cgnat-tailnet-postgres"),
    pytest.param("http://100.64.0.6:5000/v2/_catalog", id="cgnat-tailnet-registry"),
    pytest.param("http://localhost:6303/health", id="localhost"),
    pytest.param("http://[::ffff:100.64.0.1]/", id="ipv4-mapped-ipv6-tailnet"),
]

SAFE_URL = "https://example.com/page"


# --------------------------------------------------------------------------- #
# The guard itself
# --------------------------------------------------------------------------- #
class TestEnsurePublicUrl:
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    def test_rejects(self, url: str) -> None:
        with pytest.raises(BrandSsrfBlockedError):
            ensure_public_url(url)

    @pytest.mark.parametrize("url", ["https://example.com", "http://example.org/a?b=c"])
    def test_allows_public(self, url: str) -> None:
        ensure_public_url(url)  # must not raise


def _response(
    status: int, url: str, *, headers: dict[str, str] | None = None, body: bytes = b""
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers=headers or {},
        content=body,
        request=httpx.Request("GET", url),
    )


# Captured before any test monkeypatches ``httpx.AsyncClient``.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _recording_client(
    handler: Callable[[httpx.Request], httpx.Response], seen: list[str]
) -> httpx.AsyncClient:
    """A hermetic client that records every URL the transport is asked for.

    The three sinks below now fetch under a byte cap, which means ``get_guarded``
    reaches them through ``client.stream``; an ``AsyncMock`` cannot stand in for
    that (it needs an async context manager yielding a response). A real client
    over a ``MockTransport`` can, and it upgrades the assertion at the same
    time: instead of inspecting the ``follow_redirects`` kwarg we assert on the
    URLs that were actually requested, so a redirect that *was* taken shows up
    as a second entry in *seen*.

    ``follow_redirects=True`` on the client is deliberate. It is the opposite of
    what the services construct, so the test passes only because ``get_guarded``
    forces ``follow_redirects=False`` per request -- the client default cannot
    carry the test.
    """

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    # Bind the real class NOW: callers install the enclosing factory *as*
    # ``httpx.AsyncClient``, so naming it here would re-enter that factory and
    # recurse until the stack blows.
    return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(record), follow_redirects=True)


def _client_factory(
    handler: Callable[[httpx.Request], httpx.Response], seen: list[str]
) -> Callable[..., httpx.AsyncClient]:
    """``_recording_client`` as a drop-in for a patched ``httpx.AsyncClient``."""

    def factory(**_kwargs: object) -> httpx.AsyncClient:
        return _recording_client(handler, seen)

    return factory


class TestGetGuarded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_never_issues_the_request(self, url: str) -> None:
        client = AsyncMock()
        with pytest.raises(BrandSsrfBlockedError):
            await get_guarded(client, url)
        client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_redirect_into_the_tailnet_is_blocked(self) -> None:
        """A public first hop that 302s inward must not be followed.

        This is the case a first-hop-only check cannot see.
        """
        client = AsyncMock()
        client.get.return_value = _response(
            302, SAFE_URL, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )
        with pytest.raises(BrandSsrfBlockedError):
            await get_guarded(client, SAFE_URL)
        # Exactly one request: the public first hop. The redirect was not taken.
        assert client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_relative_redirect_to_public_is_followed(self) -> None:
        client = AsyncMock()
        client.get.side_effect = [
            _response(302, SAFE_URL, headers={"location": "/other"}),
            _response(200, "https://example.com/other", body=b"ok"),
        ]
        response = await get_guarded(client, SAFE_URL)
        assert response.status_code == 200
        assert client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_never_auto_follows(self) -> None:
        """Redirects must be disabled per-request, whatever the client default."""
        client = AsyncMock()
        client.get.return_value = _response(200, SAFE_URL, body=b"ok")
        await get_guarded(client, SAFE_URL)
        assert client.get.await_args.kwargs["follow_redirects"] is False

    @pytest.mark.asyncio
    async def test_redirect_loop_is_capped(self) -> None:
        client = AsyncMock()
        client.get.return_value = _response(302, SAFE_URL, headers={"location": SAFE_URL})
        with pytest.raises(BrandSsrfBlockedError, match="too many redirects"):
            await get_guarded(client, SAFE_URL)


# --------------------------------------------------------------------------- #
# Sink 1 -- Playwright page.goto (src/domains/brand/stages/fetch.py)
# --------------------------------------------------------------------------- #
class TestFetchStageSsrf:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_browser_is_never_launched(self, url: str) -> None:
        stage = FetchStage()
        ctx = PipelineContext(url=url)
        with patch("playwright.async_api.async_playwright") as pw:
            with pytest.raises(BrandSsrfBlockedError):
                await stage.run(ctx, BrandResponse(), StageResult(stage_name="fetch"))
            pw.assert_not_called()
        assert not ctx.screenshot_bytes
        assert not ctx.html

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_execute_records_a_stage_failure(self, url: str) -> None:
        """``BaseStage.execute`` must catch it as a failure, not let it escape.

        ``PipelineRunner._execute_stages`` aborts the whole pipeline when the
        ``fetch`` stage fails, so this is what stops the later stages from
        running against a target we refused.
        """
        stage = FetchStage()
        # Playwright is patched even though the guard should fire first: without
        # it, a regression here does not fail the test, it makes the test *itself*
        # drive a real Chromium at registry.internal / 100.64.0.5. A security
        # test must not become the exploit when the control it covers regresses.
        with patch("playwright.async_api.async_playwright"):
            result = await stage.execute(PipelineContext(url=url), BrandResponse())
        assert result.success is False
        # Assert on the *reason*, not just on failure. ``BaseStage.execute``
        # swallows TypeError too, so "the stage failed" is satisfied by the
        # unguarded code crashing on a mock -- this test passed under mutation
        # until it checked the message.
        assert any("refusing to fetch" in e.message for e in result.errors), result.errors


# --------------------------------------------------------------------------- #
# Sink 2 -- logo download (src/domains/brand/services/logo_service.py)
# --------------------------------------------------------------------------- #
class TestLogoServiceSsrf:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_own_client_never_requests(self, url: str) -> None:
        service = LogoService(storage=AsyncMock(spec=StorageService))
        with patch("src.domains.brand.services.logo_service.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(BrandSsrfBlockedError):
                await service.download_and_store(url, "2e960681-0187-5dad-ba0a-2b95a67ecfaa")
            client.get.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_shared_client_never_requests(self, url: str) -> None:
        """The injected app-level client is the branch the pipeline actually uses."""
        shared = AsyncMock()
        service = LogoService(storage=AsyncMock(spec=StorageService), http_client=shared)
        with pytest.raises(BrandSsrfBlockedError):
            await service.download_and_store(url, "2e960681-0187-5dad-ba0a-2b95a67ecfaa")
        shared.get.assert_not_called()
        shared.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_redirect_into_the_estate_is_blocked(self) -> None:
        seen: list[str] = []

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://registry.internal/v2/_catalog"})

        async with _recording_client(handle, seen) as shared:
            service = LogoService(storage=AsyncMock(spec=StorageService), http_client=shared)
            with pytest.raises(BrandSsrfBlockedError):
                await service.download_and_store(
                    "https://example.com/logo.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )
        # Exactly one request, and it is the public first hop. The estate URL
        # was never asked for.
        assert seen == ["https://example.com/logo.png"]


# --------------------------------------------------------------------------- #
# Sink 3 -- stylesheet fetch (src/domains/brand/services/css_parser.py)
# --------------------------------------------------------------------------- #
class TestCssParserSsrf:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_never_requests_and_fails_soft(self, url: str) -> None:
        parser = CSSBrandParser()
        with patch("src.domains.brand.services.css_parser.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            assert await parser._fetch_css(url) == ""
            client.get.assert_not_called()
            client.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_redirect_to_metadata_is_blocked(self) -> None:
        parser = CSSBrandParser()
        seen: list[str] = []

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )

        with patch(
            "src.domains.brand.services.css_parser.httpx.AsyncClient",
            _client_factory(handle, seen),
        ):
            assert await parser._fetch_css("https://example.com/a.css") == ""
        # The load-bearing assertion. "Returned empty" is *also* what the
        # unguarded version does, so only proving the metadata URL was never
        # requested distinguishes the fix from the hole -- and the client here
        # is built with follow_redirects=True, so nothing but the per-request
        # override in ``get_guarded`` can stop the second hop.
        assert seen == ["https://example.com/a.css"]


# --------------------------------------------------------------------------- #
# Sink 4 -- web manifest fetch (src/domains/brand/services/metadata_extractor.py)
# --------------------------------------------------------------------------- #
def _soup_with_manifest(href: str) -> BeautifulSoup:
    """A real parsed document carrying one ``<link rel=manifest href=...>``."""
    return BeautifulSoup(f'<html><head><link rel="manifest" href="{href}"></head></html>', "lxml")


class TestMetadataExtractorSsrf:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", BLOCKED_URLS)
    async def test_never_requests_and_fails_soft(self, url: str) -> None:
        extractor = MetadataExtractor()
        with patch("src.domains.brand.services.metadata_extractor.httpx.AsyncClient") as client_cls:
            client = AsyncMock()
            client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
            client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            theme, icons = await extractor.extract_manifest(
                _soup_with_manifest(url), "https://example.com/"
            )
            assert (theme, icons) == ("", [])
            client.get.assert_not_called()
            client.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_redirect_into_the_tailnet_is_blocked(self) -> None:
        extractor = MetadataExtractor()
        seen: list[str] = []
        soup = _soup_with_manifest("/site.webmanifest")

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://100.64.0.5:5432/"})

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _client_factory(handle, seen),
        ):
            theme, icons = await extractor.extract_manifest(soup, "https://example.com/")
        assert (theme, icons) == ("", [])
        # See TestCssParserSsrf: the fail-soft return alone is vacuous.
        assert seen == ["https://example.com/site.webmanifest"]
