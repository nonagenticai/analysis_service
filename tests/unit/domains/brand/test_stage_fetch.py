"""Unit tests for FetchStage (Playwright-based)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.fetch import FetchStage


def _make_mock_page(
    html: str = "<html><head><title>Test</title></head><body>Hello</body></html>",
    screenshot: bytes = b"fake_png_data",
    page_text: str = "Hello world",
    css_props: dict | None = None,
) -> AsyncMock:
    page = AsyncMock()
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value=html)
    page.screenshot = AsyncMock(return_value=screenshot)
    page.evaluate = AsyncMock(side_effect=[page_text, css_props or {}])
    page.close = AsyncMock()
    return page


def _make_mock_context(page: AsyncMock) -> AsyncMock:
    """A BrowserContext double.

    ``FetchStage`` creates an explicit context so the SSRF route guard has
    something to attach to -- ``browser.new_page()`` makes one implicitly and
    gives the caller no handle on it. ``context.route`` is therefore part of the
    contract this double has to satisfy.
    """
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.route = AsyncMock()
    context.close = AsyncMock()
    return context


def _make_mock_playwright(page: AsyncMock | None = None) -> MagicMock:
    if page is None:
        page = _make_mock_page()
    browser = AsyncMock()
    browser.new_context = AsyncMock(return_value=_make_mock_context(page))
    browser.close = AsyncMock()

    pw = MagicMock()
    pw.chromium.launch = AsyncMock(return_value=browser)

    ctx_manager = AsyncMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=pw)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    return ctx_manager


class TestFetchStage:
    def setup_method(self) -> None:
        self.stage = FetchStage()

    def test_name(self) -> None:
        assert self.stage.name == "fetch"

    def test_should_run_with_url(self) -> None:
        ctx = PipelineContext(url="https://example.com")
        assert self.stage.should_run(ctx) is True

    def test_should_not_run_without_url(self) -> None:
        ctx = PipelineContext()
        assert self.stage.should_run(ctx) is False

    @pytest.mark.asyncio
    async def test_renders_page_with_playwright(self) -> None:
        page = _make_mock_page()
        mock_pw = _make_mock_playwright(page)

        with patch("playwright.async_api.async_playwright", return_value=mock_pw):
            ctx = PipelineContext(url="https://example.com")
            brand = BrandResponse()
            result = StageResult(stage_name="fetch")

            await self.stage.run(ctx, brand, result)

        assert ctx.html == "<html><head><title>Test</title></head><body>Hello</body></html>"
        assert ctx.soup is not None
        assert ctx.screenshot_bytes == b"fake_png_data"
        assert ctx.page_text == "Hello world"
        page.goto.assert_called_once()
        page.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_extracts_css_custom_properties(self) -> None:
        css_props = {"--primary-color": "#ff0000", "--bg-color": "#ffffff"}
        page = _make_mock_page(css_props=css_props)
        mock_pw = _make_mock_playwright(page)

        with patch("playwright.async_api.async_playwright", return_value=mock_pw):
            ctx = PipelineContext(url="https://example.com")
            brand = BrandResponse()
            result = StageResult(stage_name="fetch")

            await self.stage.run(ctx, brand, result)

        assert ctx.computed_styles == css_props

    @pytest.mark.asyncio
    async def test_failure_via_execute(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=TimeoutError("page load timeout"))
        page.close = AsyncMock()

        mock_pw = _make_mock_playwright(page)

        with patch("playwright.async_api.async_playwright", return_value=mock_pw):
            ctx = PipelineContext(url="https://example.com")
            brand = BrandResponse()

            result = await self.stage.execute(ctx, brand)

        assert result.success is False
        assert len(result.errors) == 1
        assert "timeout" in result.errors[0].message.lower()

    @pytest.mark.asyncio
    async def test_browser_closes_on_error(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(side_effect=RuntimeError("crash"))
        page.close = AsyncMock()

        context = _make_mock_context(page)
        browser = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        browser.close = AsyncMock()

        pw = MagicMock()
        pw.chromium.launch = AsyncMock(return_value=browser)

        ctx_manager = AsyncMock()
        ctx_manager.__aenter__ = AsyncMock(return_value=pw)
        ctx_manager.__aexit__ = AsyncMock(return_value=False)

        with patch("playwright.async_api.async_playwright", return_value=ctx_manager):
            ctx = PipelineContext(url="https://example.com")
            brand = BrandResponse()

            await self.stage.execute(ctx, brand)

        page.close.assert_called_once()
        context.close.assert_called_once()
        browser.close.assert_called_once()
