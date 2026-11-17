"""Module-level types and constants for critique service phases."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

ALL_CATEGORIES = {"seo", "security", "accessibility", "performance", "ux", "code_quality"}

_background_tasks: set[asyncio.Task[None]] = set()


@dataclass
class _ScoringResult:
    """Result of dedup/suppression/scoring/summarization phase."""

    findings: list[dict[str, Any]]
    scores: dict[str, float]
    overall_score: float
    confidence: dict[str, float]
    summary: str
    report: str
    token_usage: dict[str, int] | None
