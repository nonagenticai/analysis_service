"""Shared text and score extraction utilities.

Eliminates duplication between findings.py, comparison.py,
report_generator.py, and summary.py.
"""

from __future__ import annotations


def extract_overall_score(result: dict | object) -> float | None:
    """Pull the best available overall score from a single review result.

    Checks ``computed_overall_score``, ``overall_score``, and
    ``feature_completeness_score`` in priority order.

    Returns:
        The score as a float, or ``None`` when unavailable.
    """
    if not isinstance(result, dict) or "error" in result:
        return None
    data: dict[str, object] = {str(k): v for k, v in result.items()}
    for key in ("computed_overall_score", "overall_score", "feature_completeness_score"):
        raw = data.get(key)
        if raw is not None:
            try:
                return float(str(raw))
            except ValueError, TypeError:
                continue
    return None


def extract_item_text(item: object) -> str:
    """Extract display text from a string or dict finding/recommendation.

    For dict items, checks ``description``, ``title``, ``text``, and ``name``
    keys in that priority order.
    """
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        d: dict[str, object] = {str(k): v for k, v in item.items()}
        return str(d.get("description") or d.get("title") or d.get("text") or d.get("name") or item)
    return str(item)


def collect_text_set(results: dict[str, dict], *keys: str) -> set[str]:
    """Collect normalised text items across all review types for given keys.

    Iterates every review result, extracts items from the specified keys,
    and returns a deduplicated set of stripped text values.
    """
    texts: set[str] = set()
    for result in results.values():
        if not isinstance(result, dict) or "error" in result:
            continue
        for key in keys:
            items = result.get(key, [])
            if not isinstance(items, list):
                continue
            for item in items:
                text = ""
                if isinstance(item, str):
                    text = item.strip()
                elif isinstance(item, dict):
                    d2: dict[str, object] = {str(k): v for k, v in item.items()}
                    text = str(
                        d2.get("description", "") or d2.get("title", "") or d2.get("text", "")
                    ).strip()
                if text:
                    texts.add(text)
    return texts
