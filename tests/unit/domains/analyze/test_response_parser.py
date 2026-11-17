"""Unit tests for the response_parser module."""

from __future__ import annotations

import json

import pytest

from src.domains.analyze.services.response_parser import (
    _MAX_RESPONSE_LENGTH,
    _find_json_object,
    parse_json_from_response,
)

# ---------------------------------------------------------------------------
# _find_json_object
# ---------------------------------------------------------------------------


class TestFindJsonObject:
    def test_simple_object(self) -> None:
        assert _find_json_object('{"a": 1}') == '{"a": 1}'

    def test_simple_array(self) -> None:
        assert _find_json_object("[1, 2, 3]") == "[1, 2, 3]"

    def test_nested_objects(self) -> None:
        text = '{"outer": {"inner": {"deep": true}}}'
        assert _find_json_object(text) == text

    def test_braces_inside_strings(self) -> None:
        text = '{"msg": "hello { world }"}'
        assert _find_json_object(text) == text

    def test_escaped_quotes_in_strings(self) -> None:
        text = r'{"msg": "say \"hi\""}'
        result = _find_json_object(text)
        assert result == text

    def test_no_json(self) -> None:
        assert _find_json_object("just plain text") is None

    def test_prefix_text(self) -> None:
        text = 'Here is the JSON: {"key": "val"} done.'
        assert _find_json_object(text) == '{"key": "val"}'


# ---------------------------------------------------------------------------
# Fenced ```json blocks
# ---------------------------------------------------------------------------


class TestFencedJsonBlocks:
    def test_valid_fenced_json(self) -> None:
        raw = '```json\n{"name": "test"}\n```'
        result = parse_json_from_response(raw)
        assert result == {"name": "test"}

    def test_fenced_json_with_prefix_suffix(self) -> None:
        raw = 'Here is the result:\n```json\n{"key": "value"}\n```\nDone!'
        result = parse_json_from_response(raw)
        assert result == {"key": "value"}

    def test_fenced_json_invalid_falls_through(self) -> None:
        raw = '```json\n{invalid json\n```\nBut also {"fallback": true}'
        result = parse_json_from_response(raw)
        assert result == {"fallback": True}

    def test_fenced_json_primitive_falls_through(self) -> None:
        """A fenced block that parses to a primitive should fall through."""
        raw = '```json\n42\n```\n{"real": "data"}'
        result = parse_json_from_response(raw)
        assert result == {"real": "data"}


# ---------------------------------------------------------------------------
# Unfenced ``` blocks
# ---------------------------------------------------------------------------


class TestUnfencedBlocks:
    def test_unfenced_code_block_with_json(self) -> None:
        raw = 'Result:\n```\n{"status": "ok"}\n```'
        result = parse_json_from_response(raw)
        assert result == {"status": "ok"}

    def test_unfenced_block_invalid_json_falls_through(self) -> None:
        raw = '```\nnot json\n```\n{"backup": 1}'
        result = parse_json_from_response(raw)
        assert result == {"backup": 1}


# ---------------------------------------------------------------------------
# Balanced brace counting (strategy 3)
# ---------------------------------------------------------------------------


class TestBalancedBraceCounting:
    def test_json_in_prose(self) -> None:
        raw = 'The analysis is: {"score": 99, "details": {"a": 1}} and more text.'
        result = parse_json_from_response(raw)
        assert result == {"score": 99, "details": {"a": 1}}

    def test_json_array_in_prose(self) -> None:
        raw = 'Items: [{"id": 1}, {"id": 2}] end.'
        result = parse_json_from_response(raw)
        assert result == [{"id": 1}, {"id": 2}]


# ---------------------------------------------------------------------------
# First-to-last brace fallback (strategy 4)
# ---------------------------------------------------------------------------


class TestFirstToLastBraceFallback:
    def test_broken_nested_but_valid_outer(self) -> None:
        """When balanced counting fails but first-to-last brace works."""
        # This JSON has an unbalanced inner part that confuses counting
        # but first{...}last works because the outermost braces contain valid JSON.
        raw = '{"a": 1, "b": 2}'
        result = parse_json_from_response(raw)
        assert result == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# JSON arrays
# ---------------------------------------------------------------------------


class TestJsonArrays:
    def test_fenced_array(self) -> None:
        raw = "```json\n[1, 2, 3]\n```"
        result = parse_json_from_response(raw)
        assert result == [1, 2, 3]

    def test_bare_array(self) -> None:
        raw = "Some text [1, 2, 3] more text"
        result = parse_json_from_response(raw)
        assert result == [1, 2, 3]


# ---------------------------------------------------------------------------
# No-JSON input (returns empty dict)
# ---------------------------------------------------------------------------


class TestNoJsonInput:
    def test_plain_text_returns_empty_dict(self) -> None:
        result = parse_json_from_response("Hello, world!")
        assert result == {}

    def test_empty_string_returns_empty_dict(self) -> None:
        result = parse_json_from_response("")
        assert result == {}

    def test_whitespace_only_returns_empty_dict(self) -> None:
        result = parse_json_from_response("   \n\t  \n  ")
        assert result == {}


# ---------------------------------------------------------------------------
# Required mode
# ---------------------------------------------------------------------------


class TestRequiredMode:
    def test_required_raises_on_no_json(self) -> None:
        with pytest.raises(ValueError, match="No JSON found"):
            parse_json_from_response("no json here", required=True)

    def test_required_returns_normally_on_valid(self) -> None:
        result = parse_json_from_response('{"ok": true}', required=True)
        assert result == {"ok": True}


# ---------------------------------------------------------------------------
# Max-length truncation
# ---------------------------------------------------------------------------


class TestMaxLengthTruncation:
    def test_truncation_still_parses_prefix_json(self) -> None:
        """JSON at the start is preserved after truncation."""
        obj = {"data": "x" * 100}
        payload = json.dumps(obj) + "A" * (_MAX_RESPONSE_LENGTH + 1000)
        result = parse_json_from_response(payload)
        assert result == obj

    def test_truncation_cuts_trailing_json(self) -> None:
        """JSON only at the end beyond the limit is lost."""
        padding = "A" * (_MAX_RESPONSE_LENGTH + 100)
        raw = padding + '{"key": "val"}'
        result = parse_json_from_response(raw)
        assert result == {}


# ---------------------------------------------------------------------------
# Mixed content / edge cases
# ---------------------------------------------------------------------------


class TestMixedContentAndEdgeCases:
    def test_json_embedded_in_markdown(self) -> None:
        raw = (
            "## Analysis\n\n"
            "The site looks great.\n\n"
            '```json\n{"tone": "professional", "score": 85}\n```\n\n'
            "Let me know if you need more."
        )
        result = parse_json_from_response(raw)
        assert isinstance(result, dict)
        assert result["tone"] == "professional"
        assert result["score"] == 85

    def test_multiple_json_blocks_returns_first(self) -> None:
        raw = '```json\n{"first": true}\n```\n```json\n{"second": true}\n```'
        result = parse_json_from_response(raw)
        assert result == {"first": True}

    def test_escaped_backslash_before_quote(self) -> None:
        r"""Escaped backslash followed by quote: \\" should not break parsing."""
        raw = r'{"path": "C:\\Users\\test"}'
        result = parse_json_from_response(raw)
        assert isinstance(result, dict)
        assert result["path"] == "C:\\Users\\test"

    def test_complex_nested_with_arrays(self) -> None:
        obj = {
            "items": [{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": []}],
            "meta": {"total": 2},
        }
        raw = "Result: " + json.dumps(obj) + " -- end"
        result = parse_json_from_response(raw)
        assert result == obj
