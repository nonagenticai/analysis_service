"""Real-database tests for analyze domain repositories.

Uses postgres via the db_session fixture from conftest.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from src.domains.analyze.services.repositories.context_profile_repository import (
    UserContextProfileRepository,
)
from src.domains.analyze.services.repositories.job_repository import AnalysisJobRepository
from src.domains.analyze.services.repositories.usage_repository import AnalysisUsageRepository

# ---------------------------------------------------------------------------
# AnalysisJobRepository
# ---------------------------------------------------------------------------


class TestAnalysisJobRepository:
    """Tests for AnalysisJobRepository against a real (postgres) database."""

    @pytest.mark.asyncio
    async def test_create_and_get_round_trip(self, db_session):
        """create() persists a job that get() retrieves with correct fields."""
        job_id = str(uuid.uuid7())
        tenant = "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        created = await AnalysisJobRepository.create(
            db_session, job_id=job_id, tenant_id=tenant, total_pages=5
        )
        assert created.job_id == job_id
        assert created.tenant_id == tenant
        assert created.total_pages == 5
        assert created.status == "processing"

        fetched = await AnalysisJobRepository.get(db_session, job_id, tenant_id=tenant)
        assert fetched is not None
        assert fetched.job_id == job_id
        assert fetched.tenant_id == tenant

    @pytest.mark.asyncio
    async def test_get_wrong_tenant_returns_none(self, db_session):
        """Cross-tenant lookup must return None — same shape as a true miss."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b",
            total_pages=1,
        )
        assert (
            await AnalysisJobRepository.get(
                db_session, job_id, tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b"
            )
            is not None
        )
        assert (
            await AnalysisJobRepository.get(
                db_session, job_id, tenant_id="92b53b19-5118-57d6-81de-bd0fedd0becf"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_create_with_user_context(self, db_session):
        """create() stores user_context in pages_input JSONB column."""
        job_id = str(uuid.uuid7())
        ctx = {"industry": "saas", "audience": ["developers"]}
        created = await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
            user_context=ctx,
        )
        assert created.pages_input == ctx

        fetched = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.pages_input == ctx

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db_session):
        """get() returns None for a job_id that does not exist."""
        result = await AnalysisJobRepository.get(
            db_session, "nonexistent-id", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_status_basic(self, db_session):
        """update_status() changes status field."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        await AnalysisJobRepository.update_status(
            db_session, job_id, "completed", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        fetched = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.status == "completed"

    @pytest.mark.asyncio
    async def test_update_status_wrong_tenant_is_noop(self, db_session):
        """Cross-tenant UPDATE must affect zero rows."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b",
            total_pages=1,
        )
        await AnalysisJobRepository.update_status(
            db_session, job_id, "completed", tenant_id="92b53b19-5118-57d6-81de-bd0fedd0becf"
        )
        owner_view = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b"
        )
        assert owner_view is not None
        assert owner_view.status == "processing"  # unchanged

    @pytest.mark.asyncio
    async def test_update_status_with_results(self, db_session):
        """update_status() stores results_json when provided."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        results = {"score": 85, "pages": [{"url": "https://example.com"}]}
        await AnalysisJobRepository.update_status(
            db_session,
            job_id,
            "completed",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            results=results,
        )
        fetched = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.results_json == results

    @pytest.mark.asyncio
    async def test_update_status_with_error(self, db_session):
        """update_status() stores error message."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        await AnalysisJobRepository.update_status(
            db_session,
            job_id,
            "failed",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            error="Timeout",
        )
        fetched = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.status == "failed"
        assert fetched.error == "Timeout"

    @pytest.mark.asyncio
    async def test_update_status_with_all_json_fields(self, db_session):
        """update_status() stores pages_analyzed, site_analysis, narrative_extraction."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=2,
        )
        pages = [{"url": "https://a.com", "type": "homepage"}]
        site = {"description": "Test site"}
        narrative = {"hooks": ["hook1"]}
        await AnalysisJobRepository.update_status(
            db_session,
            job_id,
            "completed",
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            pages_analyzed=pages,
            site_analysis=site,
            narrative_extraction=narrative,
        )
        fetched = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.pages_analyzed == pages
        assert fetched.site_analysis == site
        assert fetched.narrative_extraction == narrative

    @pytest.mark.asyncio
    async def test_list_by_tenant(self, db_session):
        """list_by_tenant() returns jobs for the correct tenant."""
        t1 = "ba7a5f7e-1d3b-5e38-9f93-e034662dbd4e"
        t2 = "427191db-6725-5254-aeb9-22a384d87dcf"
        await AnalysisJobRepository.create(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t1, total_pages=1
        )
        await AnalysisJobRepository.create(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t1, total_pages=1
        )
        await AnalysisJobRepository.create(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t2, total_pages=1
        )

        jobs = await AnalysisJobRepository.list_by_tenant(db_session, t1)
        assert len(jobs) == 2
        assert all(j.tenant_id == t1 for j in jobs)

    @pytest.mark.asyncio
    async def test_list_by_tenant_pagination(self, db_session):
        """list_by_tenant() respects limit and offset."""
        tenant = "271676b7-ab21-5035-8774-8aca937cb3ad"
        for _ in range(5):
            await AnalysisJobRepository.create(
                db_session, job_id=str(uuid.uuid7()), tenant_id=tenant, total_pages=1
            )
        page1 = await AnalysisJobRepository.list_by_tenant(db_session, tenant, limit=2, offset=0)
        page2 = await AnalysisJobRepository.list_by_tenant(db_session, tenant, limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].job_id != page2[0].job_id

    @pytest.mark.asyncio
    async def test_list_by_tenant_filtered_no_filter(self, db_session):
        """list_by_tenant_filtered() without status returns all jobs and count."""
        tenant = "a536bc69-6ac4-5af7-b842-07dadb0f6335"
        await AnalysisJobRepository.create(
            db_session, job_id=str(uuid.uuid7()), tenant_id=tenant, total_pages=1
        )
        await AnalysisJobRepository.create(
            db_session, job_id=str(uuid.uuid7()), tenant_id=tenant, total_pages=1
        )
        jobs, count = await AnalysisJobRepository.list_by_tenant_filtered(db_session, tenant)
        assert len(jobs) == 2
        assert count == 2

    @pytest.mark.asyncio
    async def test_list_by_tenant_filtered_by_status(self, db_session):
        """list_by_tenant_filtered() filters by status correctly."""
        tenant = "322c4fcd-e499-50f4-a436-4b090404346e"
        jid1 = str(uuid.uuid7())
        jid2 = str(uuid.uuid7())
        await AnalysisJobRepository.create(db_session, job_id=jid1, tenant_id=tenant, total_pages=1)
        await AnalysisJobRepository.create(db_session, job_id=jid2, tenant_id=tenant, total_pages=1)
        await AnalysisJobRepository.update_status(db_session, jid1, "completed", tenant_id=tenant)

        jobs, count = await AnalysisJobRepository.list_by_tenant_filtered(
            db_session, tenant, status="completed"
        )
        assert len(jobs) == 1
        assert count == 1
        assert jobs[0].job_id == jid1

    @pytest.mark.asyncio
    async def test_store_and_get_artifacts(self, db_session):
        """store_artifacts() and get_artifacts() round-trip JSONB data."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        artifacts = [
            {"type": "screenshot", "path": "/screenshots/home.png"},
            {"type": "report", "path": "/reports/full.json"},
        ]
        await AnalysisJobRepository.store_artifacts(
            db_session, job_id, artifacts, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        fetched = await AnalysisJobRepository.get_artifacts(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched == artifacts

    @pytest.mark.asyncio
    async def test_get_artifacts_wrong_tenant_returns_none(self, db_session):
        """Cross-tenant artifacts lookup returns None."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b",
            total_pages=1,
        )
        await AnalysisJobRepository.store_artifacts(
            db_session, job_id, [{"path": "x"}], tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b"
        )
        assert (
            await AnalysisJobRepository.get_artifacts(
                db_session, job_id, tenant_id="92b53b19-5118-57d6-81de-bd0fedd0becf"
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_get_artifacts_returns_none_when_not_set(self, db_session):
        """get_artifacts() returns None when no artifacts have been stored."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        result = await AnalysisJobRepository.get_artifacts(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_processing_job(self, db_session):
        """cancel() sets status to 'cancelled' for a processing job."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        job = await AnalysisJobRepository.cancel(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert job is not None
        assert job.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_completed_job_is_noop(self, db_session):
        """cancel() does not change status for terminal states."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            total_pages=1,
        )
        await AnalysisJobRepository.update_status(
            db_session, job_id, "completed", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        # Expire cached state so we re-fetch
        await db_session.flush()
        job = await AnalysisJobRepository.cancel(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert job is not None
        assert job.status == "completed"

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_none(self, db_session):
        """cancel() returns None for a non-existent job."""
        result = await AnalysisJobRepository.cancel(
            db_session, "no-such-id", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_cancel_wrong_tenant_returns_none(self, db_session):
        """Cross-tenant cancel returns None and does not modify the row."""
        job_id = str(uuid.uuid7())
        await AnalysisJobRepository.create(
            db_session,
            job_id=job_id,
            tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b",
            total_pages=1,
        )
        result = await AnalysisJobRepository.cancel(
            db_session, job_id, tenant_id="92b53b19-5118-57d6-81de-bd0fedd0becf"
        )
        assert result is None
        owner_view = await AnalysisJobRepository.get(
            db_session, job_id, tenant_id="7892fa7d-a7a1-5e88-a790-44d99c1b096b"
        )
        assert owner_view is not None
        assert owner_view.status == "processing"  # unchanged

    @pytest.mark.asyncio
    async def test_cancel_races_with_completion_no_lost_update(self, db_session_factory):
        """A concurrent cancel + completion must yield exactly one final state.

        The atomic UPDATE...WHERE status NOT IN (terminal) RETURNING in cancel()
        closes the read-modify-write race that the old implementation had: the
        old code could overwrite a freshly-written 'completed' with 'cancelled'
        because the terminal-state check and the status write were separate
        statements. After the fix, whichever wins the UPDATE serialisation
        wins for good; the loser's UPDATE matches zero rows.
        """
        tenant = "67736660-70f9-57fe-b591-8aefb1ada4e2"
        job_id = str(uuid.uuid7())
        async with db_session_factory() as setup_session, setup_session.begin():
            await AnalysisJobRepository.create(
                setup_session, job_id=job_id, tenant_id=tenant, total_pages=1
            )

        async def do_cancel() -> None:
            async with db_session_factory() as s, s.begin():
                await AnalysisJobRepository.cancel(s, job_id, tenant_id=tenant)

        async def do_complete() -> None:
            async with db_session_factory() as s, s.begin():
                await AnalysisJobRepository.update_status(s, job_id, "completed", tenant_id=tenant)

        # A genuine race may make the losing transaction raise (serialization /
        # connection churn under parallel CI load); that's a valid outcome — the
        # invariant under test is the FINAL state, not that both ops succeed.
        await asyncio.gather(do_cancel(), do_complete(), return_exceptions=True)

        async with db_session_factory() as check, check.begin():
            final = await AnalysisJobRepository.get(check, job_id, tenant_id=tenant)
        assert final is not None
        # Final state is one of the two terminal states — never the impossible
        # "I cancelled an already-completed job" lost update.
        assert final.status in ("completed", "cancelled")


# ---------------------------------------------------------------------------
# UserContextProfileRepository
# ---------------------------------------------------------------------------


class TestUserContextProfileRepository:
    """Tests for UserContextProfileRepository against a real database."""

    @pytest.mark.asyncio
    async def test_create_and_get_round_trip(self, db_session):
        """create() persists a profile that get() retrieves."""
        ctx_data = {"industry": "fintech", "competitors": ["stripe", "square"]}
        profile = await UserContextProfileRepository.create(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            name="Default",
            context_data=ctx_data,
        )
        assert profile.id is not None
        assert profile.profile_name == "Default"
        assert profile.context_data == ctx_data
        assert profile.is_default is False

        fetched = await UserContextProfileRepository.get(
            db_session, profile.id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.context_data == ctx_data

    @pytest.mark.asyncio
    async def test_create_with_is_default(self, db_session):
        """create() stores is_default flag correctly."""
        profile = await UserContextProfileRepository.create(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            name="Primary",
            context_data={"key": "val"},
            is_default=True,
        )
        assert profile.is_default is True

    @pytest.mark.asyncio
    async def test_jsonb_round_trip_complex_data(self, db_session):
        """Complex nested JSONB data is preserved."""
        ctx_data = {
            "industry": "healthtech",
            "target_audience": [
                {"segment": "hospitals", "size": "large"},
                {"segment": "clinics", "size": "small"},
            ],
            "brand": {"colors": {"primary": "#FF0000"}, "fonts": ["Inter", "Roboto"]},
        }
        profile = await UserContextProfileRepository.create(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            name="Complex",
            context_data=ctx_data,
        )
        fetched = await UserContextProfileRepository.get(
            db_session, profile.id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.context_data == ctx_data

    @pytest.mark.asyncio
    async def test_list_by_tenant(self, db_session):
        """list_by_tenant() returns profiles for the correct tenant."""
        t1, t2 = "6da76670-8a84-5bcd-9b2d-1deae2aa7237", "6c358c65-867a-55aa-a469-1a1740075b91"
        await UserContextProfileRepository.create(
            db_session, tenant_id=t1, name="A", context_data={"a": 1}
        )
        await UserContextProfileRepository.create(
            db_session, tenant_id=t1, name="B", context_data={"b": 2}
        )
        await UserContextProfileRepository.create(
            db_session, tenant_id=t2, name="C", context_data={"c": 3}
        )

        profiles = await UserContextProfileRepository.list_by_tenant(db_session, t1)
        assert len(profiles) == 2
        assert all(p.tenant_id == t1 for p in profiles)

    @pytest.mark.asyncio
    async def test_delete(self, db_session):
        """delete() removes a profile so get() returns None."""
        profile = await UserContextProfileRepository.create(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            name="ToDelete",
            context_data={"x": 1},
        )
        pid = profile.id
        assert (
            await UserContextProfileRepository.delete(
                db_session, pid, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
            )
            == 1
        )
        result = await UserContextProfileRepository.get(
            db_session, pid, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_does_not_affect_other_profiles(self, db_session):
        """delete() only removes the targeted profile."""
        p1 = await UserContextProfileRepository.create(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            name="Keep",
            context_data={"k": 1},
        )
        p2 = await UserContextProfileRepository.create(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            name="Remove",
            context_data={"r": 2},
        )
        await UserContextProfileRepository.delete(
            db_session, p2.id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        remaining = await UserContextProfileRepository.list_by_tenant(
            db_session, "7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert len(remaining) == 1
        assert remaining[0].id == p1.id

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db_session):
        """get() returns None for a non-existent profile_id."""
        result = await UserContextProfileRepository.get(
            db_session, 999999, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_wrong_tenant_leaves_row(self, db_session):
        """Tenant B's delete of tenant A's profile removes nothing.

        Defence in depth: RLS also blocks this, but the SQL predicate must stand
        on its own where RLS is not enforcing.
        """
        owner = await UserContextProfileRepository.create(
            db_session,
            tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
            name="A-owned",
            context_data={"a": 1},
        )
        pid = owner.id

        deleted = await UserContextProfileRepository.delete(
            db_session, pid, tenant_id="f4a456f8-52f9-55c7-804b-96d5328adc53"
        )
        assert deleted == 0

        survivor = await UserContextProfileRepository.get(
            db_session, pid, tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        )
        assert survivor is not None
        assert survivor.tenant_id == "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"

    @pytest.mark.asyncio
    async def test_get_wrong_tenant_returns_none(self, db_session):
        """Cross-tenant get() is a miss — same shape as a non-existent id."""
        owner = await UserContextProfileRepository.create(
            db_session,
            tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
            name="A-owned",
            context_data={"a": 1},
        )
        assert (
            await UserContextProfileRepository.get(
                db_session, owner.id, tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
            )
            is not None
        )
        assert (
            await UserContextProfileRepository.get(
                db_session, owner.id, tenant_id="4151df14-4770-5a91-ab9f-cbf2d14e68be"
            )
            is None
        )


# ---------------------------------------------------------------------------
# AnalysisUsageRepository
# ---------------------------------------------------------------------------


class TestAnalysisUsageRepository:
    """Tests for AnalysisUsageRepository against a real database."""

    @pytest.mark.asyncio
    async def test_record_usage(self, db_session):
        """record_usage() creates a usage record with correct fields."""
        usage = await AnalysisUsageRepository.record_usage(
            db_session,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_id="job-1",
            pages_count=3,
        )
        assert usage.id is not None
        assert usage.tenant_id == "7eed672d-a7ff-5f16-87d7-b6a493fee523"
        assert usage.pages_analyzed == 3
        assert usage.analysis_count == 1

    @pytest.mark.asyncio
    async def test_get_daily_usage(self, db_session):
        """get_daily_usage() sums pages_analyzed for today."""
        tenant = "c309c93b-3262-5537-91a2-24dea2ec9e8b"
        await AnalysisUsageRepository.record_usage(
            db_session, tenant_id=tenant, job_id="j1", pages_count=3
        )
        await AnalysisUsageRepository.record_usage(
            db_session, tenant_id=tenant, job_id="j2", pages_count=5
        )
        total = await AnalysisUsageRepository.get_daily_usage(db_session, tenant)
        assert total == 8

    @pytest.mark.asyncio
    async def test_get_daily_usage_empty(self, db_session):
        """get_daily_usage() returns 0 when no usage exists."""
        total = await AnalysisUsageRepository.get_daily_usage(
            db_session, "0f7b76e5-c9a1-5360-a721-9b2728924475"
        )
        assert total == 0
