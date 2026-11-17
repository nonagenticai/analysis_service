"""Shared response parsing -- extract JSON from AI tool output."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_MAX_RESPONSE_LENGTH = 500_000


def _scan_balanced(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """Scan from index ``start`` for a balanced ``open_ch``/``close_ch`` span.

    Treats quoted strings opaquely so braces inside JSON strings do not
    affect the depth counter.
    """
    depth = 0
    in_string = False
    j = start
    while j < len(text):
        c = text[j]
        if in_string:
            if c == "\\":
                j = min(j + 2, len(text))
                continue
            if c == '"':
                in_string = False
            j += 1
            continue
        if c == '"':
            in_string = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : j + 1]
        j += 1
    return None


def _find_json_object(text: str) -> str | None:
    """Find the first balanced JSON object or array in *text*.

    Uses a brace-counting approach that correctly handles nested structures
    (e.g. ``{"a": {"b": 1}}``) and quoted strings containing braces.
    """
    for i, ch in enumerate(text):
        if ch == "{":
            found = _scan_balanced(text, i, "{", "}")
            if found is not None:
                return found
        elif ch == "[":
            found = _scan_balanced(text, i, "[", "]")
            if found is not None:
                return found
    return None


def _try_fenced_json(raw: str) -> dict[str, object] | list[object] | None:
    """Strategy 1: parse a ```json ... ``` fenced block."""
    m = re.search(r"```json\s*\n(.*?)\n\s*```", raw, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        logger.warning("Fenced JSON block found but failed to parse")
        return None
    if isinstance(parsed, dict | list):
        return parsed
    logger.warning("Fenced JSON block parsed to %s, expected dict or list", type(parsed).__name__)
    return None


def _try_unfenced_block(raw: str) -> dict[str, object] | list[object] | None:
    """Strategy 2: parse a ``` ... ``` block without a language tag."""
    m = re.search(r"```\s*\n(.*?)\n\s*```", raw, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict | list) else None


def _try_balanced_scan(raw: str) -> dict[str, object] | list[object] | None:
    """Strategy 3: balanced brace/bracket scan."""
    json_str = _find_json_object(raw)
    if not json_str:
        return None
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("Bare JSON found but failed to parse")
        return None


def _try_first_to_last_brace(raw: str) -> dict[str, object] | list[object] | None:
    """Strategy 4: substring between the first ``{`` and the last ``}``."""
    first_brace = raw.find("{")
    last_brace = raw.rfind("}")
    if first_brace == -1 or last_brace <= first_brace:
        return None
    try:
        parsed = json.loads(raw[first_brace : last_brace + 1])
    except json.JSONDecodeError:
        logger.warning("First-to-last brace extraction failed to parse")
        return None
    return parsed if isinstance(parsed, dict | list) else None


def parse_json_from_response(
    raw: str, *, required: bool = False
) -> dict[str, object] | list[object]:
    """Extract a JSON object or array from an AI response.

    Tries four strategies in order:

    1. Fenced ``json`` code block (````json ... ````).
    2. Unfenced code block (```` ``` ... ``` ````).
    3. Balanced brace/bracket counting via :func:`_find_json_object`.
    4. First-to-last brace fallback.

    Returns a ``dict`` when the response contains a JSON object, or a ``list``
    when it contains a JSON array.  On any failure path returns an empty
    ``dict`` (``{}``) unless *required* is True, in which case
    :class:`ValueError` is raised.
    """
    if len(raw) > _MAX_RESPONSE_LENGTH:
        logger.warning(
            "AI response truncated from %d to %d chars for parsing",
            len(raw),
            _MAX_RESPONSE_LENGTH,
        )
        raw = raw[:_MAX_RESPONSE_LENGTH]

    for strategy in (
        _try_fenced_json,
        _try_unfenced_block,
        _try_balanced_scan,
        _try_first_to_last_brace,
    ):
        result = strategy(raw)
        if result is not None:
            return result

    if required:
        raise ValueError("No JSON found in AI response")
    return {}
