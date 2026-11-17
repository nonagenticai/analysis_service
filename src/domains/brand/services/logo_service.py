"""Logo download, validation, and storage service.

Async port of legacy _download_logo() with validation and persistent storage.
"""

from __future__ import annotations

import logging
import mimetypes
import time
from typing import ClassVar
from uuid import uuid7

import httpx

from src.domains.brand.schemas import LogoAsset
from src.domains.brand.security import get_guarded
from src.domains.brand.services.storage_service import StorageService

logger = logging.getLogger(__name__)

# Content type to file extension mapping
_EXTENSION_MAP: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/svg+xml": "svg",
    "image/x-icon": "ico",
    "image/webp": "webp",
    "image/gif": "gif",
}


class LogoService:
    """Downloads, validates, and stores logo images."""

    ALLOWED_CONTENT_TYPES: ClassVar[set[str]] = {
        "image/png",
        "image/jpeg",
        "image/svg+xml",
        "image/x-icon",
        "image/webp",
        "image/gif",
    }
    #: Answers that carry no information about the bytes, and only these, may be
    #: overridden by the URL-extension guess. An empty header means the server
    #: said nothing; ``octet-stream`` is the standard "unknown binary". Anything
    #: else is a positive claim and is taken at its word.
    _UNINFORMATIVE_CONTENT_TYPES: ClassVar[frozenset[str]] = frozenset(
        {"", "application/octet-stream", "binary/octet-stream"}
    )
    MAX_SIZE_BYTES: int = 5 * 1024 * 1024  # 5 MB
    DOWNLOAD_TIMEOUT: float = 10.0

    def __init__(
        self,
        storage: StorageService,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._storage = storage
        self._http_client = http_client

    async def download_and_store(self, url: str, tenant_id: str) -> LogoAsset:
        """Download a logo from URL, validate, store, and return asset info.

        Args:
            url: URL of the logo image to download.
            tenant_id: Tenant identifier for organizing stored files.

        Returns:
            LogoAsset with storage path and metadata.

        Raises:
            ValueError: If the content type is not allowed, or the body is empty.
            BrandResponseTooLargeError: If the body exceeds ``MAX_SIZE_BYTES``.
                A ``ValueError``, so callers that degrade on ``ValueError``
                (the ``/brand/logo/download`` route's 400, ``BaseStage.execute``)
                behave exactly as they did for the old post-hoc size check.
            BrandSsrfBlockedError: If the URL or any redirect hop is unsafe.
            httpx.HTTPError: If download fails.
        """
        # 1. Download -- SSRF-gated on the caller-supplied URL and on every
        # redirect hop, and capped at MAX_SIZE_BYTES *while streaming*
        # (``get_guarded``). ``url`` arrives either straight off
        # /brand/logo/download's request body or as a ``src`` harvested from a
        # tenant-chosen page, so neither is trustworthy; redirects were
        # previously auto-followed, which let a public URL 302 into the tailnet,
        # and the body was previously read in full before its size was checked,
        # which let any permitted host OOM the pod with an arbitrarily large
        # "logo". The cap is passed here rather than checked below because a
        # check below is a check on bytes we have already paid for.
        logger.info("Downloading logo, url=%s, tenant=%s", url, tenant_id)
        dl_start = time.monotonic()
        if self._http_client:
            response = await get_guarded(self._http_client, url, max_bytes=self.MAX_SIZE_BYTES)
            response.raise_for_status()
        else:
            async with httpx.AsyncClient(
                timeout=self.DOWNLOAD_TIMEOUT,
                follow_redirects=False,
                headers={"User-Agent": "BrandExtractorService/1.0"},
            ) as client:
                response = await get_guarded(client, url, max_bytes=self.MAX_SIZE_BYTES)
                response.raise_for_status()
        dl_ms = int((time.monotonic() - dl_start) * 1000)
        logger.info(
            "Logo downloaded, url=%s, status=%d, duration_ms=%d", url, response.status_code, dl_ms
        )

        # 2. Validate content type.
        #
        # The URL-extension fallback exists for servers that decline to say what
        # they are serving. It must NOT overrule a server that did say: an
        # explicit ``application/json`` is the origin telling us these are not
        # image bytes, and a ``.png`` in the path is not evidence against it.
        # Honouring the path there stored arbitrary bytes as ``image/png`` --
        # asset_id, extension and stored content_type all claiming an image --
        # and every downstream consumer then treats them as one.
        #
        # Only genuinely uninformative answers get the fallback.
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in self.ALLOWED_CONTENT_TYPES:
            guessed = (
                mimetypes.guess_type(url)[0]
                if content_type in self._UNINFORMATIVE_CONTENT_TYPES
                else None
            )
            if guessed and guessed in self.ALLOWED_CONTENT_TYPES:
                content_type = guessed
            else:
                raise ValueError(
                    f"Content type '{content_type}' not allowed. "
                    f"Allowed: {self.ALLOWED_CONTENT_TYPES}"
                )

        # 3. Size is already enforced by the ``max_bytes`` cap above -- the body
        # cannot be larger than MAX_SIZE_BYTES because we never read that far.
        # The old post-hoc ``len(data) > MAX_SIZE_BYTES`` check is deliberately
        # gone rather than kept as belt-and-braces: leaving it would keep
        # signalling that the limit is enforced here, which is the exact
        # misreading that let the unbounded read live for so long.
        data = response.content

        if not data:
            raise ValueError("Empty response body")

        # 4. Generate asset ID
        asset_id = str(uuid7())

        # 5. Determine extension
        extension = _EXTENSION_MAP.get(content_type, "png")

        # 6. Store via StorageService
        stored_path = await self._storage.store(tenant_id, asset_id, data, extension)

        # 7. Determine source from URL
        source = self._detect_source(url)

        # 8. Return LogoAsset schema
        return LogoAsset(
            asset_id=asset_id,
            original_url=url,
            stored_path=stored_path,
            content_type=content_type,
            size_bytes=len(data),
            source=source,
        )

    def _detect_source(self, url: str) -> str:
        """Detect the logo source type from URL patterns."""
        url_lower = url.lower()
        if "favicon" in url_lower or url_lower.endswith(".ico"):
            return "favicon"
        if "apple-touch-icon" in url_lower:
            return "apple-touch-icon"
        if "og:" in url_lower or "opengraph" in url_lower:
            return "og:image"
        return "logo-tag"

    async def retrieve(self, stored_path: str) -> bytes | None:
        """Retrieve stored logo bytes."""
        return await self._storage.retrieve(stored_path)

    async def delete(self, stored_path: str) -> bool:
        """Delete a stored logo."""
        return await self._storage.delete(stored_path)
