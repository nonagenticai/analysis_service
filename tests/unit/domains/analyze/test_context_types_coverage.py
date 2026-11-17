"""Unit tests for context types validation (src/services/context/types.py)."""

from __future__ import annotations

import pytest

from src.domains.analyze.services.context.types import (
    ContextValidationResult,
    ValidationIssue,
    VisualPreferences,
)


class TestVisualPreferencesValidation:
    def test_valid_hex_6_char(self):
        vp = VisualPreferences(highlight_color="#ff0000")
        assert vp.highlight_color == "#ff0000"

    def test_valid_hex_3_char(self):
        vp = VisualPreferences(highlight_color="#f00")
        assert vp.highlight_color == "#f00"

    def test_empty_string_accepted(self):
        vp = VisualPreferences(highlight_color="")
        assert vp.highlight_color == ""

    def test_invalid_hex_color_raises(self):
        with pytest.raises(ValueError, match="valid CSS hex color"):
            VisualPreferences(highlight_color="red")

    def test_invalid_hex_too_long(self):
        with pytest.raises(ValueError, match="valid CSS hex color"):
            VisualPreferences(highlight_color="#ff00ff00")


class TestContextValidationResult:
    def test_is_clean_with_no_issues(self):
        result = ContextValidationResult()
        assert result.is_clean is True
        assert result.has_errors is False
        assert result.has_warnings is False

    def test_has_warnings_only(self):
        result = ContextValidationResult(
            issues=[ValidationIssue(level="warning", check="test", message="warn")]
        )
        assert result.is_clean is False
        assert result.has_warnings is True
        assert result.has_errors is False

    def test_has_errors(self):
        result = ContextValidationResult(
            issues=[ValidationIssue(level="error", check="test", message="err")]
        )
        assert result.has_errors is True

    def test_mixed_issues(self):
        result = ContextValidationResult(
            issues=[
                ValidationIssue(level="warning", check="a", message="w"),
                ValidationIssue(level="error", check="b", message="e"),
            ]
        )
        assert result.has_errors is True
        assert result.has_warnings is True
        assert result.is_clean is False
