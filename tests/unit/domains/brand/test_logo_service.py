"""Tests for logo_service."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.domains.brand.services.logo_service import LogoService
from src.domains.brand.services.storage_service import StorageService

_Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def mock_storage() -> AsyncMock:
    storage = AsyncMock(spec=StorageService)
    storage.store.return_value = "/data/test-tenant/logos/test-id.png"
    storage.retrieve.return_value = b"fake-image-data"
    storage.delete.return_value = True
    return storage


@pytest.fixture
def logo_service(mock_storage: AsyncMock) -> LogoService:
    return LogoService(storage=mock_storage)


def _handler(
    content: bytes = b"fake-png-data",
    content_type: str = "image/png",
    status_code: int = 200,
) -> _Handler:
    """A MockTransport handler serving one canned logo response."""

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            headers={"content-type": content_type},
            content=content,
        )

    return handle


def _client_factory(handler: _Handler) -> Callable[..., httpx.AsyncClient]:
    """Stand-in for ``httpx.AsyncClient`` backed by a hermetic ``MockTransport``.

    The download path is capped (``max_bytes=MAX_SIZE_BYTES``) and therefore
    goes through ``client.stream``, which an ``AsyncMock`` cannot model -- it
    needs an async context manager yielding a response. A real client over a
    ``MockTransport`` is still fully offline: no network, no DNS.
    """

    # Bind the real class NOW: this factory is installed *as*
    # ``httpx.AsyncClient``, so naming it inside the body would re-enter the
    # factory and recurse until the stack blows.
    real_client_cls = httpx.AsyncClient

    def factory(**_kwargs: object) -> httpx.AsyncClient:
        return real_client_cls(transport=httpx.MockTransport(handler))

    return factory


class TestDownloadAndStore:
    @pytest.mark.asyncio
    async def test_success_png(self, logo_service: LogoService, mock_storage: AsyncMock) -> None:
        """Test successful PNG logo download and storage."""
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler()),
        ):
            result = await logo_service.download_and_store(
                "https://example.com/logo.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )

            assert result.content_type == "image/png"
            assert result.size_bytes == len(b"fake-png-data")
            assert result.original_url == "https://example.com/logo.png"
            assert result.asset_id  # not empty
            mock_storage.store.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_jpeg(self, logo_service: LogoService, mock_storage: AsyncMock) -> None:
        """Test successful JPEG logo download."""
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content_type="image/jpeg")),
        ):
            result = await logo_service.download_and_store(
                "https://example.com/logo.jpg", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )
            assert result.content_type == "image/jpeg"

    @pytest.mark.asyncio
    async def test_rejects_invalid_content_type(self, logo_service: LogoService) -> None:
        """Test rejection of non-image content type."""
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content_type="text/html")),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                await logo_service.download_and_store(
                    "https://example.com/page.html", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )

    @pytest.mark.asyncio
    async def test_rejects_oversized_file(self, logo_service: LogoService) -> None:
        """Test rejection of files exceeding size limit.

        Still a ``ValueError`` matching the same message, but it is now raised
        by the streaming cap in ``get_guarded`` rather than by a post-hoc
        ``len(response.content)`` check on a body already resident in memory.
        ``tests/unit/domains/brand/test_brand_body_caps.py`` is the test that
        proves the *read* stops; this one pins the caller-visible contract.
        """
        big_data = b"x" * (LogoService.MAX_SIZE_BYTES + 1)
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content=big_data)),
        ):
            with pytest.raises(ValueError, match="exceeds maximum"):
                await logo_service.download_and_store(
                    "https://example.com/huge.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )

    @pytest.mark.asyncio
    async def test_rejects_empty_response(self, logo_service: LogoService) -> None:
        """Test rejection of empty response body."""
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content=b"")),
        ):
            with pytest.raises(ValueError, match="Empty response"):
                await logo_service.download_and_store(
                    "https://example.com/empty.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )

    @pytest.mark.asyncio
    async def test_http_error_propagates(self, logo_service: LogoService) -> None:
        """Test that HTTP errors propagate."""
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(status_code=404)),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await logo_service.download_and_store(
                    "https://example.com/missing.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )


class TestDetectSource:
    def test_favicon(self, logo_service: LogoService) -> None:
        assert logo_service._detect_source("https://example.com/favicon.ico") == "favicon"

    def test_apple_touch_icon(self, logo_service: LogoService) -> None:
        assert (
            logo_service._detect_source("https://example.com/apple-touch-icon.png")
            == "apple-touch-icon"
        )

    def test_default(self, logo_service: LogoService) -> None:
        assert logo_service._detect_source("https://example.com/logo.png") == "logo-tag"


class TestRetrieveAndDelete:
    @pytest.mark.asyncio
    async def test_retrieve(self, logo_service: LogoService, mock_storage: AsyncMock) -> None:
        result = await logo_service.retrieve("/some/path.png")
        assert result == b"fake-image-data"
        mock_storage.retrieve.assert_called_once_with("/some/path.png")

    @pytest.mark.asyncio
    async def test_delete(self, logo_service: LogoService, mock_storage: AsyncMock) -> None:
        result = await logo_service.delete("/some/path.png")
        assert result is True
        mock_storage.delete.assert_called_once_with("/some/path.png")


class TestContentTypeConfusion:
    """The URL-extension fallback must not overrule what the server SAID.

    PR #172 fixed exactly this and was superseded before the hunk landed, so the
    defect stayed on main: a stated, disallowed content type was silently
    replaced by a guess taken from the whole URL string -- query included.
    """

    @pytest.mark.asyncio
    async def test_stated_content_type_is_not_overruled_by_the_url(
        self, logo_service: LogoService
    ) -> None:
        """A server that says `application/json` is answering with JSON.

        The extension in the path is not evidence against an explicit header;
        honouring it stores non-image bytes as `image/png`, and every downstream
        consumer then treats them as an image.
        """
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content_type="application/json")),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                await logo_service.download_and_store(
                    "https://example.com/logo.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
                )

    @pytest.mark.asyncio
    async def test_guess_ignores_the_query_string(self, logo_service: LogoService) -> None:
        """Regression guard, NOT a defect fix -- verified on Python 3.14.3.

        PR #172 hardened this by guessing from `urlsplit(url).path`. On this
        interpreter that is a no-op: `mimetypes.guess_type` already discards the
        query, so `.../export?download=logo.png` guesses `None`. The test pins
        that behaviour rather than the workaround, so it fails if the stdlib ever
        changes OR if someone re-hand-rolls the split and reintroduces the hole.
        """
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content_type="")),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                await logo_service.download_and_store(
                    "https://example.com/export?download=logo.png",
                    "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
                )

    @pytest.mark.asyncio
    async def test_uninformative_content_type_still_falls_back_to_the_path(
        self, logo_service: LogoService
    ) -> None:
        """The fallback exists for servers that decline to say. Keep that working."""
        with patch(
            "src.domains.brand.services.logo_service.httpx.AsyncClient",
            _client_factory(_handler(content_type="application/octet-stream")),
        ):
            result = await logo_service.download_and_store(
                "https://example.com/logo.png", "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
            )
            assert result.content_type == "image/png"
