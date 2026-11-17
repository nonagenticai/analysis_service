"""Unit tests for the analyzer service orchestrator."""

from __future__ import annotations

import pytest

from src.domains.analyze.services.analyzer_service import AnalyzerService
from tests.llm_test_doubles import FakeWMClient


class TestAnalyzerService:
    def setup_method(self) -> None:
        self.service = AnalyzerService(wm_client=FakeWMClient())

    async def test_analyze_pages_basic(self) -> None:
        pages = [
            {
                "page_id": "p1",
                "url": "https://example.com/",
                "title": "Test",
                "html": "<html><head><title>Test</title></head><body><h1>Hello</h1></body></html>",
            }
        ]
        results = await self.service.analyze_pages(pages)
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com/"
        assert results[0]["page_type"] == "homepage"

    async def test_analyze_pages_empty(self) -> None:
        results = await self.service.analyze_pages([])
        assert results == []

    async def test_analyze_pages_multiple(self) -> None:
        pages = [
            {
                "page_id": "p1",
                "url": "https://example.com/",
                "title": "Home",
                "html": "<html><body></body></html>",
            },
            {
                "page_id": "p2",
                "url": "https://example.com/about",
                "title": "About",
                "html": "<html><body></body></html>",
            },
        ]
        results = await self.service.analyze_pages(pages)
        assert len(results) == 2

    def test_extract_narrative(self) -> None:
        pages = [
            {
                "headings": ["A very compelling headline here"],
                "content_sections": [
                    {"section_type": "features", "heading": "Cool Feature"},
                ],
                "has_forms": True,
                "has_media": False,
            }
        ]
        result = self.service.extract_narrative(pages)
        assert "hooks" in result
        assert result["total_pages"] == 1
        assert result["pages_with_forms"] == 1

    def test_filter_language_duplicates_no_dupes(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/about"},
            {"page_id": "p2", "url": "https://example.com/pricing"},
        ]
        filtered, count = self.service.filter_language_duplicates(pages)
        assert len(filtered) == 2
        assert count == 0

    def test_filter_language_duplicates_with_dupes(self) -> None:
        pages = [
            {"page_id": "p1", "url": "https://example.com/en/about"},
            {"page_id": "p2", "url": "https://example.com/de/about"},
        ]
        filtered, count = self.service.filter_language_duplicates(pages, keep_lang="en")
        assert len(filtered) == 1
        assert count == 1
        assert filtered[0]["page_id"] == "p1"

    def test_annotate_spa_sections_no_spa(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
            {"page_id": "p2", "page_type": "about", "title": "About Us"},
        ]
        result = self.service.annotate_spa_sections(pages)
        assert all("is_spa_section" not in p or not p["is_spa_section"] for p in result)

    def test_annotate_spa_sections_detects_spa(self) -> None:
        pages = [
            {"page_id": "p1", "page_type": "homepage", "title": "My App"},
            {"page_id": "p2", "page_type": "about", "title": "My App"},
        ]
        result = self.service.annotate_spa_sections(pages)
        assert result[1].get("is_spa_section") is True

    def test_coerce_site_result_dict_input(self) -> None:
        parsed = {
            "description": "test site",
            "target_audience": ["devs"],
            "tone": "casual",
        }
        result = self.service._coerce_site_result(parsed)
        assert result["description"] == "test site"
        assert result["tone"] == "casual"

    def test_basic_site_analysis(self) -> None:
        pages = [{"title": "Example"}, {"title": "About"}]
        result = self.service._basic_site_analysis(pages)
        assert "description" in result
        assert "2 pages" in result["description"]
        assert "tone" in result


class TestAnalyzeSiteAsync:
    @pytest.mark.asyncio
    async def test_analyze_site_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        wm = FakeWMClient(raise_after="always", error=RuntimeError("WM down"))
        service = AnalyzerService(wm_client=wm)

        async def _no_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("asyncio.sleep", _no_sleep)
        result = await service.analyze_site([{"title": "Test", "url": "https://example.com"}])
        assert "description" in result.analysis
        assert "tone" in result.analysis
