"""Unit tests for schema validation."""

import pytest
from pydantic import ValidationError

from src.domains.analyze.schemas import (
    AnalyzeRequest,
    CrawledPageInput,
    PageAnalysisResponse,
    PageType,
)


class TestCrawledPageInput:
    def test_valid_minimal(self) -> None:
        page = CrawledPageInput(url="https://example.com")
        assert page.url == "https://example.com"
        assert page.page_id == ""
        assert page.html == ""

    def test_url_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CrawledPageInput(url="https://example.com/" + "a" * 2048)

    def test_html_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CrawledPageInput(url="https://example.com", html="x" * 5_000_001)

    def test_text_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CrawledPageInput(url="https://example.com", text="x" * 1_000_001)

    def test_page_id_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CrawledPageInput(url="https://example.com", page_id="x" * 101)

    def test_title_max_length(self) -> None:
        with pytest.raises(ValidationError):
            CrawledPageInput(url="https://example.com", title="x" * 501)


class TestAnalyzeRequest:
    def test_empty_pages_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AnalyzeRequest(pages=[])

    def test_too_many_pages_rejected(self) -> None:
        pages = [CrawledPageInput(url=f"https://example.com/{i}") for i in range(101)]
        with pytest.raises(ValidationError):
            AnalyzeRequest(pages=pages)

    def test_valid_request(self) -> None:
        req = AnalyzeRequest(pages=[CrawledPageInput(url="https://example.com")])
        assert len(req.pages) == 1

    def test_page_type_enum_values(self) -> None:
        assert PageType.HOMEPAGE == "homepage"
        assert PageType.UNKNOWN == "unknown"
        assert len(PageType) == 16

    def test_defaults(self) -> None:
        resp = PageAnalysisResponse(url="https://example.com")
        assert resp.page_type == PageType.UNKNOWN
        assert resp.interactive_elements == []
        assert resp.has_forms is False
