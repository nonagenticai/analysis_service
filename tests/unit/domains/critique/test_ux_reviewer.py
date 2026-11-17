"""Tests for ux_reviewer module (WMClient path)."""

import json

import pytest

from src.domains.critique.services.scanners.ux_reviewer import (
    DEFAULT_UX_PROMPT,
    MAX_HTML_CHARS,
    UXReviewer,
)
from tests.llm_test_doubles import FakeWMClient


class TestUXReviewerInit:
    """Test UXReviewer initialization."""

    def test_default_init_uses_injected_wm(self) -> None:
        wm = FakeWMClient()
        reviewer = UXReviewer(wm_client=wm)
        assert reviewer._wm is wm
        assert reviewer._default_model_override is None

    def test_init_with_model_override(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient(), model="custom-model")
        assert reviewer._default_model_override == "custom-model"


class TestUXReviewerScan:
    """Test UXReviewer.scan method."""

    @pytest.mark.asyncio
    async def test_scan_success_with_dimensions(self) -> None:
        wm = FakeWMClient(
            outputs=[
                {
                    "dimensions": {
                        "navigation": {
                            "score": 75,
                            "findings": [
                                {
                                    "severity": "medium",
                                    "title": "Nav issue",
                                    "description": "Hard to find",
                                    "recommendation": "Improve nav",
                                }
                            ],
                        }
                    }
                }
            ]
        )
        reviewer = UXReviewer(wm_client=wm)
        result = await reviewer.scan("<html>Test</html>", url="https://example.com")
        assert len(result) == 1
        assert "[Navigation]" in result[0]["description"]
        assert wm.calls[0].worker_name == "critique-ux-review"

    @pytest.mark.asyncio
    async def test_scan_wm_error_returns_finding(self) -> None:
        wm = FakeWMClient(raise_after="always", error=RuntimeError("API error"))
        reviewer = UXReviewer(wm_client=wm)

        result = await reviewer.scan("<html>Test</html>", url="https://example.com")
        assert len(result) == 1
        assert "error" in result[0]["title"].lower()

    @pytest.mark.asyncio
    async def test_scan_with_instructions_calls_wm(self) -> None:
        wm = FakeWMClient(outputs=[[]])
        reviewer = UXReviewer(wm_client=wm)
        await reviewer.scan(
            "<html>Test</html>",
            url="https://example.com",
            instructions="Focus on mobile",
        )
        assert wm.calls
        assert "Focus on mobile" in wm.calls[0].message


class TestParseResponse:
    """Test _parse_response method."""

    def test_dimensions_format(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        data = {
            "dimensions": {
                "navigation": {
                    "score": 80,
                    "findings": [
                        {
                            "severity": "medium",
                            "title": "Nav Issue",
                            "description": "Hard to find",
                            "recommendation": "Add breadcrumbs",
                        }
                    ],
                },
                "visual_hierarchy": {
                    "score": 90,
                    "findings": [],
                },
            }
        }
        result = reviewer._parse_response(json.dumps(data), "https://example.com")
        assert len(result) == 1
        assert "[Navigation]" in result[0]["description"]

    def test_array_format(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        data = [
            {
                "severity": "high",
                "title": "UX Problem",
                "description": "Bad UX",
                "recommendation": "Fix it",
            }
        ]
        result = reviewer._parse_response(json.dumps(data), "https://example.com")
        assert len(result) == 1
        assert result[0]["title"] == "UX Problem"

    def test_code_block_wrapper(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        data = [{"severity": "info", "title": "Note", "description": "Observation"}]
        text = "```json\n" + json.dumps(data) + "\n```"
        result = reviewer._parse_response(text, "https://example.com")
        assert len(result) == 1

    def test_unparseable_response(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        result = reviewer._parse_response("This is not JSON at all.", "https://example.com")
        assert len(result) == 1
        assert "parse error" in result[0]["title"].lower()

    def test_non_dict_items_in_array_skipped(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        data = [
            {"severity": "low", "title": "Valid"},
            "invalid string item",
            42,
        ]
        result = reviewer._parse_response(json.dumps(data), "https://example.com")
        assert len(result) == 1

    def test_dimension_with_non_dict_data(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        data = {"dimensions": {"navigation": "not a dict"}}
        result = reviewer._parse_response(json.dumps(data), "https://example.com")
        assert result == []

    def test_dimension_findings_non_dict_skipped(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        data = {
            "dimensions": {
                "navigation": {
                    "score": 80,
                    "findings": [
                        "not a dict",
                        {"severity": "low", "title": "OK", "description": "Fine"},
                    ],
                }
            }
        }
        result = reviewer._parse_response(json.dumps(data), "https://example.com")
        assert len(result) == 1


class TestParseDimensions:
    """Test _parse_dimensions method."""

    def test_stores_scores(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        dims = {
            "navigation": {"score": 75, "findings": []},
            "visual_hierarchy": {"score": 90, "findings": []},
        }
        reviewer._parse_dimensions(dims, "https://example.com")
        assert reviewer._last_dimension_scores["navigation"] == 75
        assert reviewer._last_dimension_scores["visual_hierarchy"] == 90

    def test_no_score_key(self) -> None:
        reviewer = UXReviewer(wm_client=FakeWMClient())
        dims = {"navigation": {"findings": [{"severity": "low", "title": "X", "description": "Y"}]}}
        result = reviewer._parse_dimensions(dims, "https://example.com")
        assert len(result) == 1


class TestConstants:
    """Test module-level constants."""

    def test_max_html_chars(self) -> None:
        assert MAX_HTML_CHARS == 50000

    def test_default_prompt_contains_dimensions(self) -> None:
        assert "Navigation" in DEFAULT_UX_PROMPT
        assert "Visual Hierarchy" in DEFAULT_UX_PROMPT
        assert "CTA" in DEFAULT_UX_PROMPT
        assert "{html}" in DEFAULT_UX_PROMPT
