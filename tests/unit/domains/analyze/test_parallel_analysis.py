"""Unit tests for parallel page analysis in AnalyzerService."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from src.domains.analyze.services.analyzer_service import AnalyzerService


def _make_page(page_id: str, url: str = "", title: str = "") -> dict:
    """Helper to create a minimal page dict."""
    return {
        "page_id": page_id,
        "url": url or f"https://example.com/{page_id}",
        "title": title or f"Page {page_id}",
        "html": f"<html><head><title>{title or page_id}</title></head><body></body></html>",
        "text": f"Content for {page_id}",
        "word_count": 10,
    }


class TestParallelAnalysis:
    def setup_method(self) -> None:
        self.service = AnalyzerService()

    async def test_single_page_returns_correct_result(self) -> None:
        """Test single page analysis returns correct result."""
        pages = [_make_page("p1", url="https://example.com/", title="Home")]
        results = await self.service.analyze_pages(pages, workers=2)
        assert len(results) == 1
        assert results[0]["page_id"] == "p1"
        assert results[0]["url"] == "https://example.com/"

    async def test_multiple_pages_preserve_order(self) -> None:
        """Test that multiple pages preserve input order in results."""
        pages = [_make_page(f"p{i}") for i in range(5)]
        results = await self.service.analyze_pages(pages, workers=2)
        assert len(results) == 5
        for i, result in enumerate(results):
            assert result["page_id"] == f"p{i}"

    async def test_workers_parameter_respected(self) -> None:
        """Test that the workers parameter sets the semaphore value."""
        pages = [_make_page("p1")]
        semaphore_values: list[int] = []
        original_semaphore = asyncio.Semaphore

        def _capture_semaphore(value: int = 1) -> asyncio.Semaphore:
            semaphore_values.append(value)
            return original_semaphore(value)

        with patch("src.domains.analyze.services.analyzer_service.asyncio") as mock_asyncio:
            mock_asyncio.Semaphore = _capture_semaphore
            mock_asyncio.to_thread = asyncio.to_thread
            mock_asyncio.wait_for = asyncio.wait_for
            mock_asyncio.gather = asyncio.gather
            await self.service.analyze_pages(pages, workers=7)

        assert semaphore_values == [7]

    async def test_default_workers_from_config(self) -> None:
        """Test that workers defaults to config value when not specified."""
        pages = [_make_page("p1")]
        semaphore_values: list[int] = []
        original_semaphore = asyncio.Semaphore

        def _capture_semaphore(value: int = 1) -> asyncio.Semaphore:
            semaphore_values.append(value)
            return original_semaphore(value)

        with (
            patch("src.domains.analyze.services.analyzer_service.get_settings") as mock_settings,
            patch("src.domains.analyze.services.analyzer_service.asyncio") as mock_asyncio,
        ):
            mock_llm = MagicMock()
            mock_llm.parallel_workers = 6
            mock_llm.page_analysis_timeout = 30
            mock_llm.max_aggregate_timeout = 300
            mock_settings.return_value.analyze = mock_llm
            mock_asyncio.Semaphore = _capture_semaphore
            mock_asyncio.to_thread = asyncio.to_thread
            mock_asyncio.wait_for = asyncio.wait_for
            mock_asyncio.gather = asyncio.gather
            await self.service.analyze_pages(pages, workers=0)

        assert semaphore_values == [6]

    async def test_error_in_one_page_does_not_cancel_siblings(self) -> None:
        """A per-page exception must yield a placeholder entry, never cancel siblings.

        Regression for the narrow-except-batch-gather-trap: pre-fix, ``_analyze_one``
        caught only ``TimeoutError``, so any other exception (here ValueError) escaped
        the per-page absorb, propagated through bare ``asyncio.gather(*tasks)``, and
        cancelled every other page's analysis — one bad page took out the batch.
        Post-fix, ``_analyze_one``'s ``except Exception`` returns a placeholder entry
        so siblings continue.
        """
        pages = [_make_page("p1"), _make_page("p2"), _make_page("p3")]

        original_analyze = self.service.page_analyzer.analyze

        def _failing_analyze(
            page_id: str = "",
            url: str = "",
            title: str = "",
            html: str = "",
            text: str = "",
            word_count: int | None = None,
        ) -> dict:
            if page_id == "p2":
                raise ValueError("Analysis failed for p2")
            return original_analyze(
                page_id=page_id,
                url=url,
                title=title,
                html=html,
                text=text,
                word_count=word_count,
            )

        with patch.object(self.service.page_analyzer, "analyze", side_effect=_failing_analyze):
            results = await self.service.analyze_pages(pages, workers=2)

        assert len(results) == 3
        # Siblings completed normally with real analysis output.
        assert results[0]["page_id"] == "p1"
        assert "error" not in results[0]
        assert results[2]["page_id"] == "p3"
        assert "error" not in results[2]
        # The failing page is a placeholder entry, not a missing slot.
        assert results[1]["page_id"] == "p2"
        assert "ValueError" in results[1]["error"]

    async def test_off_tuple_exception_in_one_page_does_not_cancel_siblings(self) -> None:
        """KeyError must not cancel siblings — proves the absorb is genuinely wide.

        Pre-fix ``_analyze_one`` caught only ``TimeoutError``; everything else
        (KeyError, AttributeError, custom domain errors) escaped into the bare
        ``asyncio.gather`` and cancelled every other page.
        """
        pages = [_make_page("p1"), _make_page("p2"), _make_page("p3")]

        original_analyze = self.service.page_analyzer.analyze

        def _failing_analyze(
            page_id: str = "",
            url: str = "",
            title: str = "",
            html: str = "",
            text: str = "",
            word_count: int | None = None,
        ) -> dict:
            if page_id == "p2":
                raise KeyError("simulated off-tuple failure")
            return original_analyze(
                page_id=page_id,
                url=url,
                title=title,
                html=html,
                text=text,
                word_count=word_count,
            )

        with patch.object(self.service.page_analyzer, "analyze", side_effect=_failing_analyze):
            results = await self.service.analyze_pages(pages, workers=2)

        assert len(results) == 3
        assert "error" not in results[0]
        assert "KeyError" in results[1]["error"]
        assert "error" not in results[2]

    async def test_empty_pages_returns_empty_list(self) -> None:
        """Test that empty input returns empty list."""
        results = await self.service.analyze_pages([], workers=2)
        assert results == []

    async def test_is_actually_async(self) -> None:
        """Test that analyze_pages is a coroutine function."""
        assert asyncio.iscoroutinefunction(self.service.analyze_pages)

    async def test_concurrency_limit(self) -> None:
        """Test that the semaphore constrains parallel work to worker count."""
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        original_analyze = self.service.page_analyzer.analyze

        async def _tracking_analyze(
            page_id: str = "",
            url: str = "",
            title: str = "",
            html: str = "",
            text: str = "",
            word_count: int | None = None,
        ) -> dict:
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
            # Yield control so other tasks can start if allowed
            await asyncio.sleep(0.01)
            result = original_analyze(
                page_id=page_id,
                url=url,
                title=title,
                html=html,
                text=text,
                word_count=word_count,
            )
            async with lock:
                current_concurrent -= 1
            return result

        pages = [_make_page(f"p{i}") for i in range(8)]

        with patch(
            "src.domains.analyze.services.analyzer_service.asyncio.to_thread"
        ) as mock_to_thread:

            async def _side_effect(
                fn: object,
                page_id: str = "",
                url: str = "",
                title: str = "",
                html: str = "",
                text: str = "",
                word_count: int | None = None,
            ) -> dict:
                return await _tracking_analyze(
                    page_id=page_id,
                    url=url,
                    title=title,
                    html=html,
                    text=text,
                    word_count=word_count,
                )

            mock_to_thread.side_effect = _side_effect
            results = await self.service.analyze_pages(pages, workers=2)

        assert len(results) == 8
        assert max_concurrent <= 2

    async def test_config_override_workers(self) -> None:
        """Test that explicit workers overrides config default."""
        pages = [_make_page("p1")]
        semaphore_values: list[int] = []
        original_semaphore = asyncio.Semaphore

        def _capture_semaphore(value: int = 1) -> asyncio.Semaphore:
            semaphore_values.append(value)
            return original_semaphore(value)

        with patch("src.domains.analyze.services.analyzer_service.asyncio") as mock_asyncio:
            mock_asyncio.Semaphore = _capture_semaphore
            mock_asyncio.to_thread = asyncio.to_thread
            mock_asyncio.wait_for = asyncio.wait_for
            mock_asyncio.gather = asyncio.gather
            # Explicit workers=3 should override config default of 4
            await self.service.analyze_pages(pages, workers=3)

        assert semaphore_values == [3]
