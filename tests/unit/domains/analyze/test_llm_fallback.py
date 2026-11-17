"""Tests for WM retry behavior and enhanced fallback."""

import pytest

from src.domains.analyze.services.analyzer_service import AnalyzerService
from tests.llm_test_doubles import FakeWMClient


@pytest.fixture
def service():
    return AnalyzerService(wm_client=FakeWMClient())


@pytest.fixture
def sample_pages():
    return [
        {
            "page_id": "p1",
            "url": "https://example.com/",
            "title": "Example Homepage",
            "page_type": "homepage",
            "headings": ["Welcome to Example", "Our Amazing Features", "Get Started Today"],
            "word_count": 300,
            "has_forms": False,
            "has_media": True,
            "interactive_elements": [],
            "content_sections": [],
        },
        {
            "page_id": "p2",
            "url": "https://example.com/pricing",
            "title": "Pricing Plans",
            "page_type": "pricing",
            "headings": ["Choose Your Plan"],
            "word_count": 150,
            "has_forms": True,
            "has_media": False,
            "interactive_elements": [],
            "content_sections": [],
        },
    ]


class TestWMRetry:
    @pytest.mark.asyncio
    async def test_analyze_site_success_no_retry(self, sample_pages):
        """Successful WM call should not retry."""
        wm = FakeWMClient(outputs=[{"description": "A SaaS product", "target_audience": ["devs"]}])
        service = AnalyzerService(wm_client=wm)

        result = await service.analyze_site(sample_pages, job_id="test-job")

        assert result.analysis["description"] == "A SaaS product"
        assert len(wm.calls) == 1

    @pytest.mark.asyncio
    async def test_analyze_site_retry_on_failure(self, sample_pages, monkeypatch):
        """Should retry once on transient failure, then fall back."""
        wm = FakeWMClient(
            raise_after="always",
            error=RuntimeError("conn"),
        )
        service = AnalyzerService(wm_client=wm)

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        result = await service.analyze_site(sample_pages, job_id="retry-job")

        # Default max_retries=1 → 1 initial + 1 retry = 2 attempts
        assert len(wm.calls) == 2
        # Should fall back to basic analysis
        assert "description" in result.analysis

    @pytest.mark.asyncio
    async def test_analyze_site_retry_succeeds_second_time(self, sample_pages, monkeypatch):
        """Should succeed on retry if second attempt works."""
        payload = {"description": "Recovered result", "target_audience": ["users"]}
        wm = FakeWMClient(
            outputs=[payload, payload],
            raise_after=1,
            error=RuntimeError("first-call-fail"),
        )
        service = AnalyzerService(wm_client=wm)

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        result = await service.analyze_site(sample_pages, job_id="recover-job")

        assert result.analysis["description"] == "Recovered result"
        assert len(wm.calls) == 2


class TestEnhancedFallback:
    def test_fallback_infers_audience_from_pricing(self, service):
        """Fallback should infer business audience from pricing pages."""
        pages = [
            {"page_type": "pricing", "title": "Pricing", "word_count": 100, "headings": []},
            {"page_type": "homepage", "title": "Home", "word_count": 200, "headings": []},
        ]
        result = service._basic_site_analysis(pages)
        assert (
            "business" in result["target_audience"][0].lower()
            or "customer" in result["target_audience"][1].lower()
        )

    def test_fallback_infers_audience_from_docs(self, service):
        """Fallback should infer developer audience from docs pages."""
        pages = [
            {"page_type": "documentation", "title": "API Docs", "word_count": 800, "headings": []},
        ]
        result = service._basic_site_analysis(pages)
        assert any(
            "developer" in a.lower() or "technical" in a.lower() for a in result["target_audience"]
        )

    def test_fallback_infers_tone_from_word_count(self, service):
        """Fallback should infer tone from average word count."""
        verbose_pages = [
            {"page_type": "blog_post", "title": f"Post {i}", "word_count": 1000, "headings": []}
            for i in range(3)
        ]
        result = service._basic_site_analysis(verbose_pages)
        assert "technical" in result["tone"] or "detailed" in result["tone"]

    def test_fallback_extracts_selling_points(self, service):
        """Fallback should extract selling points from homepage headings."""
        pages = [
            {
                "page_type": "homepage",
                "title": "Acme Corp",
                "word_count": 200,
                "headings": ["Build Faster with Acme", "Enterprise Ready Platform", "Short"],
            }
        ]
        result = service._basic_site_analysis(pages)
        assert "Build Faster with Acme" in result["selling_points"]
        assert "Enterprise Ready Platform" in result["selling_points"]
        # "Short" should be excluded (< 10 chars)
        assert "Short" not in result["selling_points"]

    def test_fallback_generates_narrative_hooks(self, service):
        """Fallback should generate hooks from page titles."""
        pages = [
            {
                "page_type": "homepage",
                "title": "Welcome to the Future of AI",
                "word_count": 100,
                "headings": [],
            },
            {
                "page_type": "about",
                "title": "Our Story and Mission",
                "word_count": 100,
                "headings": [],
            },
            {"page_type": "pricing", "title": "Plans", "word_count": 50, "headings": []},
        ]
        result = service._basic_site_analysis(pages)
        assert "Welcome to the Future of AI" in result["narrative_hooks"]
        assert "Our Story and Mission" in result["narrative_hooks"]
        # "Plans" too short (< 10 chars)
        assert "Plans" not in result["narrative_hooks"]

    def test_fallback_empty_pages(self, service):
        """Fallback should handle empty page list gracefully."""
        result = service._basic_site_analysis([])
        assert "0 pages" in result["description"]
        assert result["target_audience"] == ["general"]

    def test_fallback_config_driven_wm(self, service, sample_pages, monkeypatch):
        """Service should pick up WM retry/timeout config from settings."""
        from src.config import clear_settings_cache, get_settings

        monkeypatch.setenv("WM_MAX_RETRIES", "1")
        monkeypatch.setenv("WM_RETRY_DELAY", "2.0")
        clear_settings_cache()
        try:
            settings = get_settings()
            assert settings.wm.max_retries == 1
            assert settings.wm.retry_delay == 2.0
        finally:
            clear_settings_cache()

    def test_fallback_concise_tone(self, service):
        """Short word counts should infer concise/marketing tone."""
        pages = [
            {"page_type": "landing", "title": "Get Started", "word_count": 50, "headings": []},
            {"page_type": "homepage", "title": "Home", "word_count": 30, "headings": []},
        ]
        result = service._basic_site_analysis(pages)
        assert "concise" in result["tone"] or "marketing" in result["tone"]
