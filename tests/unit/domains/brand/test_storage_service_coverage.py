"""Extended tests for storage_service.py -- covers the facade delegation for the
MinIO backend, guess_content_type, and local edge cases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.domains.brand.services.storage_service import StorageService


@pytest.fixture
def minio_storage(tmp_path: Path) -> StorageService:
    """Create a StorageService with minio backend."""
    with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.storage.backend = "minio"
        settings.storage.local_path = str(tmp_path)
        settings.storage.minio_endpoint = "localhost:9000"
        settings.storage.minio_access_key = "minioadmin"
        settings.storage.minio_secret_key = "minioadmin"  # pragma: allowlist secret
        settings.storage.minio_bucket = "test-bucket"
        settings.storage.minio_secure = False
        settings.storage.minio_external_endpoint = None
        settings.storage.presign_expiry_seconds = 3600
        return StorageService()


@pytest.fixture
def local_storage(tmp_path: Path) -> StorageService:
    """Create a StorageService with local backend."""
    with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.storage.backend = "local"
        settings.storage.local_path = str(tmp_path)
        settings.storage.minio_endpoint = "localhost:9000"
        settings.storage.minio_access_key = ""
        settings.storage.minio_secret_key = ""
        settings.storage.minio_bucket = "test"
        settings.storage.minio_secure = False
        return StorageService()


class TestGuessContentType:
    def test_png(self) -> None:
        assert StorageService._guess_content_type("png") == "image/png"

    def test_jpg(self) -> None:
        assert StorageService._guess_content_type("jpg") == "image/jpeg"

    def test_jpeg(self) -> None:
        assert StorageService._guess_content_type("jpeg") == "image/jpeg"

    def test_gif(self) -> None:
        assert StorageService._guess_content_type("gif") == "image/gif"

    def test_svg(self) -> None:
        assert StorageService._guess_content_type("svg") == "image/svg+xml"

    def test_webp(self) -> None:
        assert StorageService._guess_content_type("webp") == "image/webp"

    def test_ico(self) -> None:
        assert StorageService._guess_content_type("ico") == "image/x-icon"

    def test_unknown(self) -> None:
        assert StorageService._guess_content_type("bmp") == "application/octet-stream"

    def test_case_insensitive(self) -> None:
        assert StorageService._guess_content_type("PNG") == "image/png"


class TestMinioInitialize:
    @pytest.mark.asyncio
    async def test_delegates_to_facade_initialize(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = {"init": False}

        async def _init() -> None:
            called["init"] = True

        monkeypatch.setattr(minio_storage._storage, "initialize", _init)
        await minio_storage.initialize()
        assert called["init"] is True

    @pytest.mark.asyncio
    async def test_init_tolerates_facade_failure(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom() -> None:
            raise ConnectionError("minio unreachable at boot")

        monkeypatch.setattr(minio_storage._storage, "initialize", _boom)
        # Anti-crashloop: a minio blip at boot must be swallowed, not raised.
        await minio_storage.initialize()


class TestMinioStore:
    @pytest.mark.asyncio
    async def test_store_minio_returns_uri(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}

        async def _put(key: str, data: bytes, *, content_type: str = "") -> str:
            seen["key"] = key
            seen["content_type"] = content_type
            return key

        monkeypatch.setattr(minio_storage._storage, "put_bytes", _put)
        path = await minio_storage.store("tenant1", "asset-123", b"image data", "png")
        assert path == "minio://test-bucket/tenant1/logos/asset-123.png"
        assert seen["key"] == "tenant1/logos/asset-123.png"
        assert seen["content_type"] == "image/png"


class TestMinioRetrieve:
    @pytest.mark.asyncio
    async def test_retrieve_minio(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}

        async def _get(ref: str) -> bytes | None:
            seen["ref"] = ref
            return b"image data"

        monkeypatch.setattr(minio_storage._storage, "get_bytes", _get)
        result = await minio_storage.retrieve("minio://test-bucket/tenant1/logos/asset.png")
        assert result == b"image data"
        # The wrapper strips the minio://{bucket}/ prefix and passes the bare key.
        assert seen["ref"] == "tenant1/logos/asset.png"

    @pytest.mark.asyncio
    async def test_retrieve_minio_missing(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _get(ref: str) -> bytes | None:
            return None

        monkeypatch.setattr(minio_storage._storage, "get_bytes", _get)
        result = await minio_storage.retrieve("minio://test-bucket/missing.png")
        assert result is None


class TestMinioDelete:
    @pytest.mark.asyncio
    async def test_delete_minio(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, str] = {}

        async def _del(key: str, *, bucket: str | None = None) -> bool:
            seen["key"] = key
            return True

        monkeypatch.setattr(minio_storage._storage, "delete_object", _del)
        result = await minio_storage.delete("minio://test-bucket/tenant1/logos/asset.png")
        assert result is True
        assert seen["key"] == "tenant1/logos/asset.png"

    @pytest.mark.asyncio
    async def test_delete_minio_failure(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _del(key: str, *, bucket: str | None = None) -> bool:
            return False

        monkeypatch.setattr(minio_storage._storage, "delete_object", _del)
        result = await minio_storage.delete("minio://test-bucket/asset.png")
        assert result is False


class TestMinioExists:
    @pytest.mark.asyncio
    async def test_exists_minio_true(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _ex(key: str, *, bucket: str | None = None) -> bool | None:
            return True

        monkeypatch.setattr(minio_storage._storage, "object_exists_key", _ex)
        assert await minio_storage.exists("minio://test-bucket/tenant1/logos/asset.png") is True

    @pytest.mark.asyncio
    async def test_exists_minio_false(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _ex(key: str, *, bucket: str | None = None) -> bool | None:
            return False

        monkeypatch.setattr(minio_storage._storage, "object_exists_key", _ex)
        assert await minio_storage.exists("minio://test-bucket/missing.png") is False

    @pytest.mark.asyncio
    async def test_exists_minio_indeterminate_is_false(
        self, minio_storage: StorageService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _ex(key: str, *, bucket: str | None = None) -> bool | None:
            return None

        monkeypatch.setattr(minio_storage._storage, "object_exists_key", _ex)
        # Tri-state None (indeterminate) collapses to False for the bool contract.
        assert await minio_storage.exists("minio://test-bucket/x.png") is False


class TestStorageServiceLocalEdgeCases:
    @pytest.mark.asyncio
    async def test_retrieve_local_file(self, local_storage: StorageService) -> None:
        path = await local_storage.store("tenant1", "asset-1", b"test data", "png")
        result = await local_storage.retrieve(path)
        assert result == b"test data"

    @pytest.mark.asyncio
    async def test_exists_local_true(self, local_storage: StorageService) -> None:
        path = await local_storage.store("tenant1", "asset-2", b"data", "png")
        assert await local_storage.exists(path) is True

    @pytest.mark.asyncio
    async def test_exists_local_false(self, local_storage: StorageService) -> None:
        assert await local_storage.exists("/no/such/path.png") is False
