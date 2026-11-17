"""Unit tests for analysis artifact storage."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.analyze.schemas import ArtifactsResponse, LLMArtifact
from src.domains.analyze.services.analyzer_service import AnalyzerService
from src.domains.analyze.services.repositories.job_repository import AnalysisJobRepository
from tests.llm_test_doubles import FakeWMClient


class TestLLMArtifactSchema:
    def test_defaults(self) -> None:
        """LLMArtifact should have sensible defaults for all fields."""
        artifact = LLMArtifact()
        assert artifact.type == "llm_call"
        assert artifact.model == ""
        assert artifact.prompt_tokens == 0
        assert artifact.response_tokens == 0
        assert artifact.duration_ms == 0.0
        assert artifact.status == ""
        assert artifact.prompt_preview == ""
        assert artifact.response_preview == ""
        assert artifact.error == ""

    def test_custom_values(self) -> None:
        """LLMArtifact should accept custom values."""
        artifact = LLMArtifact(
            model="claude-3",
            prompt_tokens=100,
            response_tokens=50,
            duration_ms=1234.5,
            status="success",
            prompt_preview="Hello",
            response_preview="World",
        )
        assert artifact.model == "claude-3"
        assert artifact.prompt_tokens == 100
        assert artifact.response_tokens == 50
        assert artifact.duration_ms == 1234.5


class TestArtifactsResponseSchema:
    def test_empty_artifacts(self) -> None:
        """ArtifactsResponse with no artifacts should have empty list."""
        resp = ArtifactsResponse(job_id="job-1")
        assert resp.job_id == "job-1"
        assert resp.artifacts == []

    def test_with_artifacts(self) -> None:
        """ArtifactsResponse should accept a list of LLMArtifact dicts."""
        resp = ArtifactsResponse(
            job_id="job-2",
            artifacts=[
                LLMArtifact(model="claude-3", status="success"),
                LLMArtifact(model="claude-3", status="failure", error="timeout"),
            ],
        )
        assert len(resp.artifacts) == 2
        assert resp.artifacts[0].status == "success"
        assert resp.artifacts[1].error == "timeout"


class TestArtifactCollectionSuccess:
    @pytest.mark.asyncio
    async def test_successful_llm_call_records_artifact(self) -> None:
        """A successful WM call should record a success artifact."""
        wm = FakeWMClient(
            outputs=[
                {
                    "description": "Test",
                    "target_audience": [],
                    "use_cases": [],
                    "selling_points": [],
                    "narrative_hooks": [],
                    "tone": "pro",
                    "visual_identity": {},
                }
            ],
            usage={
                "prompt_tokens": 150,
                "completion_tokens": 40,
                "total_tokens": 190,
            },
        )
        service = AnalyzerService(wm_client=wm)
        result = await service.analyze_site(
            [{"title": "Test", "url": "https://example.com"}],
            job_id="artifact-test-1",
        )

        assert len(result.artifacts) == 1
        art = result.artifacts[0]
        assert art["type"] == "llm_call"
        assert art["status"] == "success"
        assert art["prompt_tokens"] > 0
        assert art["response_tokens"] > 0
        assert art["duration_ms"] >= 0
        assert "model" in art
        assert "prompt_preview" in art
        assert "response_preview" in art
        # Verify actual token usage is recorded
        assert art["prompt_tokens"] == 150
        assert art["response_tokens"] == 40
        assert art["total_tokens_actual"] == 190
        # Heuristic estimates should still be present
        assert art["prompt_tokens_estimated"] > 0
        assert art["response_tokens_estimated"] > 0


class TestArtifactCollectionFailure:
    @pytest.mark.asyncio
    async def test_failed_llm_call_records_failure_artifact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed WM call should record a failure artifact with the error string."""
        wm = FakeWMClient(
            raise_after="always",
            error=RuntimeError("Connection error"),
        )
        service = AnalyzerService(wm_client=wm)

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        result = await service.analyze_site(
            [{"title": "Test", "url": "https://example.com"}],
            job_id="artifact-test-2",
        )

        assert len(result.artifacts) >= 1
        for art in result.artifacts:
            assert art["type"] == "llm_call"
            assert art["status"] == "failure"
            assert "Connection error" in art["error"]
            assert art["prompt_tokens"] > 0
            assert art["duration_ms"] >= 0


class TestArtifactRepository:
    # ``db_session`` comes from the root conftest (NullPool + per-xdist-worker schema
    # isolation). A class-local engine would skip the ``_isolate_worker_schema`` connect
    # listener and race sibling TRUNCATEs under ``-n 4`` — see test_usage_repository.py.

    async def test_store_artifacts(self, db_session: AsyncSession) -> None:
        """store_artifacts should persist artifacts to the database."""
        await AnalysisJobRepository.create(
            session=db_session,
            job_id="art-job-1",
            tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
            total_pages=1,
        )
        artifacts = [
            {
                "type": "llm_call",
                "model": "claude-3",
                "prompt_tokens": 100,
                "status": "success",
            }
        ]
        await AnalysisJobRepository.store_artifacts(
            db_session, "art-job-1", artifacts, tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        )
        job = await AnalysisJobRepository.get(
            db_session, "art-job-1", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        )
        assert job is not None
        assert job.analysis_artifacts == artifacts

    async def test_get_artifacts(self, db_session: AsyncSession) -> None:
        """get_artifacts should return stored artifacts."""
        await AnalysisJobRepository.create(
            session=db_session,
            job_id="art-job-2",
            tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
            total_pages=1,
        )
        artifacts = [
            {
                "type": "llm_call",
                "model": "claude-3",
                "status": "failure",
                "error": "timeout",
            }
        ]
        await AnalysisJobRepository.store_artifacts(
            db_session, "art-job-2", artifacts, tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        )
        result = await AnalysisJobRepository.get_artifacts(
            db_session, "art-job-2", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        )
        assert result is not None
        assert len(result) == 1
        assert result[0]["status"] == "failure"
        assert result[0]["error"] == "timeout"

    async def test_get_artifacts_nonexistent(self, db_session: AsyncSession) -> None:
        """get_artifacts for a non-existent job should return None."""
        result = await AnalysisJobRepository.get_artifacts(
            db_session, "no-such-job", tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
        )
        assert result is None
