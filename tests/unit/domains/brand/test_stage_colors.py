"""Unit tests for ColorStage."""

import pytest
from bs4 import BeautifulSoup

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.types import StageResult
from src.domains.brand.schemas import BrandResponse
from src.domains.brand.stages.colors import ColorStage, _extract_colors_from_html, _extract_css_urls


def _make_ctx(html: str) -> PipelineContext:
    ctx = PipelineContext(url="https://example.com")
    ctx.soup = BeautifulSoup(html, "html.parser")
    return ctx


class TestExtractColorsFromHtml:
    def test_theme_color(self) -> None:
        html = '<html><head><meta name="theme-color" content="#ff6600"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        primary, secondary = _extract_colors_from_html(soup)
        assert primary == "#ff6600"
        assert secondary  # darken_hex produces some value

    def test_tile_color(self) -> None:
        html = '<html><head><meta name="msapplication-TileColor" content="#0078d4"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        primary, _secondary = _extract_colors_from_html(soup)
        assert primary == "#0078d4"

    def test_css_variable(self) -> None:
        html = "<html><head><style>:root { --primary-color: #e74c3c; }</style></head></html>"
        soup = BeautifulSoup(html, "html.parser")
        primary, _secondary = _extract_colors_from_html(soup)
        assert primary == "#e74c3c"

    def test_no_colors(self) -> None:
        soup = BeautifulSoup("<html></html>", "html.parser")
        primary, secondary = _extract_colors_from_html(soup)
        assert primary == ""
        assert secondary == ""


class TestExtractCssUrls:
    def test_extracts_stylesheet_links(self) -> None:
        html = '<html><head><link rel="stylesheet" href="/style.css"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        urls = _extract_css_urls(soup, "https://example.com")
        assert len(urls) == 1
        assert urls[0] == "https://example.com/style.css"

    def test_skips_google_fonts(self) -> None:
        html = '<html><head><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto"></head></html>'
        soup = BeautifulSoup(html, "html.parser")
        urls = _extract_css_urls(soup, "https://example.com")
        assert len(urls) == 0

    def test_protocol_relative_url(self) -> None:
        html = (
            '<html><head><link rel="stylesheet" href="//cdn.example.com/style.css"></head></html>'
        )
        soup = BeautifulSoup(html, "html.parser")
        urls = _extract_css_urls(soup, "https://example.com")
        assert urls[0] == "https://cdn.example.com/style.css"


class TestColorStage:
    def setup_method(self) -> None:
        self.stage = ColorStage()

    def test_name(self) -> None:
        assert self.stage.name == "colors"

    @pytest.mark.asyncio
    async def test_extracts_theme_color(self) -> None:
        html = '<html><head><meta name="theme-color" content="#3498db"></head></html>'
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="colors")
        await self.stage.run(ctx, brand, result)
        assert brand.primary_color == "#3498db"

    @pytest.mark.asyncio
    async def test_tracks_background_color(self) -> None:
        html = "<html><head><style>body { background-color: #f5f5f5; }</style></head></html>"
        ctx = _make_ctx(html)
        brand = BrandResponse()
        result = StageResult(stage_name="colors")
        await self.stage.run(ctx, brand, result)
        assert ctx.background_color == "#f5f5f5"
