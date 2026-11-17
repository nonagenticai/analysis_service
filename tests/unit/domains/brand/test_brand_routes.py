"""Unit tests for API route handlers in routes.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from src.domains.brand.routes import (
    _get_pipeline_runner,
    _get_tenant_id,
    batch_extract,
    delete_logo,
    download_logo,
    extract_brand,
    extract_from_url,
    serve_logo,
)
from src.domains.brand.schemas import (
    AnalysisData,
    BatchExtractRequest,
    BrandExtractRequest,
    BrandFromUrlRequest,
    BrandResponse,
    ExtractionMetadata,
    LogoDownloadRequest,
    PageData,
)

# BrandRepository is imported locally inside route functions, so we must
# patch it at the module where it lives.
_REPO = "src.domains.brand.services.brand_repository.BrandRepository"


def _make_request(
    tenant_id: str = "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e",
    pipeline_runner: MagicMock | None = None,
    brand_cache: AsyncMock | None = None,
    logo_service: AsyncMock | None = None,
    event_publisher: AsyncMock | None = None,
    http_client: AsyncMock | None = None,
) -> MagicMock:
    """Build a mock Request with optional app.state services."""
    request = MagicMock()
    request.state.tenant_id = tenant_id
    request.app.state.pipeline_runner = pipeline_runner or MagicMock()
    request.app.state.http_client = http_client
    request.app.state.logo_service = logo_service
    for attr, val in [
        ("brand_cache", brand_cache),
        ("event_publisher", event_publisher),
    ]:
        if val is not None:
            setattr(request.app.state, attr, val)
        else:
            setattr(request.app.state, attr, None)
    return request


class TestGetPipelineRunner:
    def test_returns_runner_from_state(self) -> None:
        runner = MagicMock()
        request = MagicMock()
        request.app.state.pipeline_runner = runner
        assert _get_pipeline_runner(request) is runner


class TestGetTenantId:
    def test_returns_tenant_from_state(self) -> None:
        request = MagicMock()
        request.state.tenant_id = "29161dbd-657f-515d-99b4-62d787def011"
        assert _get_tenant_id(request) == "29161dbd-657f-515d-99b4-62d787def011"

    def test_missing_tenant_is_401_not_the_removed_default_sentinel(self) -> None:
        """DELIBERATE STATUS CHANGE. See the twin in test_brands_api.py: the
        "default" fallback pooled every unidentified caller into one shared
        tenant scope, and 0.67.0 deleted the concept outright."""
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401

    def test_the_nil_uuid_is_refused(self) -> None:
        request = MagicMock()
        request.state.tenant_id = "00000000-0000-0000-0000-000000000000"
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401


class TestExtractBrand:
    @pytest.mark.asyncio
    async def test_extract_from_analysis_basic(self) -> None:
        request = _make_request()
        body = BrandExtractRequest(
            analysis=AnalysisData(pages=[PageData(url="https://test.com", title="Test Corp")])
        )

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await extract_brand(body=body, request=request, db=AsyncMock())

        assert result.domain == "test.com"

    @pytest.mark.asyncio
    async def test_persist_failure_does_not_break(self) -> None:
        request = _make_request()
        body = BrandExtractRequest(analysis=AnalysisData(pages=[]))

        with patch(
            f"{_REPO}.log_extraction",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("DB down"),
        ):
            result = await extract_brand(body=body, request=request, db=AsyncMock())

        assert isinstance(result, BrandResponse)

    @pytest.mark.asyncio
    async def test_caches_result_when_domain_present(self) -> None:
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock()
        request = _make_request(brand_cache=brand_cache)
        body = BrandExtractRequest(
            analysis=AnalysisData(pages=[PageData(url="https://test.com", title="Test")])
        )

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            await extract_brand(body=body, request=request, db=AsyncMock())

        brand_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_failure_does_not_break(self) -> None:
        brand_cache = AsyncMock()
        brand_cache.set = AsyncMock(side_effect=ConnectionError("Redis down"))
        request = _make_request(brand_cache=brand_cache)
        body = BrandExtractRequest(
            analysis=AnalysisData(pages=[PageData(url="https://test.com", title="Test")])
        )

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await extract_brand(body=body, request=request, db=AsyncMock())

        assert result.domain == "test.com"


class TestExtractFromUrl:
    @pytest.mark.asyncio
    async def test_returns_cached_result(self) -> None:
        cached = BrandResponse(name="Cached", domain="cached.com")
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=cached)

        request = _make_request(brand_cache=brand_cache)
        body = BrandFromUrlRequest(url="https://cached.com")
        result = await extract_from_url(body=body, request=request, db=AsyncMock())

        assert result.name == "Cached"

    @pytest.mark.asyncio
    async def test_extracts_from_url_on_cache_miss(self) -> None:
        brand_result = BrandResponse(name="Fresh", domain="fresh.com")
        runner = AsyncMock()
        runner.run = AsyncMock(return_value=(brand_result, []))
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=None)
        brand_cache.set = AsyncMock()

        request = _make_request(pipeline_runner=runner, brand_cache=brand_cache)
        body = BrandFromUrlRequest(url="https://fresh.com")

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await extract_from_url(body=body, request=request, db=AsyncMock())

        assert result.name == "Fresh"

    @pytest.mark.asyncio
    async def test_extraction_failure_raises_502(self) -> None:
        runner = AsyncMock()
        runner.run = AsyncMock(side_effect=OSError("Connection timeout"))
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=None)

        request = _make_request(pipeline_runner=runner, brand_cache=brand_cache)
        body = BrandFromUrlRequest(url="https://broken.com")

        with pytest.raises(HTTPException) as exc_info:
            await extract_from_url(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_with_custom_config(self) -> None:
        brand_result = BrandResponse(name="Custom", domain="custom.com")
        runner = AsyncMock()
        runner.run = AsyncMock(return_value=(brand_result, []))
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=None)
        brand_cache.set = AsyncMock()

        request = _make_request(pipeline_runner=runner, brand_cache=brand_cache)
        body = BrandFromUrlRequest(
            url="https://custom.com",
            config={"max_css_stylesheets": 3},
        )

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await extract_from_url(body=body, request=request, db=AsyncMock())

        assert result.name == "Custom"

    @pytest.mark.asyncio
    async def test_publishes_event_on_success(self) -> None:
        brand_result = BrandResponse(
            name="Test",
            domain="test.com",
            metadata=ExtractionMetadata(confidence=0.9),
        )
        runner = AsyncMock()
        runner.run = AsyncMock(return_value=(brand_result, []))
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(return_value=None)
        brand_cache.set = AsyncMock()
        event_publisher = AsyncMock()
        event_publisher.publish_extraction = AsyncMock()

        request = _make_request(
            pipeline_runner=runner,
            brand_cache=brand_cache,
            event_publisher=event_publisher,
        )
        body = BrandFromUrlRequest(url="https://test.com")

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            await extract_from_url(body=body, request=request, db=AsyncMock())

        event_publisher.publish_extraction.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_lookup_failure_does_not_break(self) -> None:
        brand_result = BrandResponse(name="Test", domain="test.com")
        runner = AsyncMock()
        runner.run = AsyncMock(return_value=(brand_result, []))
        brand_cache = AsyncMock()
        brand_cache.get_by_url = AsyncMock(side_effect=ConnectionError("Redis down"))
        brand_cache.set = AsyncMock()

        request = _make_request(pipeline_runner=runner, brand_cache=brand_cache)
        body = BrandFromUrlRequest(url="https://test.com")

        with (
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await extract_from_url(body=body, request=request, db=AsyncMock())

        assert result.name == "Test"


class TestBatchExtract:
    @pytest.mark.asyncio
    async def test_batch_all_succeed(self) -> None:
        async def mock_run(ctx):
            domain = ctx.url.replace("https://", "")
            return BrandResponse(name=f"Brand-{ctx.url}", domain=domain), []

        runner = AsyncMock()
        runner.run = mock_run

        request = _make_request(pipeline_runner=runner)
        body = BatchExtractRequest(urls=["https://a.com", "https://b.com"])

        # get_session_maker() is awaited and returns a callable session_maker.
        # session_maker() is called (not awaited) and returns an async context manager.
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker = MagicMock(return_value=mock_session_cm)

        with (
            patch(
                "src.shared.database.session.get_session_maker",
                new_callable=AsyncMock,
                return_value=mock_session_maker,
            ),
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await batch_extract(body=body, request=request)

        assert result.total == 2
        assert result.succeeded == 2
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_batch_partial_failure(self) -> None:
        async def mock_run(ctx):
            if "bad" in ctx.url:
                raise ValueError("Extraction failed")
            return BrandResponse(name="Good", domain="good.com"), []

        runner = AsyncMock()
        runner.run = mock_run

        request = _make_request(pipeline_runner=runner)
        body = BatchExtractRequest(urls=["https://good.com", "https://bad.com"])

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker = MagicMock(return_value=mock_session_cm)

        with (
            patch(
                "src.shared.database.session.get_session_maker",
                new_callable=AsyncMock,
                return_value=mock_session_maker,
            ),
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await batch_extract(body=body, request=request)

        assert result.total == 2
        assert result.succeeded == 1
        assert result.failed == 1
        assert "https://bad.com" in result.errors

    @pytest.mark.asyncio
    async def test_batch_off_tuple_exception_does_not_cancel_siblings(self) -> None:
        """A RuntimeError from runner.run() must not take out the other URLs.

        Regression for the narrow-except bug: ``except (OSError, httpx.HTTPError,
        ValueError)`` let RuntimeError / KeyError / AssertionError escape
        _extract_one, escape the gather, and silently cancel every sibling task.
        With the widened ``except Exception`` (+ gather return_exceptions=True),
        one bad URL is captured in `errors` and the rest succeed.
        """

        async def mock_run(ctx):
            if "bad" in ctx.url:
                raise RuntimeError("LLM payload malformed")  # off-tuple
            return BrandResponse(name="Good", domain="good.com"), []

        runner = AsyncMock()
        runner.run = mock_run

        request = _make_request(pipeline_runner=runner)
        body = BatchExtractRequest(
            urls=[
                "https://good-1.com",
                "https://bad.com",
                "https://good-2.com",
            ],
        )

        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session_cm = AsyncMock()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=False)
        mock_session_maker = MagicMock(return_value=mock_session_cm)

        with (
            patch(
                "src.shared.database.session.get_session_maker",
                new_callable=AsyncMock,
                return_value=mock_session_maker,
            ),
            patch(f"{_REPO}.log_extraction", new_callable=AsyncMock),
            patch(f"{_REPO}.upsert_brand", new_callable=AsyncMock),
        ):
            result = await batch_extract(body=body, request=request)

        assert result.total == 3
        assert result.succeeded == 2
        assert result.failed == 1
        assert "https://bad.com" in result.errors
        assert "LLM payload malformed" in result.errors["https://bad.com"]
        # The two good URLs completed — they were NOT cancelled mid-flight.
        assert "https://good-1.com" in result.results
        assert "https://good-2.com" in result.results


class TestDownloadLogo:
    @pytest.mark.asyncio
    async def test_logo_service_not_available(self) -> None:
        request = _make_request()
        request.app.state = MagicMock(spec=[])
        request.state.tenant_id = "49f8b31f-dcb0-5e78-9ab6-e67313b6654e"

        body = LogoDownloadRequest(url="https://example.com/logo.png")
        with pytest.raises(HTTPException) as exc_info:
            await download_logo(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_download_success(self) -> None:
        asset = MagicMock()
        asset.asset_id = "abc-123"
        asset.stored_path = "/tmp/logos/abc-123.png"
        asset.content_type = "image/png"
        asset.size_bytes = 1024
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(return_value=asset)

        request = _make_request(logo_service=logo_service)
        body = LogoDownloadRequest(url="https://example.com/logo.png")

        with patch(f"{_REPO}.save_logo_asset", new_callable=AsyncMock):
            result = await download_logo(body=body, request=request, db=AsyncMock())

        assert result.asset_id == "abc-123"
        assert result.content_type == "image/png"
        assert "/api/brand/logo/abc-123" in result.serve_url

    @pytest.mark.asyncio
    async def test_download_invalid_url_raises_400(self) -> None:
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(side_effect=ValueError("Invalid URL"))

        request = _make_request(logo_service=logo_service)
        body = LogoDownloadRequest(url="not-a-url")

        with pytest.raises(HTTPException) as exc_info:
            await download_logo(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_download_failure_raises_502(self) -> None:
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(side_effect=OSError("Connection refused"))

        request = _make_request(logo_service=logo_service)
        body = LogoDownloadRequest(url="https://example.com/logo.png")

        with pytest.raises(HTTPException) as exc_info:
            await download_logo(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 502

    @pytest.mark.asyncio
    async def test_download_uses_authenticated_tenant_not_body(self) -> None:
        """The body cannot choose which tenant's storage prefix gets written.

        The request model no longer carries a tenant field at all, and the blob
        is stored before the DB row is inserted, so an accepted override would
        be a cross-tenant write that RLS could not undo.
        """
        assert "tenant_id" not in LogoDownloadRequest.model_fields

        asset = MagicMock()
        asset.asset_id = "abc-123"
        asset.stored_path = "/tmp/logos/abc-123.png"
        asset.content_type = "image/png"
        asset.size_bytes = 512
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(return_value=asset)

        request = _make_request(logo_service=logo_service)
        body = LogoDownloadRequest(url="https://example.com/logo.png")

        with patch(f"{_REPO}.save_logo_asset", new_callable=AsyncMock):
            await download_logo(body=body, request=request, db=AsyncMock())

        logo_service.download_and_store.assert_called_once_with(
            "https://example.com/logo.png", "e04c4af8-4ed1-5ab2-b369-1b47e0a2528e"
        )

    @pytest.mark.asyncio
    async def test_save_asset_failure_does_not_break(self) -> None:
        asset = MagicMock()
        asset.asset_id = "abc-123"
        asset.stored_path = "/tmp/logos/abc-123.png"
        asset.content_type = "image/png"
        asset.size_bytes = 512
        logo_service = AsyncMock()
        logo_service.download_and_store = AsyncMock(return_value=asset)

        request = _make_request(logo_service=logo_service)
        body = LogoDownloadRequest(url="https://example.com/logo.png")

        with patch(
            f"{_REPO}.save_logo_asset",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("DB error"),
        ):
            result = await download_logo(body=body, request=request, db=AsyncMock())

        assert result.asset_id == "abc-123"


class TestServeLogo:
    @pytest.mark.asyncio
    async def test_logo_service_not_available(self) -> None:
        request = _make_request()
        request.app.state = MagicMock(spec=[])

        with pytest.raises(HTTPException) as exc_info:
            await serve_logo(asset_id="abc-123", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_asset_not_found(self) -> None:
        logo_service = AsyncMock()
        request = _make_request(logo_service=logo_service)

        with patch(f"{_REPO}.get_logo_asset", new_callable=AsyncMock, return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await serve_logo(asset_id="missing", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_file_not_found_on_disk(self) -> None:
        logo_service = AsyncMock()
        logo_service.retrieve = AsyncMock(return_value=None)
        request = _make_request(logo_service=logo_service)
        record = MagicMock(storage_path="/tmp/gone.png", content_type="image/png")

        with patch(f"{_REPO}.get_logo_asset", new_callable=AsyncMock, return_value=record):
            with pytest.raises(HTTPException) as exc_info:
                await serve_logo(asset_id="abc", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_serves_logo_data(self) -> None:
        logo_data = b"PNG data"
        logo_service = AsyncMock()
        logo_service.retrieve = AsyncMock(return_value=logo_data)
        request = _make_request(logo_service=logo_service)
        record = MagicMock(storage_path="/tmp/logo.png", content_type="image/png")

        with patch(f"{_REPO}.get_logo_asset", new_callable=AsyncMock, return_value=record):
            result = await serve_logo(asset_id="abc", request=request, db=AsyncMock())

        assert result.body == logo_data
        assert result.media_type == "image/png"


class TestDeleteLogo:
    @pytest.mark.asyncio
    async def test_logo_service_not_available(self) -> None:
        request = _make_request()
        request.app.state = MagicMock(spec=[])

        with pytest.raises(HTTPException) as exc_info:
            await delete_logo(asset_id="abc", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_asset_not_found(self) -> None:
        logo_service = AsyncMock()
        request = _make_request(logo_service=logo_service)

        with patch(f"{_REPO}.get_logo_asset", new_callable=AsyncMock, return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await delete_logo(asset_id="missing", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_deletes_successfully(self) -> None:
        logo_service = AsyncMock()
        logo_service.delete = AsyncMock()
        request = _make_request(logo_service=logo_service)
        record = MagicMock(storage_path="/tmp/logo.png")

        with (
            patch(f"{_REPO}.get_logo_asset", new_callable=AsyncMock, return_value=record),
            patch(f"{_REPO}.delete_logo_asset", new_callable=AsyncMock),
        ):
            result = await delete_logo(asset_id="abc-123", request=request, db=AsyncMock())

        assert result["deleted"] is True
        assert result["asset_id"] == "abc-123"
        logo_service.delete.assert_called_once_with("/tmp/logo.png")


class TestFromUrlRouteRegistration:
    """Regression: POST /brand/from-url must be bound to ``extract_from_url`` (a
    BODY param), NOT to the ``_check_url_cache`` helper (whose ``tenant_id``/``url``
    become required QUERY params, so every JSON-body POST 422s). The pre-existing
    handler tests call ``extract_from_url`` directly, so they never caught the
    misplaced decorator — this asserts the route wiring itself."""

    def _from_url_route(self):  # type: ignore[no-untyped-def]
        from src.domains.brand.routes import router

        return next(
            r
            for r in router.routes
            if getattr(r, "path", "").endswith("/brand/from-url")
            and "POST" in getattr(r, "methods", set())
        )

    def test_route_endpoint_is_extract_from_url(self) -> None:
        assert self._from_url_route().endpoint is extract_from_url

    def test_route_takes_body_not_query_params(self) -> None:
        route = self._from_url_route()
        assert [p.name for p in route.dependant.body_params] == ["body"]
        # No required query params — a JSON-body POST must not 422 on missing query.
        assert [p.name for p in route.dependant.query_params if p.required] == []
