"""Tests for analyzer service WM integration paths."""

from __future__ import annotations

import pytest

from src.domains.analyze.services.analyzer_service import AnalyzerService
from tests.llm_test_doubles import FakeWMClient


@pytest.fixture
def service():
    return AnalyzerService(wm_client=FakeWMClient())


class TestAnalyzeSiteWM:
    @pytest.mark.asyncio
    async def test_analyze_site_success(self) -> None:
        """analyze_site should parse a valid WM dict response."""
        wm = FakeWMClient(
            outputs=[
                {
                    "description": "A great site",
                    "target_audience": ["developers"],
                    "use_cases": ["testing"],
                    "selling_points": ["fast"],
                    "narrative_hooks": ["Try it"],
                    "tone": "casual",
                    "visual_identity": {"style": "modern"},
                }
            ]
        )
        service = AnalyzerService(wm_client=wm)
        result = await service.analyze_site(
            [
                {
                    "title": "Test",
                    "url": "https://example.com",
                    "page_type": "homepage",
                    "word_count": 100,
                    "interactive_elements": [],
                    "headings": ["Welcome"],
                }
            ],
            job_id="wm-test-1",
        )
        assert result.analysis["description"] == "A great site"
        assert result.analysis["tone"] == "casual"
        assert "developers" in result.analysis["target_audience"]

    @pytest.mark.asyncio
    async def test_analyze_site_with_user_context(self) -> None:
        """analyze_site should pass user context through the prompt builder."""
        wm = FakeWMClient(
            outputs=[
                {
                    "description": "SaaS app",
                    "target_audience": ["devs"],
                    "use_cases": [],
                    "selling_points": [],
                    "narrative_hooks": [],
                    "tone": "professional",
                    "visual_identity": {},
                }
            ]
        )
        service = AnalyzerService(wm_client=wm)
        result = await service.analyze_site(
            [{"title": "Test", "url": "https://example.com"}],
            user_context={"description": "A SaaS product", "site_type": "saas"},
            job_id="wm-test-2",
        )
        assert result.analysis["description"] == "SaaS app"


class TestAnalyzeSiteUnparseableOutput:
    """Free-text WM output with no JSON must NOT be treated as a successful
    empty analysis (see memory: unparseable-llm-output-is-not-empty-success).

    Pre-fix, ``_normalize_wm_output`` called ``parse_json_from_response``
    without ``required=True``, which returns ``{}`` on failure. Since ``{}``
    passes ``isinstance(parsed, dict)``, the empty result flowed straight
    into ``_coerce_site_result`` as a "successful" analysis: no exception,
    no retry, no ``_basic_site_analysis`` heuristic fallback, and the caller
    got an empty-but-valid-shaped result back.
    """

    @pytest.mark.asyncio
    async def test_unparseable_text_engages_retry_and_fallback(self) -> None:
        """A response with no extractable JSON must retry, exhaust retries,
        and fall back to the heuristic analysis — never return silently-empty
        fields as if the LLM call had succeeded.
        """
        wm = FakeWMClient(outputs=["Sorry, I cannot help with that request today."])
        service = AnalyzerService(wm_client=wm)
        pages = [
            {
                "title": "Widget Co Homepage",
                "url": "https://example.com",
                "page_type": "homepage",
                "word_count": 100,
                "interactive_elements": [],
                "headings": ["Welcome to Widget Co"],
            }
        ]
        result = await service.analyze_site(pages, job_id="wm-unparseable-1")

        # Every WM attempt was exhausted (1 initial + max_retries), never
        # short-circuited as a false success.
        from src.config import get_settings

        max_retries = get_settings().wm.max_retries
        assert len(wm.calls) == 1 + max_retries

        # The heuristic fallback ran, producing a NON-empty description
        # derived from the page titles — not the silently-empty shape a
        # `{}`-coerced "success" would have produced.
        assert result.analysis["description"] != ""
        assert "Widget Co Homepage" in result.analysis["description"]

        # Every recorded llm_call artifact for this response is a failure,
        # never a false "success" against unparseable output.
        llm_call_artifacts = [a for a in result.artifacts if a["type"] == "llm_call"]
        assert llm_call_artifacts
        assert all(a["status"] == "failure" for a in llm_call_artifacts)

    @pytest.mark.asyncio
    async def test_valid_json_string_output_still_succeeds(self) -> None:
        """A free-text response that DOES contain valid JSON must still be
        parsed and treated as a real success (not a regression from the fix).
        """
        wm = FakeWMClient(
            outputs=[
                '```json\n{"description": "A real site", "target_audience": ["devs"], '
                '"use_cases": [], "selling_points": [], "narrative_hooks": [], '
                '"tone": "casual", "visual_identity": {}}\n```'
            ]
        )
        service = AnalyzerService(wm_client=wm)
        result = await service.analyze_site(
            [{"title": "Test", "url": "https://example.com"}],
            job_id="wm-valid-text-1",
        )
        assert len(wm.calls) == 1
        assert result.analysis["description"] == "A real site"
        assert result.artifacts[0]["status"] == "success"

    @pytest.mark.asyncio
    async def test_structured_dict_output_still_succeeds_unchanged(self) -> None:
        """Structured-output mode (WM returns a dict directly) is untouched
        by this fix — it never goes through ``parse_json_from_response`` at
        all.
        """
        wm = FakeWMClient(
            outputs=[
                {
                    "description": "Structured site",
                    "target_audience": [],
                    "use_cases": [],
                    "selling_points": [],
                    "narrative_hooks": [],
                    "tone": "",
                    "visual_identity": {},
                }
            ]
        )
        service = AnalyzerService(wm_client=wm)
        result = await service.analyze_site(
            [{"title": "Test", "url": "https://example.com"}],
            job_id="wm-dict-1",
        )
        assert len(wm.calls) == 1
        assert result.analysis["description"] == "Structured site"
        assert result.artifacts[0]["status"] == "success"


class TestCoerceSiteResult:
    def test_dict_input(self, service: AnalyzerService) -> None:
        result = service._coerce_site_result(
            {
                "description": "test",
                "target_audience": ["a"],
                "use_cases": [],
                "selling_points": [],
                "narrative_hooks": [],
                "tone": "pro",
                "visual_identity": {},
            }
        )
        assert result["description"] == "test"

    def test_dict_minimal(self, service: AnalyzerService) -> None:
        """Missing optional keys default to empty values."""
        result = service._coerce_site_result({"description": "embedded", "tone": "casual"})
        assert result["description"] == "embedded"
        assert result["tone"] == "casual"
        assert result["target_audience"] == []


class TestBuildSitePrompt:
    def test_without_context(self, service: AnalyzerService) -> None:
        prompt = service._build_site_prompt(
            [
                {
                    "title": "Page",
                    "url": "https://a.com",
                    "page_type": "homepage",
                    "word_count": 50,
                    "interactive_elements": [],
                    "headings": ["Hi"],
                }
            ]
        )
        assert "Page" in prompt
        assert "JSON response" in prompt

    def test_with_context(self, service: AnalyzerService) -> None:
        prompt = service._build_site_prompt(
            [
                {
                    "title": "Page",
                    "url": "https://a.com",
                    "page_type": "homepage",
                    "word_count": 50,
                    "interactive_elements": [],
                    "headings": [],
                }
            ],
            user_context={
                "description": "My SaaS",
                "site_type": "saas",
                "target_audience": "devs",
            },
        )
        assert "My SaaS" in prompt
