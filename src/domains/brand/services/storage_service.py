"""Logo file storage service.

Manages local and MinIO/S3 file storage for downloaded brand logos. Object I/O
is delegated to the shared ``w2v_common.storage.StorageService`` facade (the
fleet-wide consolidation of the per-service MinIO clients). This thin wrapper
keeps the brand-domain concerns the facade does not know about: the
``{tenant}/logos/{asset}.{ext}`` key layout, the ``minio://{bucket}/{key}``
reference format persisted in DB rows, the traversal guard on stale/tampered
local paths, and content-type guessing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from w2v_common.storage import StorageService as CommonStorage

from src.config import get_settings

logger = logging.getLogger(__name__)


class StorageService:
    """Manages logo file storage (local or MinIO) over the shared facade."""

    def __init__(self) -> None:
        settings = get_settings()
        self._backend = settings.storage.backend
        self._local_path = Path(settings.storage.local_path)
        self._minio_bucket = settings.storage.minio_bucket
        self._storage = CommonStorage(settings.storage)

    async def initialize(self) -> None:
        """Initialize storage backend.

        MinIO probe is best-effort: if the upstream is unreachable at boot we
        log and continue so the service still starts and serves non-storage
        routes. Bucket creation falls through to the first ``store()`` call.
        Crashing here turned a minio blip into fleet-wide w2v CrashLoop on
        2026-05-11.
        """
        if self._backend == "minio":
            try:
                await self._storage.initialize()
                logger.info("MinIO storage initialized: bucket=%s", self._minio_bucket)
            except Exception as exc:  # noqa: BLE001 — startup must tolerate any minio failure
                logger.warning(
                    "MinIO bucket probe failed at startup (%s: %s) — proceeding "
                    "without verification; bucket will be created on first write.",
                    type(exc).__name__,
                    exc,
                )
        else:
            await self._storage.initialize()
            logger.info("Local storage initialized at %s", self._local_path)

    async def store(self, tenant_id: str, asset_id: str, data: bytes, extension: str) -> str:
        """Store logo data and return the storage path.

        Args:
            tenant_id: Tenant identifier for directory isolation.
            asset_id: Unique asset identifier for the filename.
            data: Raw image bytes.
            extension: File extension (e.g. "png", "svg").

        Returns:
            Storage path (local file path or ``minio://`` URI).

        Raises:
            ValueError: If storage backend is not supported.
        """
        object_name = f"{tenant_id}/logos/{asset_id}.{extension}"
        content_type = self._guess_content_type(extension)
        ref = await self._storage.put_bytes(object_name, data, content_type=content_type)
        if self._backend == "minio":
            path = f"minio://{self._minio_bucket}/{object_name}"
            logger.info("Stored logo in MinIO: %s (%d bytes)", path, len(data))
            return path
        logger.info("Stored logo: %s (%d bytes)", ref, len(data))
        return ref

    async def retrieve(self, path: str) -> bytes | None:
        """Retrieve logo data from storage path."""
        if path.startswith("minio://"):
            return await self._storage.get_bytes(self._minio_key(path))
        file_path = self._safe_local_path(path)
        if file_path is None:
            return None
        data = await self._storage.get_bytes(str(file_path))
        if data is None:
            logger.warning("Logo file not found: %s", path)
        return data

    async def delete(self, path: str) -> bool:
        """Delete a stored logo file."""
        if path.startswith("minio://"):
            return await self._storage.delete_object(self._minio_key(path))
        file_path = self._safe_local_path(path)
        if file_path is None or not file_path.exists():
            return False
        await self._storage.delete_object(str(file_path))
        logger.info("Deleted logo: %s", path)
        return True

    async def exists(self, path: str) -> bool:
        """Check if a file exists at the given path."""
        if path.startswith("minio://"):
            return await self._storage.object_exists_key(self._minio_key(path)) is True
        file_path = self._safe_local_path(path)
        if file_path is None:
            return False
        return file_path.exists()

    @staticmethod
    def _minio_key(path: str) -> str:
        """Extract the object key from a ``minio://{bucket}/{key}`` reference."""
        stripped = path[len("minio://") :]
        _bucket, _, object_name = stripped.partition("/")
        return object_name

    def _safe_local_path(self, raw: str) -> Path | None:
        """Resolve ``raw`` and reject anything outside ``self._local_path``.

        Defense in depth: ``store`` only emits paths under the configured root,
        so the DB row should always round-trip cleanly. A rejection here means
        either a stale row from a different configuration or a tampered DB
        entry — return ``None`` (treated as "not found" by callers) rather than
        honouring the traversal.
        """
        root = self._local_path.resolve()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self._local_path / raw
        try:
            resolved = candidate.resolve()
        except OSError, RuntimeError:
            logger.warning("Could not resolve storage path: %s", raw)
            return None
        if not resolved.is_relative_to(root):
            logger.warning("Rejected path outside storage root: %s", raw)
            return None
        return resolved

    @staticmethod
    def _guess_content_type(extension: str) -> str:
        """Map file extension to MIME type."""
        mapping = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "svg": "image/svg+xml",
            "webp": "image/webp",
            "ico": "image/x-icon",
        }
        return mapping.get(extension.lower(), "application/octet-stream")
