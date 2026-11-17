"""Unit tests for response enrichment (Task 6)."""

from __future__ import annotations

import pytest

from src.domains.analyze.schemas import AnalysisResultResponse
from src.domains.analyze.services.analyzer_service import AnalyzerService, SiteAnalysisResult
from tests.llm_test_doubles import FakeWMClient

_SUCCESS_PAYLOAD = {
    "description": "Test site",
    "target_audience": [],
    "use_cases": [],
    "selling_points": [],
    "narrative_hooks": [],
    "tone": "neutral",
    "visual_identity": {},
}
_RETRY_PAYLOAD = {
    "description": "Retry site",
    "target_audience": [],
    "use_cases": [],
    "selling_points": [],
    "narrative_hooks": [],
    "tone": "casual",
    "visual_identity": {},
}


class TestSiteAnalysisResultDefaults:
    """Test SiteAnalysisResult dataclass defaults."""

    def test_defaults(self) -> None:
        result = SiteAnalysisResult()
        assert result.analysis == {}
        assert result.llm_calls == 0
        assert result.artifacts == []

    def test_with_values(self) -> None:
        analysis = {"description": "test", "tone": "formal"}
        artifacts = [{"type": "screenshot", "url": "https://example.com/shot.png"}]
        result = SiteAnalysisResult(analysis=analysis, llm_calls=3, artifacts=artifacts)
        assert result.analysis == analysis
        assert result.llm_calls == 3
        assert result.artifacts == artifacts


class TestAnalysisResultResponseDefaults:
    """Test AnalysisResultResponse enrichment field defaults."""

    def test_new_field_defaults(self) -> None:
        resp = AnalysisResultResponse(job_id="j1", status="completed")
        assert resp.processing_time_seconds is None
        assert resp.llm_calls == 0
        assert resp.schema_version == "1.0"
        assert resp.pages_analyzed_count == 0

    def test_with_enrichment_values(self) -> None:
        resp = AnalysisResultResponse(
            job_id="j1",
            status="completed",
            processing_time_seconds=1.234,
            llm_calls=2,
            schema_version="1.0",
            pages_analyzed_count=5,
        )
        assert resp.processing_time_seconds == 1.234
        assert resp.llm_calls == 2
        assert resp.schema_version == "1.0"
        assert resp.pages_analyzed_count == 5

    def test_schema_version_default_is_1_0(self) -> None:
        resp = AnalysisResultResponse(job_id="j1", status="processing")
        assert resp.schema_version == "1.0"

    def test_processing_time_is_float(self) -> None:
        resp = AnalysisResultResponse(
            job_id="j1", status="completed", processing_time_seconds=2.567
        )
        assert isinstance(resp.processing_time_seconds, float)


class TestLLMCallsTracking:
    """Test ``llm_calls`` tracking in ``analyze_site`` against a fake WMClient."""

    @pytest.mark.asyncio
    async def test_llm_calls_success_single_attempt(self) -> None:
        """Succeed on first try, verify llm_calls=1."""
        wm = FakeWMClient(outputs=[_SUCCESS_PAYLOAD])
        service = AnalyzerService(wm_client=wm)
        result = await service.analyze_site([{"title": "Test"}])
        assert isinstance(result, SiteAnalysisResult)
        assert result.llm_calls == 1
        assert result.analysis["description"] == "Test site"

    @pytest.mark.asyncio
    async def test_llm_calls_with_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail once then succeed; verify llm_calls=2."""
        wm = FakeWMClient(
            outputs=[_RETRY_PAYLOAD, _RETRY_PAYLOAD],
            raise_after=1,
            error=RuntimeError("transient"),
        )
        service = AnalyzerService(wm_client=wm)

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", _no_sleep)

        result = await service.analyze_site([{"title": "Test"}])
        assert isinstance(result, SiteAnalysisResult)
        assert result.llm_calls == 2
        assert result.analysis["description"] == "Retry site"
