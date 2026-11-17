"""Tests for AnalysisUsageRepository."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.services.repositories.usage_repository import AnalysisUsageRepository

# ``db_session`` comes from the root conftest (NullPool + per-xdist-worker schema
# isolation). Do NOT redefine a local engine here: a module-local engine skips the
# ``_isolate_worker_schema`` connect listener, so its ``TRUNCATE … CASCADE`` runs in
# the shared per-job search_path and races sibling workers' TRUNCATEs under ``-n 4``,
# taking an ACCESS EXCLUSIVE lock that never times out → the pre-commit hang.


async def test_record_usage(db_session: AsyncSession) -> None:
    """Recording usage should persist the entry with correct fields."""
    usage = await AnalysisUsageRepository.record_usage(
        session=db_session,
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        job_id="job-001",
        pages_count=5,
    )
    assert usage.id is not None
    assert usage.tenant_id == "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    assert usage.pages_analyzed == 5
    assert usage.analysis_count == 1


async def test_get_daily_usage(db_session: AsyncSession) -> None:
    """Daily usage should sum all pages_analyzed for the tenant today."""
    await AnalysisUsageRepository.record_usage(
        session=db_session,
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        job_id="job-001",
        pages_count=3,
    )
    await AnalysisUsageRepository.record_usage(
        session=db_session,
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        job_id="job-002",
        pages_count=7,
    )
    total = await AnalysisUsageRepository.get_daily_usage(
        db_session, "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    )
    assert total == 10


async def test_get_daily_usage_empty(db_session: AsyncSession) -> None:
    """Daily usage for a tenant with no records should return 0."""
    total = await AnalysisUsageRepository.get_daily_usage(
        db_session, "9797815e-3f7b-585c-a39b-cb5cb3f69e68"
    )
    assert total == 0


async def test_record_multiple_usage(db_session: AsyncSession) -> None:
    """Multiple usage entries for the same tenant should all persist."""
    entries = []
    for i in range(4):
        entry = await AnalysisUsageRepository.record_usage(
            session=db_session,
            tenant_id="805487ed-d0ba-53fe-b6a2-ecff11eb5d4c",
            job_id=f"job-multi-{i}",
            pages_count=(i + 1) * 2,
        )
        entries.append(entry)
    assert len(entries) == 4
    assert all(e.tenant_id == "805487ed-d0ba-53fe-b6a2-ecff11eb5d4c" for e in entries)
    # 2 + 4 + 6 + 8 = 20
    total = await AnalysisUsageRepository.get_daily_usage(
        db_session, "805487ed-d0ba-53fe-b6a2-ecff11eb5d4c"
    )
    assert total == 20


async def test_get_daily_usage_different_tenants(db_session: AsyncSession) -> None:
    """Daily usage should be isolated per tenant."""
    await AnalysisUsageRepository.record_usage(
        session=db_session,
        tenant_id="a71efcab-f4a8-5582-912e-5f4dc29b3c0d",
        job_id="job-x-1",
        pages_count=10,
    )
    await AnalysisUsageRepository.record_usage(
        session=db_session,
        tenant_id="e7fc047d-232b-5855-b3b7-fd2ee0643341",
        job_id="job-y-1",
        pages_count=25,
    )
    usage_x = await AnalysisUsageRepository.get_daily_usage(
        db_session, "a71efcab-f4a8-5582-912e-5f4dc29b3c0d"
    )
    usage_y = await AnalysisUsageRepository.get_daily_usage(
        db_session, "e7fc047d-232b-5855-b3b7-fd2ee0643341"
    )
    assert usage_x == 10
    assert usage_y == 25
