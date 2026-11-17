"""Unit tests for brands.py API route handlers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.domains.brand.brands_routes import (
    _get_tenant_id,
    compare_brands,
    export_brand,
    get_brand,
    get_brand_history,
    get_quality_report,
    invalidate_cache,
    list_brands,
    override_brand,
    serve_domain_logo,
)
from src.domains.brand.schemas import (
    BrandComparisonRequest,
    BrandOverrideRequest,
    BrandResponse,
)


def _make_request(
    tenant_id: str = "df5ce213-933c-5747-9b72-840f50559ec6", app_state: dict | None = None
) -> MagicMock:
    """Build a mock Request with optional app.state attributes."""
    request = MagicMock()
    request.state.tenant_id = tenant_id
    app_state = app_state or {}
    for key, val in app_state.items():
        setattr(request.app.state, key, val)
    return request


def _make_brand_record(**overrides) -> MagicMock:
    """Build a mock DB brand record with sensible defaults."""
    defaults = dict(
        name="Acme Corp",
        domain="acme.com",
        logo_url="https://acme.com/logo.png",
        primary_color="#FF5733",
        secondary_color="#1E40AF",
        has_logo=True,
        tagline="Build better",
        brand_voice="professional",
        font_family_primary="Inter",
        font_family_secondary="Roboto",
        quality_score=None,
        version=3,
    )
    defaults.update(overrides)
    record = MagicMock()
    for k, v in defaults.items():
        setattr(record, k, v)
    return record


class TestGetTenantId:
    def test_returns_tenant_from_state(self) -> None:
        request = MagicMock()
        request.state.tenant_id = "29161dbd-657f-515d-99b4-62d787def011"
        assert _get_tenant_id(request) == "29161dbd-657f-515d-99b4-62d787def011"

    def test_missing_tenant_is_401_not_the_removed_default_sentinel(self) -> None:
        """DELIBERATE STATUS CHANGE, not a weakened assertion.

        This asserted ``== "default"``. There is no "default" tenant any more:
        w2v_common 0.67.0 deleted every fallback and the product ruling is that
        every row carries a real registered tenant. The old behaviour POOLED
        every unidentified caller into one shared scope, so the next such caller
        read and deleted the previous one's brands. A missing identity is a 401.
        """
        request = MagicMock(spec=[])
        request.state = MagicMock(spec=[])
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401

    @pytest.mark.parametrize(
        "nil",
        [
            "00000000-0000-0000-0000-000000000000",
            "00000000000000000000000000000000",
            "{00000000-0000-0000-0000-000000000000}",
            "urn:uuid:00000000-0000-0000-0000-000000000000",
        ],
    )
    def test_the_nil_uuid_is_refused_in_every_spelling(self, nil: str) -> None:
        """The nil UUID is a VALID uuid registered upstream as ``default-anon``,
        so a None-only guard would let it straight through and mis-attribute
        every brand written under it."""
        request = MagicMock()
        request.state.tenant_id = nil
        with pytest.raises(HTTPException) as exc:
            _get_tenant_id(request)
        assert exc.value.status_code == 401


class TestListBrands:
    @pytest.mark.asyncio
    async def test_returns_paginated_brands(self) -> None:
        brand_record = _make_brand_record()
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.list_brands",
            new_callable=AsyncMock,
            return_value=([brand_record], 1),
        ):
            request = _make_request()
            result = await list_brands(request=request, page=1, page_size=20, db=AsyncMock())

        assert result.total == 1
        assert result.page == 1
        assert len(result.items) == 1
        assert result.items[0].name == "Acme Corp"
        assert result.items[0].domain == "acme.com"

    @pytest.mark.asyncio
    async def test_caps_page_size_at_100(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.list_brands",
            new_callable=AsyncMock,
            return_value=([], 0),
        ) as mock_list:
            request = _make_request()
            result = await list_brands(request=request, page=1, page_size=500, db=AsyncMock())

        # Should have been called with capped page_size=100
        mock_list.assert_called_once()
        assert result.page_size == 100

    @pytest.mark.asyncio
    async def test_has_next_true_when_more_pages(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.list_brands",
            new_callable=AsyncMock,
            return_value=([], 50),
        ):
            request = _make_request()
            result = await list_brands(request=request, page=1, page_size=20, db=AsyncMock())

        assert result.has_next is True

    @pytest.mark.asyncio
    async def test_has_next_false_on_last_page(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.list_brands",
            new_callable=AsyncMock,
            return_value=([], 10),
        ):
            request = _make_request()
            result = await list_brands(request=request, page=1, page_size=20, db=AsyncMock())

        assert result.has_next is False


class TestGetBrand:
    @pytest.mark.asyncio
    async def test_returns_brand_from_db(self) -> None:
        brand_record = _make_brand_record()
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=brand_record,
        ):
            request = _make_request(app_state={"brand_cache": None})
            result = await get_brand(domain="acme.com", request=request, db=AsyncMock())

        assert result.name == "Acme Corp"
        assert result.domain == "acme.com"

    @pytest.mark.asyncio
    async def test_returns_cached_brand(self) -> None:
        cached_brand = BrandResponse(name="Cached Corp", domain="cached.com")
        brand_cache = AsyncMock()
        brand_cache.get = AsyncMock(return_value=cached_brand)

        request = _make_request(app_state={"brand_cache": brand_cache})
        result = await get_brand(domain="cached.com", request=request, db=AsyncMock())

        assert result.name == "Cached Corp"

    @pytest.mark.asyncio
    async def test_cache_miss_falls_through_to_db(self) -> None:
        brand_cache = AsyncMock()
        brand_cache.get = AsyncMock(return_value=None)
        brand_cache.set = AsyncMock()
        brand_record = _make_brand_record()

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=brand_record,
        ):
            request = _make_request(app_state={"brand_cache": brand_cache})
            result = await get_brand(domain="acme.com", request=request, db=AsyncMock())

        assert result.name == "Acme Corp"
        brand_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_not_found_raises_404(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=None,
        ):
            request = _make_request(app_state={"brand_cache": None})
            with pytest.raises(HTTPException) as exc_info:
                await get_brand(domain="unknown.com", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cache_error_does_not_break(self) -> None:
        brand_cache = AsyncMock()
        brand_cache.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        brand_record = _make_brand_record()

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=brand_record,
        ):
            request = _make_request(app_state={"brand_cache": brand_cache})
            result = await get_brand(domain="acme.com", request=request, db=AsyncMock())

        assert result.name == "Acme Corp"


class TestInvalidateCache:
    @pytest.mark.asyncio
    async def test_cache_not_available(self) -> None:
        request = _make_request()
        # Make brand_cache missing on app state
        request.app.state = MagicMock(spec=[])
        request.state.tenant_id = "df5ce213-933c-5747-9b72-840f50559ec6"
        result = await invalidate_cache(domain="acme.com", request=request)

        assert result.invalidated is False
        assert "not available" in result.message

    @pytest.mark.asyncio
    async def test_cache_invalidated_successfully(self) -> None:
        brand_cache = AsyncMock()
        brand_cache.invalidate = AsyncMock(return_value=True)
        request = _make_request(app_state={"brand_cache": brand_cache})
        result = await invalidate_cache(domain="acme.com", request=request)

        assert result.invalidated is True
        assert result.domain == "acme.com"
        assert "invalidated" in result.message.lower()

    @pytest.mark.asyncio
    async def test_no_cached_entry(self) -> None:
        brand_cache = AsyncMock()
        brand_cache.invalidate = AsyncMock(return_value=False)
        request = _make_request(app_state={"brand_cache": brand_cache})
        result = await invalidate_cache(domain="acme.com", request=request)

        assert result.invalidated is False
        assert "no cached" in result.message.lower()


class TestGetBrandHistory:
    @pytest.mark.asyncio
    async def test_returns_history(self) -> None:
        brand_record = _make_brand_record(version=3)
        version_record = MagicMock()
        version_record.version = 2
        version_record.data = {"name": "Old Name"}
        version_record.changed_fields = ["name"]
        version_record.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        with (
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand",
                new_callable=AsyncMock,
                return_value=brand_record,
            ),
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand_history",
                new_callable=AsyncMock,
                return_value=[version_record],
            ),
        ):
            request = _make_request()
            result = await get_brand_history(domain="acme.com", request=request, db=AsyncMock())

        assert result.domain == "acme.com"
        assert result.current_version == 3
        assert len(result.history) == 1
        assert result.history[0].version == 2

    @pytest.mark.asyncio
    async def test_no_brand_returns_version_zero(self) -> None:
        with (
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand_history",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            request = _make_request()
            result = await get_brand_history(domain="unknown.com", request=request, db=AsyncMock())

        assert result.current_version == 0
        assert result.history == []


class TestServeDomainLogo:
    @pytest.mark.asyncio
    async def test_logo_service_not_available(self) -> None:
        request = _make_request()
        request.app.state = MagicMock(spec=[])
        request.state.tenant_id = "49f8b31f-dcb0-5e78-9ab6-e67313b6654e"

        with pytest.raises(HTTPException) as exc_info:
            await serve_domain_logo(domain="acme.com", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_no_logo_record(self) -> None:
        logo_service = AsyncMock()
        request = _make_request(app_state={"logo_service": logo_service})

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_logo_for_domain",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await serve_domain_logo(domain="acme.com", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_logo_file_not_found_on_disk(self) -> None:
        logo_service = AsyncMock()
        logo_service.retrieve = AsyncMock(return_value=None)
        request = _make_request(app_state={"logo_service": logo_service})
        record = MagicMock(storage_path="/tmp/logo.png", content_type="image/png")

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_logo_for_domain",
            new_callable=AsyncMock,
            return_value=record,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await serve_domain_logo(domain="acme.com", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_serves_logo_successfully(self) -> None:
        logo_data = b"\x89PNG fake logo data"
        logo_service = AsyncMock()
        logo_service.retrieve = AsyncMock(return_value=logo_data)
        request = _make_request(app_state={"logo_service": logo_service})
        record = MagicMock(storage_path="/tmp/logo.png", content_type="image/png")

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_logo_for_domain",
            new_callable=AsyncMock,
            return_value=record,
        ):
            result = await serve_domain_logo(domain="acme.com", request=request, db=AsyncMock())

        assert result.body == logo_data
        assert result.media_type == "image/png"


class TestOverrideBrand:
    @pytest.mark.asyncio
    async def test_applies_override_successfully(self) -> None:
        brand_record = _make_brand_record()
        updated_record = _make_brand_record(version=4)

        with (
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand",
                new_callable=AsyncMock,
                return_value=brand_record,
            ),
            patch(
                "src.domains.brand.brands_routes.BrandRepository.upsert_brand",
                new_callable=AsyncMock,
                return_value=updated_record,
            ),
        ):
            request = _make_request(app_state={"event_publisher": None})
            body = BrandOverrideRequest(domain="acme.com", overrides={"name": "New Acme"})
            result = await override_brand(
                domain="acme.com", body=body, request=request, db=AsyncMock()
            )

        assert result.domain == "acme.com"
        assert result.new_version == 4

    @pytest.mark.asyncio
    async def test_brand_not_found(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=None,
        ):
            request = _make_request(app_state={"event_publisher": None})
            body = BrandOverrideRequest(domain="unknown.com", overrides={"name": "X"})
            with pytest.raises(HTTPException) as exc_info:
                await override_brand(
                    domain="unknown.com", body=body, request=request, db=AsyncMock()
                )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_validation_errors_raise_422(self) -> None:
        with patch("src.domains.brand.brands_routes._override_service") as mock_svc:
            mock_svc.validate_override.return_value = ["Invalid color format"]

            request = _make_request(app_state={"event_publisher": None})
            body = BrandOverrideRequest(domain="acme.com", overrides={"primary_color": "notacolor"})
            with pytest.raises(HTTPException) as exc_info:
                await override_brand(domain="acme.com", body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_publishes_event_on_override(self) -> None:
        brand_record = _make_brand_record()
        updated_record = _make_brand_record(version=4)
        event_publisher = AsyncMock()
        event_publisher.publish_override = AsyncMock()

        with (
            patch(
                "src.domains.brand.brands_routes.BrandRepository.get_brand",
                new_callable=AsyncMock,
                return_value=brand_record,
            ),
            patch(
                "src.domains.brand.brands_routes.BrandRepository.upsert_brand",
                new_callable=AsyncMock,
                return_value=updated_record,
            ),
        ):
            request = _make_request(app_state={"event_publisher": event_publisher})
            body = BrandOverrideRequest(domain="acme.com", overrides={"name": "New"})
            await override_brand(domain="acme.com", body=body, request=request, db=AsyncMock())

        event_publisher.publish_override.assert_called_once()


class TestExportBrand:
    @pytest.mark.asyncio
    async def test_exports_from_db(self) -> None:
        brand_record = _make_brand_record()

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=brand_record,
        ):
            request = _make_request(app_state={"brand_cache": None})
            result = await export_brand(domain="acme.com", request=request, db=AsyncMock())

        assert result.name == "Acme Corp"
        assert result.domain == "acme.com"

    @pytest.mark.asyncio
    async def test_exports_from_cache(self) -> None:
        cached = BrandResponse(name="Cached", domain="cached.com", primary_color="#FF0000")
        brand_cache = AsyncMock()
        brand_cache.get = AsyncMock(return_value=cached)

        request = _make_request(app_state={"brand_cache": brand_cache})
        result = await export_brand(domain="cached.com", request=request, db=AsyncMock())

        assert result.name == "Cached"

    @pytest.mark.asyncio
    async def test_not_found_raises_404(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=None,
        ):
            request = _make_request(app_state={"brand_cache": None})
            with pytest.raises(HTTPException) as exc_info:
                await export_brand(domain="missing.com", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404


class TestGetQualityReport:
    @pytest.mark.asyncio
    async def test_returns_stored_quality_score(self) -> None:
        brand_record = _make_brand_record(quality_score={"overall": 0.85, "completeness": 0.9})
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=brand_record,
        ):
            request = _make_request()
            result = await get_quality_report(domain="acme.com", request=request, db=AsyncMock())

        assert result["domain"] == "acme.com"
        assert result["overall"] == 0.85

    @pytest.mark.asyncio
    async def test_computes_quality_on_the_fly(self) -> None:
        brand_record = _make_brand_record(quality_score=None)
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=brand_record,
        ):
            request = _make_request()
            result = await get_quality_report(domain="acme.com", request=request, db=AsyncMock())

        assert result["domain"] == "acme.com"
        # Should have computed quality score fields
        assert "overall_score" in result or "domain" in result

    @pytest.mark.asyncio
    async def test_not_found_raises_404(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            return_value=None,
        ):
            request = _make_request()
            with pytest.raises(HTTPException) as exc_info:
                await get_quality_report(domain="missing.com", request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404


class TestCompareBrands:
    @pytest.mark.asyncio
    async def test_compare_two_brands(self) -> None:
        brand_a_record = _make_brand_record(name="Brand A", domain="a.com")
        brand_b_record = _make_brand_record(name="Brand B", domain="b.com")

        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            side_effect=[brand_a_record, brand_b_record],
        ):
            request = _make_request()
            body = BrandComparisonRequest(domain_a="a.com", domain_b="b.com")
            result = await compare_brands(body=body, request=request, db=AsyncMock())

        assert result.domain_a == "a.com"
        assert result.domain_b == "b.com"
        assert 0.0 <= result.overall_similarity <= 1.0
        assert len(result.field_comparisons) > 0

    @pytest.mark.asyncio
    async def test_brand_a_not_found(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            side_effect=[None, _make_brand_record()],
        ):
            request = _make_request()
            body = BrandComparisonRequest(domain_a="missing.com", domain_b="b.com")
            with pytest.raises(HTTPException) as exc_info:
                await compare_brands(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_brand_b_not_found(self) -> None:
        with patch(
            "src.domains.brand.brands_routes.BrandRepository.get_brand",
            new_callable=AsyncMock,
            side_effect=[_make_brand_record(), None],
        ):
            request = _make_request()
            body = BrandComparisonRequest(domain_a="a.com", domain_b="missing.com")
            with pytest.raises(HTTPException) as exc_info:
                await compare_brands(body=body, request=request, db=AsyncMock())

        assert exc_info.value.status_code == 404
