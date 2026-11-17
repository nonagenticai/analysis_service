"""Unit tests for storage service."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.domains.brand.services.storage_service import StorageService


def _minio_service(tmp_path: Path) -> StorageService:
    """Build a StorageService bound to the minio backend (dual-backend)."""
    with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
        s = mock_settings.return_value.storage
        s.backend = "minio"
        s.local_path = str(tmp_path)
        s.minio_bucket = "analysis-service"
        s.minio_endpoint = "minio.test:9000"
        s.minio_access_key = "k"
        s.minio_secret_key = "s"
        s.minio_secure = False
        s.minio_external_endpoint = None
        s.presign_expiry_seconds = 3600
        return StorageService()


@pytest.fixture
def storage_service(tmp_path: Path) -> StorageService:
    """Create a StorageService with a temp directory."""
    with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
        settings = mock_settings.return_value
        settings.storage.backend = "local"
        settings.storage.local_path = str(tmp_path)
        return StorageService()


class TestStorageServiceInitialize:
    @pytest.mark.asyncio
    async def test_creates_local_directory(self, storage_service: StorageService) -> None:
        await storage_service.initialize()
        assert Path(storage_service._local_path).exists()


class TestStorageServiceStore:
    @pytest.mark.asyncio
    async def test_store_creates_file(self, storage_service: StorageService) -> None:
        data = b"fake image data"
        path = await storage_service.store("tenant1", "asset-abc", data, "png")

        assert Path(path).exists()
        assert Path(path).read_bytes() == data

    @pytest.mark.asyncio
    async def test_store_creates_nested_directory(self, storage_service: StorageService) -> None:
        path = await storage_service.store("tenant1", "asset-xyz", b"data", "jpg")

        # Should be at {local_path}/tenant1/logos/asset-xyz.jpg
        assert "tenant1" in path
        assert "logos" in path
        assert "asset-xyz.jpg" in path

    @pytest.mark.asyncio
    async def test_unsupported_backend_raises(self, tmp_path: Path) -> None:
        with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
            settings = mock_settings.return_value
            settings.storage.backend = "s3"
            settings.storage.local_path = str(tmp_path)
            svc = StorageService()

        with pytest.raises(ValueError, match="Unsupported"):
            await svc.store("tenant", "asset", b"data", "png")


class TestStorageServiceRetrieve:
    @pytest.mark.asyncio
    async def test_retrieve_existing_file(self, storage_service: StorageService) -> None:
        data = b"retrieve me"
        path = await storage_service.store("tenant1", "asset-1", data, "png")

        result = await storage_service.retrieve(path)
        assert result == data

    @pytest.mark.asyncio
    async def test_retrieve_missing_file_returns_none(
        self, storage_service: StorageService
    ) -> None:
        result = await storage_service.retrieve("/nonexistent/path.png")
        assert result is None


class TestStorageServiceDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_file(self, storage_service: StorageService) -> None:
        path = await storage_service.store("tenant1", "asset-del", b"delete me", "png")
        assert Path(path).exists()

        result = await storage_service.delete(path)
        assert result is True
        assert not Path(path).exists()

    @pytest.mark.asyncio
    async def test_delete_missing_file_returns_false(self, storage_service: StorageService) -> None:
        result = await storage_service.delete("/nonexistent/path.png")
        assert result is False


class TestStorageServiceExists:
    @pytest.mark.asyncio
    async def test_exists_true_for_stored_file(self, storage_service: StorageService) -> None:
        path = await storage_service.store("tenant1", "asset-ex", b"data", "png")
        assert await storage_service.exists(path) is True

    @pytest.mark.asyncio
    async def test_exists_false_for_missing_file(self, storage_service: StorageService) -> None:
        assert await storage_service.exists("/no/such/file.png") is False


class TestStorageServicePathConfinement:
    """Defense-in-depth: retrieve/delete/exists must refuse paths outside root."""

    @pytest.mark.asyncio
    async def test_retrieve_rejects_absolute_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
            mock_settings.return_value.storage.backend = "local"
            mock_settings.return_value.storage.local_path = str(root)
            svc = StorageService()
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"sensitive")
        assert await svc.retrieve(str(outside)) is None
        assert outside.read_bytes() == b"sensitive"

    @pytest.mark.asyncio
    async def test_retrieve_rejects_traversal(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
            mock_settings.return_value.storage.backend = "local"
            mock_settings.return_value.storage.local_path = str(root)
            svc = StorageService()
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"sensitive")
        assert await svc.retrieve("../secret.png") is None
        assert outside.read_bytes() == b"sensitive"

    @pytest.mark.asyncio
    async def test_delete_rejects_absolute_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
            mock_settings.return_value.storage.backend = "local"
            mock_settings.return_value.storage.local_path = str(root)
            svc = StorageService()
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"sensitive")
        assert await svc.delete(str(outside)) is False
        assert outside.exists()

    @pytest.mark.asyncio
    async def test_exists_rejects_path_outside_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        with patch("src.domains.brand.services.storage_service.get_settings") as mock_settings:
            mock_settings.return_value.storage.backend = "local"
            mock_settings.return_value.storage.local_path = str(root)
            svc = StorageService()
        outside = tmp_path / "secret.png"
        outside.write_bytes(b"sensitive")
        # Even though the file exists, exists() must report False — the API
        # contract is "exists *inside the storage root*", not "exists on disk".
        assert await svc.exists(str(outside)) is False


class TestStorageServiceMinioBackend:
    """Dual-backend: the minio backend must init and delegate without raising.

    This is the regression the prior facade migration missed (crashloop on the
    prod deploy, never caught by per-PR CI): a wrapper that raised at boot under
    ``STORAGE_BACKEND=minio``.
    """

    def test_minio_backend_constructs(self, tmp_path: Path) -> None:
        svc = _minio_service(tmp_path)
        assert svc._backend == "minio"

    @pytest.mark.asyncio
    async def test_minio_init_failure_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _minio_service(tmp_path)

        async def _boom() -> None:
            raise ConnectionError("minio unreachable at boot")

        monkeypatch.setattr(svc._storage, "initialize", _boom)
        # Anti-crashloop: a minio blip at boot must be swallowed, not raised.
        await svc.initialize()

    @pytest.mark.asyncio
    async def test_minio_store_returns_uri_reference(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _minio_service(tmp_path)

        async def _put(key: str, data: bytes, *, content_type: str) -> str:
            return key

        monkeypatch.setattr(svc._storage, "put_bytes", _put)
        ref = await svc.store("tenantA", "asset-9", b"img", "png")
        assert ref == "minio://analysis-service/tenantA/logos/asset-9.png"

    @pytest.mark.asyncio
    async def test_minio_retrieve_extracts_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = _minio_service(tmp_path)
        seen: dict[str, str] = {}

        async def _get(ref: str) -> bytes:
            seen["ref"] = ref
            return b"payload"

        monkeypatch.setattr(svc._storage, "get_bytes", _get)
        out = await svc.retrieve("minio://analysis-service/tenantA/logos/asset-9.png")
        assert out == b"payload"
        # The wrapper strips the minio://{bucket}/ prefix and passes the bare key.
        assert seen["ref"] == "tenantA/logos/asset-9.png"
