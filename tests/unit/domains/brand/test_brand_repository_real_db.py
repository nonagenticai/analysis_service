"""Real-database tests for BrandRepository.

Uses postgres via the db_session fixture from conftest.
"""

from __future__ import annotations

import uuid

import pytest

from src.domains.brand.services.brand_repository import BrandRepository

# Every logo fixture in this module is seeded under "7eed672d-a7ff-5f16-87d7-b6a493fee523"; the repository's
# by-id lookups are tenant-scoped, so the reads must name that same tenant.
_LOGO_TENANT = "7eed672d-a7ff-5f16-87d7-b6a493fee523"


class TestBrandRepository:
    """Tests for BrandRepository against a real (postgres) database."""

    # ------------------------------------------------------------------ #
    # upsert_brand: create new
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_upsert_creates_new_brand(self, db_session):
        """upsert_brand() creates a brand when none exists for tenant+domain."""
        brand_data = {
            "name": "Acme Corp",
            "logo_url": "https://acme.com/logo.png",
            "has_logo": True,
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "tagline": "We make things",
            "confidence_score": 0.95,
        }
        brand = await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="acme.com",
            brand_data=brand_data,
            source="url",
        )
        assert brand.id is not None
        assert brand.name == "Acme Corp"
        assert brand.domain == "acme.com"
        assert brand.primary_color == "#FF0000"
        assert brand.version == 1
        assert brand.has_logo is True
        assert brand.tagline == "We make things"

    @pytest.mark.asyncio
    async def test_upsert_brand_data_jsonb_round_trip(self, db_session):
        """JSONB columns (fonts, color_palette, social_images) round-trip correctly."""
        brand_data = {
            "name": "JsonTest",
            "fonts": [{"family": "Inter", "weight": 400}],
            "color_palette": {
                "primary": "#111",
                "secondary": "#222",
                "accent": "#333",
            },
            "social_images": {"og": "https://example.com/og.png"},
            "industry": {"category": "tech", "subcategory": "saas"},
        }
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="json.test",
            brand_data=brand_data,
        )
        fetched = await BrandRepository.get_brand(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "json.test"
        )
        assert fetched is not None
        assert fetched.fonts == [{"family": "Inter", "weight": 400}]
        assert fetched.color_palette == {"primary": "#111", "secondary": "#222", "accent": "#333"}
        assert fetched.social_images == {"og": "https://example.com/og.png"}
        assert fetched.industry == {"category": "tech", "subcategory": "saas"}

    # ------------------------------------------------------------------ #
    # upsert_brand: update existing (creates BrandVersion)
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_brand(self, db_session):
        """upsert_brand() on an existing brand updates changed fields and bumps version."""
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="update.com",
            brand_data={"name": "Original", "primary_color": "#AAA"},
        )
        updated = await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="update.com",
            brand_data={"name": "Updated", "primary_color": "#BBB"},
            source="analysis",
        )
        assert updated.name == "Updated"
        assert updated.primary_color == "#BBB"
        assert updated.version == 2
        assert updated.source == "analysis"

    @pytest.mark.asyncio
    async def test_upsert_creates_version_snapshot(self, db_session):
        """upsert_brand() creates a BrandVersion entry on update."""
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="versioned.com",
            brand_data={"name": "V1Name", "primary_color": "#111"},
        )
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="versioned.com",
            brand_data={"name": "V2Name"},
        )
        history = await BrandRepository.get_brand_history(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "versioned.com"
        )
        assert len(history) == 1
        assert history[0].version == 1
        assert history[0].data["name"] == "V1Name"
        assert history[0].changed_fields is not None
        assert "name" in history[0].changed_fields

    @pytest.mark.asyncio
    async def test_upsert_no_change_no_version(self, db_session):
        """upsert_brand() with identical data does not create a BrandVersion."""
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="stable.com",
            brand_data={"name": "Stable"},
        )
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="stable.com",
            brand_data={"name": "Stable"},
        )
        history = await BrandRepository.get_brand_history(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "stable.com"
        )
        assert len(history) == 0
        brand = await BrandRepository.get_brand(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "stable.com"
        )
        assert brand is not None
        assert brand.version == 1

    # ------------------------------------------------------------------ #
    # get_brand
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_brand_returns_none_for_missing(self, db_session):
        """get_brand() returns None for nonexistent tenant+domain."""
        result = await BrandRepository.get_brand(
            db_session, "26f49354-6372-560b-a644-119bdc106ab1", "nope.com"
        )
        assert result is None

    # ------------------------------------------------------------------ #
    # list_brands
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_list_brands(self, db_session):
        """list_brands() returns brands for the correct tenant with count."""
        t1, t2 = "6f2b5abf-10cf-55b2-822b-4b93a2557663", "ec5493cf-dde1-580e-8438-46adb07171d7"
        await BrandRepository.upsert_brand(
            db_session, tenant_id=t1, domain="a.com", brand_data={"name": "A"}
        )
        await BrandRepository.upsert_brand(
            db_session, tenant_id=t1, domain="b.com", brand_data={"name": "B"}
        )
        await BrandRepository.upsert_brand(
            db_session, tenant_id=t2, domain="c.com", brand_data={"name": "C"}
        )

        brands, total = await BrandRepository.list_brands(db_session, t1)
        assert total == 2
        assert len(brands) == 2
        assert all(b.tenant_id == t1 for b in brands)

    @pytest.mark.asyncio
    async def test_list_brands_pagination(self, db_session):
        """list_brands() respects page and page_size."""
        tenant = "83235a92-6862-5c24-94df-1ce51a7c6b2a"
        for i in range(5):
            await BrandRepository.upsert_brand(
                db_session,
                tenant_id=tenant,
                domain=f"d{i}.com",
                brand_data={"name": f"Brand{i}"},
            )
        brands_p1, total = await BrandRepository.list_brands(
            db_session, tenant, page=1, page_size=2
        )
        brands_p2, _ = await BrandRepository.list_brands(db_session, tenant, page=2, page_size=2)
        assert total == 5
        assert len(brands_p1) == 2
        assert len(brands_p2) == 2
        assert brands_p1[0].domain != brands_p2[0].domain

    # ------------------------------------------------------------------ #
    # delete_brand
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_delete_brand(self, db_session):
        """delete_brand() removes the brand and returns True."""
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="delete-me.com",
            brand_data={"name": "Gone"},
        )
        result = await BrandRepository.delete_brand(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "delete-me.com"
        )
        assert result is True
        fetched = await BrandRepository.get_brand(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "delete-me.com"
        )
        assert fetched is None

    @pytest.mark.asyncio
    async def test_delete_brand_nonexistent_returns_false(self, db_session):
        """delete_brand() returns False for nonexistent brand."""
        result = await BrandRepository.delete_brand(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "never.com"
        )
        assert result is False

    # ------------------------------------------------------------------ #
    # get_brand_history
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_brand_history_empty(self, db_session):
        """get_brand_history() returns [] for a brand with no updates."""
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="no-history.com",
            brand_data={"name": "Fresh"},
        )
        history = await BrandRepository.get_brand_history(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "no-history.com"
        )
        assert history == []

    @pytest.mark.asyncio
    async def test_get_brand_history_multiple_versions(self, db_session):
        """get_brand_history() returns versions in descending order."""
        domain = "multi-ver.com"
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain=domain,
            brand_data={"name": "V1"},
        )
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain=domain,
            brand_data={"name": "V2"},
        )
        await BrandRepository.upsert_brand(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain=domain,
            brand_data={"name": "V3"},
        )
        history = await BrandRepository.get_brand_history(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", domain
        )
        assert len(history) == 2  # V1->V2 and V2->V3 snapshots
        assert history[0].version > history[1].version  # desc order

    # ------------------------------------------------------------------ #
    # Logo asset CRUD
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_save_and_get_logo_asset(self, db_session):
        """save_logo_asset() persists a record retrieved by get_logo_asset()."""
        asset_id = str(uuid.uuid7())
        record = await BrandRepository.save_logo_asset(
            db_session,
            asset_id=asset_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="logo.com",
            original_url="https://logo.com/logo.png",
            storage_path="/logos/logo.png",
            content_type="image/png",
            size_bytes=12345,
            width=200,
            height=100,
        )
        assert record.asset_id == asset_id
        assert record.size_bytes == 12345

        fetched = await BrandRepository.get_logo_asset(db_session, asset_id, tenant_id=_LOGO_TENANT)
        assert fetched is not None
        assert fetched.width == 200
        assert fetched.height == 100

    @pytest.mark.asyncio
    async def test_get_logo_for_domain(self, db_session):
        """get_logo_for_domain() returns a logo for the correct tenant+domain."""
        t1 = "fc206325-eca1-5b3b-bcda-6df8a156f929"
        id1 = str(uuid.uuid7())
        await BrandRepository.save_logo_asset(
            db_session,
            asset_id=id1,
            tenant_id=t1,
            domain="logos.com",
            original_url="https://logos.com/old.png",
            storage_path="/old.png",
            content_type="image/png",
            size_bytes=100,
        )
        id2 = str(uuid.uuid7())
        await BrandRepository.save_logo_asset(
            db_session,
            asset_id=id2,
            tenant_id=t1,
            domain="logos.com",
            original_url="https://logos.com/new.png",
            storage_path="/new.png",
            content_type="image/png",
            size_bytes=200,
        )
        logo = await BrandRepository.get_logo_for_domain(db_session, t1, "logos.com")
        assert logo is not None
        assert logo.asset_id in {id1, id2}
        assert logo.tenant_id == t1
        assert logo.domain == "logos.com"

    @pytest.mark.asyncio
    async def test_get_logo_for_domain_returns_none(self, db_session):
        """get_logo_for_domain() returns None when no logo exists."""
        result = await BrandRepository.get_logo_for_domain(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523", "no-logo.com"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_logo_asset(self, db_session):
        """delete_logo_asset() removes the record."""
        asset_id = str(uuid.uuid7())
        await BrandRepository.save_logo_asset(
            db_session,
            asset_id=asset_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            domain="del-logo.com",
            original_url="https://del.com/logo.png",
            storage_path="/del.png",
            content_type="image/png",
            size_bytes=50,
        )
        result = await BrandRepository.delete_logo_asset(
            db_session, asset_id, tenant_id=_LOGO_TENANT
        )
        assert result is True
        assert (
            await BrandRepository.get_logo_asset(db_session, asset_id, tenant_id=_LOGO_TENANT)
            is None
        )

    @pytest.mark.asyncio
    async def test_delete_logo_asset_nonexistent(self, db_session):
        """delete_logo_asset() returns False for nonexistent asset."""
        result = await BrandRepository.delete_logo_asset(
            db_session, "no-such-asset", tenant_id=_LOGO_TENANT
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_get_logo_asset_nonexistent(self, db_session):
        """get_logo_asset() returns None for nonexistent asset_id."""
        result = await BrandRepository.get_logo_asset(
            db_session, "missing-id", tenant_id=_LOGO_TENANT
        )
        assert result is None

    # ------------------------------------------------------------------ #
    # log_extraction
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_log_extraction(self, db_session):
        """log_extraction() creates an audit record with JSONB result."""
        result_data = {"brand_name": "Test", "confidence": 0.9}
        log = await BrandRepository.log_extraction(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url="https://example.com",
            source="url",
            result=result_data,
        )
        assert log.id is not None
        assert log.tenant_id == "7eed672d-a7ff-5f16-87d7-b6a493fee523"
        assert log.source == "url"
        assert log.result == result_data

    @pytest.mark.asyncio
    async def test_log_extraction_null_url(self, db_session):
        """log_extraction() accepts None for url (analysis-based extraction)."""
        log = await BrandRepository.log_extraction(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            url=None,
            source="analysis",
            result={"data": "test"},
        )
        assert log.url is None
        assert log.source == "analysis"
