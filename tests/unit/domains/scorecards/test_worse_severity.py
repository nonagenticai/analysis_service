"""The shared worst-severity comparison used by both per-key rollups.

``worse_severity`` (in ``src.domains.scorecards.findings``) is the one copy of
the ``SEVERITY_ORDER``-ranked "keep the worse of two" compare that
``summarize_buckets`` and ``artifact_score._summarize`` previously hand-inlined.
"""

from __future__ import annotations

import pytest

from src.domains.scorecards.findings import worse_severity


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        ("info", "critical", "critical"),  # candidate worse → candidate
        ("critical", "info", "critical"),  # current worse → current
        ("medium", "high", "high"),
        ("high", "medium", "high"),
        ("low", "low", "low"),  # tie → current (candidate not strictly worse)
        ("info", "info", "info"),
    ],
)
def test_worse_severity_keeps_the_worse_rank(current: str, candidate: str, expected: str) -> None:
    assert worse_severity(current, candidate) == expected


def test_unknown_severity_ranks_as_info() -> None:
    # Unknown labels default to rank 4 (info), so a known severity always wins.
    assert worse_severity("bogus", "high") == "high"
    assert worse_severity("high", "bogus") == "high"
    # Two unknowns → the incumbent (candidate not strictly worse).
    assert worse_severity("bogus", "mystery") == "bogus"
