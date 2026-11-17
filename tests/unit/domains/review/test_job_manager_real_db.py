"""Real-database tests for review domain JobManager.

Uses postgres via the db_session fixture from conftest.
"""

from __future__ import annotations

import uuid

import pytest

from src.domains.review.services.job_manager import JobManager


class TestReviewJobManager:
    """Tests for review JobManager against a real (postgres) database."""

    # ------------------------------------------------------------------ #
    # create_job + get_job
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_create_and_get_round_trip(self, db_session):
        """create_job() persists a job retrieved by get_job()."""
        job_id = str(uuid.uuid7())
        job = await JobManager.create_job(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["seo", "security"],
            url="https://example.com",
            domain="example.com",
        )
        assert job.job_id == job_id
        assert job.status == "processing"
        assert job.review_types == ["seo", "security"]
        assert job.url == "https://example.com"
        assert job.domain == "example.com"

        fetched = await JobManager.get_job(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.job_id == job_id
        assert fetched.review_types == ["seo", "security"]

    @pytest.mark.asyncio
    async def test_create_with_user_context(self, db_session):
        """create_job() stores user_context JSONB."""
        ctx = {"industry": "fintech", "focus": ["mobile"]}
        job = await JobManager.create_job(
            db_session,
            job_id=str(uuid.uuid7()),
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["ux"],
            user_context=ctx,
        )
        fetched = await JobManager.get_job(
            db_session, job.job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.user_context == ctx

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, db_session):
        """get_job() returns None for a nonexistent job_id."""
        result = await JobManager.get_job(
            db_session, "no-such-job", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    # ------------------------------------------------------------------ #
    # Status transitions
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_mark_completed(self, db_session):
        """mark_completed() sets status and stores results JSONB."""
        job_id = str(uuid.uuid7())
        await JobManager.create_job(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["seo"],
        )
        results = {
            "seo": {"score": 85, "findings": [{"title": "Missing meta"}]},
            "overall_score": 85,
        }
        await JobManager.mark_completed(db_session, job_id, results)
        fetched = await JobManager.get_job(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.status == "completed"
        assert fetched.results == results

    @pytest.mark.asyncio
    async def test_mark_failed(self, db_session):
        """mark_failed() sets status and error message."""
        job_id = str(uuid.uuid7())
        await JobManager.create_job(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["a11y"],
        )
        await JobManager.mark_failed(db_session, job_id, "Connection timeout")
        fetched = await JobManager.get_job(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.status == "failed"
        assert fetched.error == "Connection timeout"

    # ------------------------------------------------------------------ #
    # update_results_partial
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_update_results_partial(self, db_session):
        """update_results_partial() stores incremental results."""
        job_id = str(uuid.uuid7())
        await JobManager.create_job(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["seo", "ux"],
        )
        partial = {"seo": {"score": 70}}
        await JobManager.update_results_partial(db_session, job_id, partial)
        fetched = await JobManager.get_job(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.results == partial

    # ------------------------------------------------------------------ #
    # list_jobs
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_list_jobs_by_tenant(self, db_session):
        """list_jobs() filters by tenant and returns count."""
        t1, t2 = "b2df3ba6-f94d-5250-a394-636908081a2c", "2a58560e-191e-5693-8c00-f81e69911d76"
        await JobManager.create_job(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t1, review_types=["seo"]
        )
        await JobManager.create_job(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t1, review_types=["ux"]
        )
        await JobManager.create_job(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t2, review_types=["a11y"]
        )

        jobs, total = await JobManager.list_jobs(db_session, tenant_id=t1)
        assert total == 2
        assert len(jobs) == 2
        assert all(j.tenant_id == t1 for j in jobs)

    @pytest.mark.asyncio
    async def test_list_jobs_pagination(self, db_session):
        """list_jobs() respects limit and offset."""
        tenant = "5394332c-5b6a-5cc0-9904-4846fce09a25"
        for _ in range(5):
            await JobManager.create_job(
                db_session,
                job_id=str(uuid.uuid7()),
                tenant_id=tenant,
                review_types=["seo"],
            )
        page1, total = await JobManager.list_jobs(db_session, tenant_id=tenant, limit=2, offset=0)
        page2, _ = await JobManager.list_jobs(db_session, tenant_id=tenant, limit=2, offset=2)
        assert total == 5
        assert len(page1) == 2
        assert len(page2) == 2
        assert page1[0].job_id != page2[0].job_id

    @pytest.mark.asyncio
    async def test_list_jobs_never_spans_tenants(self, db_session):
        """A tenant's page — and its total — cover only that tenant's jobs.

        This used to be ``test_list_jobs_no_tenant_filter``, asserting that
        omitting ``tenant_id`` returned ALL jobs. That was the defect, not the
        contract: the parameter is now required, so "list everything" is no
        longer expressible, and a tenant that owns nothing sees nothing.
        """
        t1 = "eff5ef5c-0d52-5e16-8882-266ec4353176"
        t2 = "872bf32b-1f5f-5b40-ac19-fc6243cbf904"
        await JobManager.create_job(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t1, review_types=["seo"]
        )
        await JobManager.create_job(
            db_session, job_id=str(uuid.uuid7()), tenant_id=t2, review_types=["ux"]
        )

        jobs, total = await JobManager.list_jobs(db_session, tenant_id=t1)
        assert total == 1, "the COUNT leaked the other tenant's jobs"
        assert [j.tenant_id for j in jobs] == [t1]

        # A tenant with no rows gets an empty page, not the other two tenants'.
        empty, empty_total = await JobManager.list_jobs(
            db_session, tenant_id="cd808d14-f16d-5cc8-8c43-ec5bbb7af126"
        )
        assert (empty, empty_total) == ([], 0)

    # ------------------------------------------------------------------ #
    # get_domain_history
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_get_domain_history(self, db_session):
        """get_domain_history() returns jobs for the specified domain."""
        domain = "history-test.com"
        await JobManager.create_job(
            db_session,
            job_id=str(uuid.uuid7()),
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["seo"],
            domain=domain,
        )
        await JobManager.create_job(
            db_session,
            job_id=str(uuid.uuid7()),
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["ux"],
            domain=domain,
        )
        await JobManager.create_job(
            db_session,
            job_id=str(uuid.uuid7()),
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["perf"],
            domain="other.com",
        )

        history = await JobManager.get_domain_history(
            db_session, domain=domain, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert len(history) == 2
        assert all(j.domain == domain for j in history)

    @pytest.mark.asyncio
    async def test_get_domain_history_with_tenant_filter(self, db_session):
        """get_domain_history() respects tenant_id filter."""
        domain = "shared-domain.com"
        await JobManager.create_job(
            db_session,
            job_id=str(uuid.uuid7()),
            tenant_id="8cacab9c-483a-59cf-b0e6-dbb314495f8e",
            review_types=["seo"],
            domain=domain,
        )
        await JobManager.create_job(
            db_session,
            job_id=str(uuid.uuid7()),
            tenant_id="fb588f7d-a178-5486-a6ad-ff85589d5937",
            review_types=["seo"],
            domain=domain,
        )
        history = await JobManager.get_domain_history(
            db_session, domain=domain, tenant_id="8cacab9c-483a-59cf-b0e6-dbb314495f8e"
        )
        assert len(history) == 1
        assert history[0].tenant_id == "8cacab9c-483a-59cf-b0e6-dbb314495f8e"

    @pytest.mark.asyncio
    async def test_get_domain_history_limit(self, db_session):
        """get_domain_history() respects limit parameter."""
        domain = "limit-test.com"
        for _ in range(5):
            await JobManager.create_job(
                db_session,
                job_id=str(uuid.uuid7()),
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
                review_types=["seo"],
                domain=domain,
            )
        history = await JobManager.get_domain_history(
            db_session, domain=domain, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", limit=3
        )
        assert len(history) == 3

    # ------------------------------------------------------------------ #
    # Batch management
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_create_and_get_batch(self, db_session):
        """create_batch() persists a batch retrieved by get_batch()."""
        batch_id = str(uuid.uuid7())
        job_ids = [str(uuid.uuid7()) for _ in range(3)]
        batch = await JobManager.create_batch(
            db_session,
            batch_id=batch_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_ids=job_ids,
            total=3,
            callback_url="https://hook.example.com/done",
        )
        assert batch.batch_id == batch_id
        assert batch.status == "processing"
        assert batch.total == 3
        assert batch.job_ids == job_ids

        fetched = await JobManager.get_batch(
            db_session, batch_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.batch_id == batch_id
        assert fetched.callback_url == "https://hook.example.com/done"

    @pytest.mark.asyncio
    async def test_get_batch_nonexistent(self, db_session):
        """get_batch() returns None for nonexistent batch_id."""
        result = await JobManager.get_batch(
            db_session, "no-batch", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_batch_progress_partial(self, db_session, monkeypatch):
        """update_batch_progress() tracks completed/failed counts."""
        # Create jobs first
        j1 = str(uuid.uuid7())
        j2 = str(uuid.uuid7())
        j3 = str(uuid.uuid7())
        for jid in (j1, j2, j3):
            await JobManager.create_job(
                db_session,
                job_id=jid,
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
                review_types=["seo"],
            )

        # Create batch
        batch_id = str(uuid.uuid7())
        await JobManager.create_batch(
            db_session,
            batch_id=batch_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_ids=[j1, j2, j3],
            total=3,
        )

        # Complete one job
        await JobManager.mark_completed(db_session, j1, {"score": 80})
        await db_session.flush()
        await JobManager.update_batch_progress(db_session, batch_id)
        batch = await JobManager.get_batch(
            db_session, batch_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert batch is not None
        assert batch.completed == 1
        assert batch.status == "processing"

    @pytest.mark.asyncio
    async def test_update_batch_progress_all_done(self, db_session, monkeypatch):
        """update_batch_progress() marks batch completed when all jobs finish."""
        # Patch settings to disable webhook
        monkeypatch.setattr(
            "src.domains.review.services.job_manager.JobManager.get_batch",
            JobManager.get_batch,
        )

        j1 = str(uuid.uuid7())
        j2 = str(uuid.uuid7())
        for jid in (j1, j2):
            await JobManager.create_job(
                db_session,
                job_id=jid,
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
                review_types=["seo"],
            )

        batch_id = str(uuid.uuid7())
        await JobManager.create_batch(
            db_session,
            batch_id=batch_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_ids=[j1, j2],
            total=2,
        )

        await JobManager.mark_completed(db_session, j1, {"score": 90})
        await JobManager.mark_completed(db_session, j2, {"score": 85})
        await db_session.flush()
        await JobManager.update_batch_progress(db_session, batch_id)

        batch = await JobManager.get_batch(
            db_session, batch_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert batch is not None
        assert batch.completed == 2
        assert batch.status == "completed"
        assert batch.completed_at is not None

    @pytest.mark.asyncio
    async def test_update_batch_progress_partial_failures(self, db_session, monkeypatch):
        """update_batch_progress() marks batch as 'partial' when some jobs fail."""
        j1 = str(uuid.uuid7())
        j2 = str(uuid.uuid7())
        for jid in (j1, j2):
            await JobManager.create_job(
                db_session,
                job_id=jid,
                tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
                review_types=["seo"],
            )

        batch_id = str(uuid.uuid7())
        await JobManager.create_batch(
            db_session,
            batch_id=batch_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            job_ids=[j1, j2],
            total=2,
        )

        await JobManager.mark_completed(db_session, j1, {"score": 90})
        await JobManager.mark_failed(db_session, j2, "Timeout")
        await db_session.flush()
        await JobManager.update_batch_progress(db_session, batch_id)

        batch = await JobManager.get_batch(
            db_session, batch_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert batch is not None
        assert batch.status == "partial"
        assert batch.completed == 1
        assert batch.failed == 1

    @pytest.mark.asyncio
    async def test_update_batch_progress_nonexistent_batch(self, db_session):
        """update_batch_progress() is a no-op for nonexistent batch."""
        # Should not raise
        await JobManager.update_batch_progress(db_session, "no-batch")

    # ------------------------------------------------------------------ #
    # Results JSONB round-trip
    # ------------------------------------------------------------------ #

    @pytest.mark.asyncio
    async def test_results_jsonb_complex_round_trip(self, db_session):
        """Complex nested JSONB results are preserved through mark_completed."""
        job_id = str(uuid.uuid7())
        await JobManager.create_job(
            db_session,
            job_id=job_id,
            tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523",
            review_types=["seo", "security", "ux"],
        )
        results = {
            "seo": {
                "score": 82,
                "findings": [
                    {"id": "f1", "severity": "high", "title": "Missing meta"},
                    {"id": "f2", "severity": "low", "title": "No sitemap"},
                ],
                "details": {"crawled_pages": 5, "keywords": ["test", "example"]},
            },
            "security": {"score": 95, "findings": []},
            "overall_score": 88.5,
            "metadata": {"scan_duration": 12.3, "version": "2.0"},
        }
        await JobManager.mark_completed(db_session, job_id, results)
        fetched = await JobManager.get_job(
            db_session, job_id, tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523"
        )
        assert fetched is not None
        assert fetched.results == results
