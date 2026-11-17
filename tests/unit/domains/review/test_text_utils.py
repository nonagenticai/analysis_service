"""Unit tests for shared text/score extraction utilities."""

import pytest

from src.domains.review.services.text_utils import (
    collect_text_set,
    extract_item_text,
    extract_overall_score,
)


@pytest.mark.unit
class TestExtractOverallScore:
    def test_computed_score_preferred(self) -> None:
        result = {"computed_overall_score": 8.5, "overall_score": 7.0}
        assert extract_overall_score(result) == 8.5

    def test_falls_back_to_overall_score(self) -> None:
        result = {"overall_score": 7.0}
        assert extract_overall_score(result) == 7.0

    def test_falls_back_to_feature_completeness(self) -> None:
        result = {"feature_completeness_score": 6.5}
        assert extract_overall_score(result) == 6.5

    def test_string_score_coerced(self) -> None:
        result = {"overall_score": "7.5"}
        assert extract_overall_score(result) == 7.5

    def test_returns_none_for_empty(self) -> None:
        assert extract_overall_score({}) is None

    def test_returns_none_for_error_result(self) -> None:
        assert extract_overall_score({"error": "failed", "overall_score": 7}) is None

    def test_returns_none_for_non_dict(self) -> None:
        assert extract_overall_score("not a dict") is None

    def test_returns_none_for_invalid_score(self) -> None:
        assert extract_overall_score({"overall_score": "not a number"}) is None


@pytest.mark.unit
class TestExtractItemText:
    def test_string_passthrough(self) -> None:
        assert extract_item_text("hello") == "hello"

    def test_dict_description(self) -> None:
        assert extract_item_text({"description": "Fix bug"}) == "Fix bug"

    def test_dict_title(self) -> None:
        assert extract_item_text({"title": "Add search"}) == "Add search"

    def test_dict_text(self) -> None:
        assert extract_item_text({"text": "Some text"}) == "Some text"

    def test_dict_name(self) -> None:
        assert extract_item_text({"name": "Widget"}) == "Widget"

    def test_dict_priority_order(self) -> None:
        item = {"description": "Desc", "title": "Title", "text": "Text", "name": "Name"}
        assert extract_item_text(item) == "Desc"

    def test_dict_fallback_to_str(self) -> None:
        item = {"unknown_key": "value"}
        result = extract_item_text(item)
        assert "unknown_key" in result

    def test_non_string_non_dict(self) -> None:
        assert extract_item_text(42) == "42"


@pytest.mark.unit
class TestCollectTextSet:
    def test_collects_strings(self) -> None:
        results = {
            "design_review": {"strengths": ["Clean layout", "Good colors"]},
        }
        texts = collect_text_set(results, "strengths")
        assert texts == {"Clean layout", "Good colors"}

    def test_collects_dict_items(self) -> None:
        results = {
            "usability_review": {"issues": [{"description": "Poor contrast"}]},
        }
        texts = collect_text_set(results, "issues")
        assert texts == {"Poor contrast"}

    def test_multiple_keys(self) -> None:
        results = {
            "design_review": {
                "strengths": ["A"],
                "weaknesses": ["B"],
            },
        }
        texts = collect_text_set(results, "strengths", "weaknesses")
        assert texts == {"A", "B"}

    def test_skips_error_results(self) -> None:
        results = {
            "design_review": {"error": "failed", "strengths": ["Should skip"]},
        }
        texts = collect_text_set(results, "strengths")
        assert texts == set()

    def test_skips_non_list_items(self) -> None:
        results = {
            "design_review": {"strengths": "not a list"},
        }
        texts = collect_text_set(results, "strengths")
        assert texts == set()

    def test_empty_results(self) -> None:
        assert collect_text_set({}, "strengths") == set()

    def test_deduplicates(self) -> None:
        results = {
            "design_review": {"strengths": ["Same"]},
            "usability_review": {"strengths": ["Same"]},
        }
        texts = collect_text_set(results, "strengths")
        assert texts == {"Same"}
