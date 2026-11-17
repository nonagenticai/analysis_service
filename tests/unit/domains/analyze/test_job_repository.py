"""Tests for AnalysisJobRepository."""

from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.services.repositories.job_repository import AnalysisJobRepository

# ``db_session`` comes from the root conftest (NullPool + per-xdist-worker schema
# isolation). A module-local engine would skip the ``_isolate_worker_schema`` connect
# listener and race sibling TRUNCATEs under ``-n 4`` — see test_usage_repository.py.


async def test_create_job(db_session: AsyncSession) -> None:
    """Creating a job should persist all fields correctly."""
    job = await AnalysisJobRepository.create(
        session=db_session,
        job_id="job-001",
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        total_pages=5,
        user_context={"industry": "tech"},
    )
    assert job.job_id == "job-001"
    assert job.tenant_id == "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    assert job.total_pages == 5
    assert job.status == "processing"
    assert job.pages_input == {"industry": "tech"}
    assert job.id is not None


async def test_get_job(db_session: AsyncSession) -> None:
    """Getting a job by job_id should return the correct record."""
    await AnalysisJobRepository.create(
        session=db_session,
        job_id="job-002",
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        total_pages=3,
    )
    fetched = await AnalysisJobRepository.get(
        db_session, "job-002", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    )
    assert fetched is not None
    assert fetched.job_id == "job-002"
    assert fetched.tenant_id == "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    assert fetched.total_pages == 3


async def test_get_nonexistent_job(db_session: AsyncSession) -> None:
    """Getting a non-existent job should return None."""
    result = await AnalysisJobRepository.get(
        db_session, "does-not-exist", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    )
    assert result is None


async def test_update_status_completed(db_session: AsyncSession) -> None:
    """Updating status to completed with results should persist both."""
    await AnalysisJobRepository.create(
        session=db_session,
        job_id="job-003",
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        total_pages=2,
    )
    results_data = {"summary": "analysis complete", "score": 95}
    await AnalysisJobRepository.update_status(
        session=db_session,
        job_id="job-003",
        status="completed",
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        results=results_data,
        pages_analyzed=[{"page_id": "p1", "type": "homepage"}],
        site_analysis={"tone": "professional"},
        narrative_extraction={"hooks": ["welcome"]},
    )
    job = await AnalysisJobRepository.get(
        db_session, "job-003", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    )
    assert job is not None
    assert job.status == "completed"
    assert job.results_json == results_data
    assert job.pages_analyzed == [{"page_id": "p1", "type": "homepage"}]
    assert job.site_analysis == {"tone": "professional"}
    assert job.narrative_extraction == {"hooks": ["welcome"]}
    assert job.error is None


async def test_update_status_failed(db_session: AsyncSession) -> None:
    """Updating status to failed with an error message should persist both."""
    await AnalysisJobRepository.create(
        session=db_session,
        job_id="job-004",
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        total_pages=1,
    )
    await AnalysisJobRepository.update_status(
        session=db_session,
        job_id="job-004",
        status="failed",
        tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
        error="LLM timeout after 30s",
    )
    job = await AnalysisJobRepository.get(
        db_session, "job-004", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    )
    assert job is not None
    assert job.status == "failed"
    assert job.error == "LLM timeout after 30s"
    assert job.results_json is None


async def test_list_by_tenant(db_session: AsyncSession) -> None:
    """Listing by tenant should return only that tenant's jobs."""
    for i in range(3):
        await AnalysisJobRepository.create(
            session=db_session,
            job_id=f"job-tenant-a-{i}",
            tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
            total_pages=i + 1,
        )
    await AnalysisJobRepository.create(
        session=db_session,
        job_id="job-tenant-b-0",
        tenant_id="4151df14-4770-5a91-ab9f-cbf2d14e68be",
        total_pages=1,
    )
    jobs_a = await AnalysisJobRepository.list_by_tenant(
        db_session, "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
    )
    jobs_b = await AnalysisJobRepository.list_by_tenant(
        db_session, "4151df14-4770-5a91-ab9f-cbf2d14e68be"
    )
    assert len(jobs_a) == 3
    assert len(jobs_b) == 1
    assert all(j.tenant_id == "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d" for j in jobs_a)
    assert jobs_b[0].tenant_id == "4151df14-4770-5a91-ab9f-cbf2d14e68be"


async def test_list_by_tenant_pagination(db_session: AsyncSession) -> None:
    """Limit and offset should control which jobs are returned."""
    for i in range(5):
        await AnalysisJobRepository.create(
            session=db_session,
            job_id=f"job-page-{i}",
            tenant_id="f490a023-c984-55de-813f-cccae19e8d3c",
            total_pages=1,
        )
    first_page = await AnalysisJobRepository.list_by_tenant(
        db_session, "f490a023-c984-55de-813f-cccae19e8d3c", limit=2, offset=0
    )
    second_page = await AnalysisJobRepository.list_by_tenant(
        db_session, "f490a023-c984-55de-813f-cccae19e8d3c", limit=2, offset=2
    )
    last_page = await AnalysisJobRepository.list_by_tenant(
        db_session, "f490a023-c984-55de-813f-cccae19e8d3c", limit=2, offset=4
    )
    assert len(first_page) == 2
    assert len(second_page) == 2
    assert len(last_page) == 1
    # Ensure no overlap between pages
    all_ids = (
        {j.job_id for j in first_page}
        | {j.job_id for j in second_page}
        | {j.job_id for j in last_page}
    )
    assert len(all_ids) == 5


async def test_list_by_tenant_empty(db_session: AsyncSession) -> None:
    """Listing for an unknown tenant should return an empty list."""
    jobs = await AnalysisJobRepository.list_by_tenant(
        db_session, "51344e05-c0f8-5ddc-b3d2-7b18b124b9ff"
    )
    assert jobs == []
