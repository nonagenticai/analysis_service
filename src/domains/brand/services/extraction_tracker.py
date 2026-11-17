"""Track which extraction methods produced which brand data results."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class MethodResult:
    """Record of a single extraction method finding a value."""

    method: str  # e.g. "html_meta", "css_parser", "schema_org"
    field: str  # e.g. "primary_color", "logo_url", "name"
    value: str  # the extracted value
    confidence: float = 0.0  # 0.0-1.0
    timestamp_ms: int = 0  # when it was recorded


class ExtractionTracker:
    """Tracks extraction method results for a single extraction run.

    Instantiated per-extraction to collect which methods found which data.
    """

    def __init__(self) -> None:
        self._results: list[MethodResult] = []
        self._warnings: list[str] = []
        self._start_ms = int(time.monotonic() * 1000)

    def record(self, method: str, field: str, value: str, confidence: float = 1.0) -> None:
        """Record a successful extraction result."""
        self._results.append(
            MethodResult(
                method=method,
                field=field,
                value=str(value),
                confidence=max(0.0, min(1.0, confidence)),
                timestamp_ms=int(time.monotonic() * 1000) - self._start_ms,
            )
        )

    def warn(self, message: str) -> None:
        """Record a non-fatal warning."""
        self._warnings.append(message)

    def get_warnings(self) -> list[str]:
        """Get all recorded warnings."""
        return list(self._warnings)

    def get_methods_used(self) -> list[str]:
        """Get unique list of methods that found data, in order of first appearance."""
        seen: set[str] = set()
        methods: list[str] = []
        for r in self._results:
            if r.method not in seen:
                seen.add(r.method)
                methods.append(r.method)
        return methods

    def get_field_sources(self, field: str) -> list[MethodResult]:
        """Get all method results that found a value for a given field."""
        return [r for r in self._results if r.field == field]

    def get_all_results(self) -> list[MethodResult]:
        """Get all recorded results."""
        return list(self._results)

    def overall_confidence(self) -> float:
        """Calculate weighted aggregate confidence.

        Weight: each unique field contributes its highest-confidence result.
        Overall = average of per-field max confidences.
        """
        if not self._results:
            return 0.0

        field_max: dict[str, float] = {}
        for r in self._results:
            if r.field not in field_max or r.confidence > field_max[r.field]:
                field_max[r.field] = r.confidence

        return sum(field_max.values()) / len(field_max) if field_max else 0.0

    def field_count(self) -> int:
        """Number of unique fields that have at least one result."""
        return len({r.field for r in self._results})

    def result_count(self) -> int:
        """Total number of recorded results."""
        return len(self._results)

    def to_dict(self) -> dict:
        """Full serialization of tracker state."""
        return {
            "methods_used": self.get_methods_used(),
            "overall_confidence": round(self.overall_confidence(), 4),
            "field_count": self.field_count(),
            "result_count": self.result_count(),
            "results": [
                {
                    "method": r.method,
                    "field": r.field,
                    "value": r.value,
                    "confidence": r.confidence,
                    "timestamp_ms": r.timestamp_ms,
                }
                for r in self._results
            ],
        }
