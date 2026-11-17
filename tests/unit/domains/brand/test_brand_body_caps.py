"""Byte-cap and payload-shape regression tests for the brand domain's fetches.

Two defects live here, both of which survived the SSRF hardening because they
are about *what a permitted host sends back*, not about which host we may talk
to:

* The 5 MB logo limit was decorative. ``client.get`` buffers the entire body
  before it returns, so ``len(response.content) > MAX_SIZE_BYTES`` was checked
  on bytes the pod had already paid for. The same unbounded read reached
  stylesheets and web manifests.
* A syntactically valid web manifest that is a JSON *list* made ``data.get``
  raise ``AttributeError``, caught by neither ``extract_manifest``'s except
  tuple nor ``pipeline/base.py``'s -- a one-line file took the stage down.

The load-bearing assertion for the first is **how much was consumed**, not that
an exception was raised: a post-hoc check raises too, after the damage. Every
test here therefore counts the chunks actually pulled off the wire, and all
transport is an ``httpx.MockTransport`` -- no network, no DNS.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from src.domains.brand.security import BrandResponseTooLargeError, get_guarded
from src.domains.brand.services.css_parser import _MAX_CSS_BYTES, CSSBrandParser
from src.domains.brand.services.logo_service import LogoService
from src.domains.brand.services.metadata_extractor import _MAX_MANIFEST_BYTES, MetadataExtractor
from src.domains.brand.services.storage_service import StorageService

SAFE_URL = "https://example.com/asset"
BASE_URL = "https://example.com/"

# Chunk size for the streaming fixtures. Every cap under test is an exact
# multiple of it, which makes the expected abort point an exact integer.
CHUNK = 64 * 1024

_Handler = Callable[[httpx.Request], httpx.Response]


class CountingStream(httpx.AsyncByteStream):
    """A response body that yields chunks lazily and records how many were pulled.

    This is what makes the cap testable at all. Handing ``httpx.Response`` a
    ``content=`` blob means the body already exists in memory before the guard
    runs, which is precisely the bug -- such a fixture cannot tell the fixed
    implementation from the broken one. A lazy stream can: a streaming reader
    stops early and leaves most chunks un-yielded, a buffering reader consumes
    all of them.

    The same chunk object is re-yielded, so the fixture never allocates the
    notional payload size itself.
    """

    def __init__(self, chunk_size: int, chunk_count: int) -> None:
        self._chunk = b"x" * chunk_size
        self._chunk_count = chunk_count
        self.chunks_yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunk_count):
            self.chunks_yielded += 1
            yield self._chunk


def _abort_chunk(cap: int) -> int:
    """Chunk ordinal at which a ``CHUNK``-sized stream must trip *cap*."""
    return cap // CHUNK + 1


def _oversized_handler(stream: CountingStream, content_type: str) -> _Handler:
    """Serve *stream* with no ``Content-Length``.

    Omitting the length is deliberate: it forces the cap to be proven by the
    streaming accumulator rather than by ``_get_capped``'s cheap header
    pre-check, which is the path that bounds an origin that declares nothing.
    """

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, stream=stream)

    return handle


def _client_factory(handler: _Handler) -> Callable[..., httpx.AsyncClient]:
    """Stand-in for ``httpx.AsyncClient`` backed by a hermetic ``MockTransport``.

    The services construct their own client with timeout/header kwargs that are
    irrelevant here, so the factory ignores them. Two things matter:

    * it returns a **real** client, because the capped path uses
      ``client.stream``, and an ``AsyncMock`` cannot model an async context
      manager yielding a response;
    * it sets ``follow_redirects=True``, so a test asserting a redirect was
      *not* taken proves the per-request ``follow_redirects=False`` override in
      ``get_guarded`` rather than merely the client default.
    """

    # Bind the real class NOW. Tests install this factory *as*
    # ``httpx.AsyncClient``, so referring to ``httpx.AsyncClient`` inside the
    # body would re-enter the factory and recurse until the stack blows.
    real_client_cls = httpx.AsyncClient

    def factory(**_kwargs: object) -> httpx.AsyncClient:
        return real_client_cls(transport=httpx.MockTransport(handler), follow_redirects=True)

    return factory


def _soup_with_manifest(href: str = "/site.webmanifest") -> BeautifulSoup:
    return BeautifulSoup(f'<html><head><link rel="manifest" href="{href}"></head></html>', "lxml")


# --------------------------------------------------------------------------- #
# The cap itself
# --------------------------------------------------------------------------- #
class TestGetGuardedByteCap:
    @pytest.mark.asyncio
    async def test_aborts_mid_body_and_does_not_buffer_the_rest(self) -> None:
        """The point of the fix: the read stops, it is not merely judged after."""
        stream = CountingStream(chunk_size=64, chunk_count=100)
        async with _client_factory(_oversized_handler(stream, "image/png"))() as client:
            with pytest.raises(BrandResponseTooLargeError, match="exceeds maximum"):
                await get_guarded(client, SAFE_URL, max_bytes=100)
        # 64 bytes is under the cap, 128 is over: exactly two chunks are pulled
        # off a 100-chunk body. A buffering implementation reports 100.
        assert stream.chunks_yielded == 2

    @pytest.mark.asyncio
    async def test_no_cap_keeps_the_buffered_get_path(self) -> None:
        """Regression guard for the callers that did not opt in.

        ``max_bytes=None`` must still be a plain ``client.get`` -- not a stream
        with an infinite cap -- so nothing changes underneath a caller (or a
        test) that passes nothing.
        """
        body = b"x" * 10_000
        client = AsyncMock()
        client.get.return_value = httpx.Response(
            200, content=body, request=httpx.Request("GET", SAFE_URL)
        )
        response = await get_guarded(client, SAFE_URL)
        assert response.content == body
        assert client.get.await_count == 1
        client.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_body_exactly_at_the_cap_succeeds(self) -> None:
        body = b"y" * 100

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/css"}, content=body)

        async with _client_factory(handle)() as client:
            response = await get_guarded(client, SAFE_URL, max_bytes=100)
        assert response.content == body
        # The rebuilt response must still look like the original to callers.
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/css"

    @pytest.mark.asyncio
    async def test_one_byte_over_the_cap_raises(self) -> None:
        """Boundary partner of the test above, via the declared-length path."""

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"y" * 101)

        async with _client_factory(handle)() as client:
            with pytest.raises(BrandResponseTooLargeError, match="exceeds maximum"):
                await get_guarded(client, SAFE_URL, max_bytes=100)

    @pytest.mark.asyncio
    async def test_one_byte_over_raises_without_a_declared_length(self) -> None:
        """An origin that declares nothing must still be caught by the accumulator."""
        stream = CountingStream(chunk_size=101, chunk_count=1)
        async with _client_factory(_oversized_handler(stream, "text/css"))() as client:
            with pytest.raises(BrandResponseTooLargeError):
                await get_guarded(client, SAFE_URL, max_bytes=100)
        assert stream.chunks_yielded == 1

    @pytest.mark.asyncio
    async def test_declared_oversize_is_refused_without_reading(self) -> None:
        """A ``Content-Length`` over the cap means the body is never touched."""
        stream = CountingStream(chunk_size=CHUNK, chunk_count=4)

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-length": "999999"}, stream=stream)

        async with _client_factory(handle)() as client:
            with pytest.raises(BrandResponseTooLargeError, match="declares"):
                await get_guarded(client, SAFE_URL, max_bytes=100)
        assert stream.chunks_yielded == 0

    @pytest.mark.asyncio
    async def test_redirects_are_still_hopped_with_a_cap_set(self) -> None:
        """Switching to ``stream`` must not break manual hop handling.

        The redirect response is read for its ``Location`` only, so this proves
        both that the hop is taken and that the final body still arrives.
        """
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/a.css":
                return httpx.Response(302, headers={"location": "/b.css"})
            return httpx.Response(200, content=b"body { color: red; }")

        async with _client_factory(handle)() as client:
            response = await get_guarded(client, "https://example.com/a.css", max_bytes=1_000)
        assert response.status_code == 200
        assert response.content == b"body { color: red; }"
        assert seen == ["https://example.com/a.css", "https://example.com/b.css"]


# --------------------------------------------------------------------------- #
# Sink 1 -- logo download: hard-fails (the caller explicitly asked for a logo)
# --------------------------------------------------------------------------- #
class TestLogoServiceBodyCap:
    @pytest.mark.asyncio
    async def test_oversized_logo_raises_before_it_is_materialised(self) -> None:
        expected = _abort_chunk(LogoService.MAX_SIZE_BYTES)
        stream = CountingStream(chunk_size=CHUNK, chunk_count=expected * 2)
        storage = AsyncMock(spec=StorageService)
        service = LogoService(storage=storage)

        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_oversized_handler(stream, "image/png")),
        ):
            with pytest.raises(BrandResponseTooLargeError, match="exceeds maximum"):
                await service.download_and_store(
                    "https://example.com/huge.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )

        assert stream.chunks_yielded == expected
        storage.store.assert_not_called()

    def test_oversize_is_a_valueerror_so_callers_still_degrade(self) -> None:
        """The route maps ``ValueError`` to a 400 and stages treat it as failure.

        The old post-hoc check raised a plain ``ValueError``; the replacement
        must keep that contract or the 400 silently becomes a 500.
        """
        assert issubclass(BrandResponseTooLargeError, ValueError)


# --------------------------------------------------------------------------- #
# Sink 2 -- stylesheet fetch: fail-soft (one best-effort signal among many)
# --------------------------------------------------------------------------- #
class TestCssParserBodyCap:
    @pytest.mark.asyncio
    async def test_oversized_stylesheet_degrades_to_empty(self) -> None:
        expected = _abort_chunk(_MAX_CSS_BYTES)
        stream = CountingStream(chunk_size=CHUNK, chunk_count=expected * 2)
        parser = CSSBrandParser()

        with patch(
            "src.domains.brand.services.css_parser.httpx.AsyncClient",
            _client_factory(_oversized_handler(stream, "text/css")),
        ):
            assert await parser._fetch_css("https://cdn.example.com/huge.css") == ""

        assert stream.chunks_yielded == expected


# --------------------------------------------------------------------------- #
# Sink 3 -- web manifest: fail-soft, plus the payload-shape defect
# --------------------------------------------------------------------------- #
class TestMetadataExtractorBodyCap:
    @pytest.mark.asyncio
    async def test_oversized_manifest_degrades_to_empty(self) -> None:
        expected = _abort_chunk(_MAX_MANIFEST_BYTES)
        stream = CountingStream(chunk_size=CHUNK, chunk_count=expected * 2)
        extractor = MetadataExtractor()
        soup = _soup_with_manifest()

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _client_factory(_oversized_handler(stream, "application/manifest+json")),
        ):
            result = await extractor.extract_manifest(soup, BASE_URL)

        assert result == ("", [])
        assert stream.chunks_yielded == expected


class TestMetadataExtractorPayloadShape:
    """A manifest that parses but is not an object must not kill the stage."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(json.dumps([{"src": "/icon.png"}]), id="json-list"),
            pytest.param(json.dumps("a string manifest"), id="json-string"),
            pytest.param(json.dumps(42), id="json-number"),
            pytest.param("<html>not json at all</html>", id="not-json"),
            pytest.param("", id="empty-body"),
        ],
    )
    async def test_degrades_instead_of_raising(self, body: str) -> None:
        extractor = MetadataExtractor()
        soup = _soup_with_manifest()

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "application/json"}, content=body.encode()
            )

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _client_factory(handle),
        ):
            # Must not raise AttributeError / JSONDecodeError: neither is caught
            # here nor by ``BaseStage.execute``'s tuple.
            result = await extractor.extract_manifest(soup, BASE_URL)

        assert result == ("", [])

    @pytest.mark.asyncio
    async def test_a_real_object_manifest_still_works(self) -> None:
        """The shape check must not swallow the happy path it guards."""
        extractor = MetadataExtractor()
        soup = _soup_with_manifest()
        manifest = {"theme_color": "#FF5733", "icons": [{"src": "/icon-192.png"}]}

        def handle(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=manifest)

        with patch(
            "src.domains.brand.services.metadata_extractor.httpx.AsyncClient",
            _client_factory(handle),
        ):
            theme, icons = await extractor.extract_manifest(soup, BASE_URL)

        assert theme == "#FF5733"
        assert icons == ["https://example.com/icon-192.png"]
