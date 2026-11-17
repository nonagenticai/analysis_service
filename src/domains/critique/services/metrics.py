"""Lightweight Prometheus metrics without external dependencies."""

from __future__ import annotations

import threading
from collections import defaultdict


class _Counter:
    """Thread-safe monotonic counter.

    Attributes:
        name: Prometheus metric name.
        help_text: Description for the HELP line.
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: str) -> None:
        """Increment the counter by value for the given label set."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += value

    def render(self) -> str:
        """Render counter in Prometheus text exposition format."""
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            for labels, value in sorted(self._values.items()):
                label_str = _format_labels(labels)
                lines.append(f"{self.name}{label_str} {value}")
        return "\n".join(lines)


class _Gauge:
    """Thread-safe gauge.

    Attributes:
        name: Prometheus metric name.
        help_text: Description for the HELP line.
    """

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        """Set the gauge to an absolute value."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels: str) -> None:
        """Increment the gauge by value."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += value

    def dec(self, value: float = 1.0, **labels: str) -> None:
        """Decrement the gauge by value."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] -= value

    def render(self) -> str:
        """Render gauge in Prometheus text exposition format."""
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} gauge"]
        with self._lock:
            for labels, value in sorted(self._values.items()):
                label_str = _format_labels(labels)
                lines.append(f"{self.name}{label_str} {value}")
        return "\n".join(lines)


class _Histogram:
    """Thread-safe histogram with configurable buckets.

    Attributes:
        name: Prometheus metric name.
        help_text: Description for the HELP line.
    """

    DEFAULT_BUCKETS = (
        0.005,
        0.01,
        0.025,
        0.05,
        0.1,
        0.25,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        float("inf"),
    )

    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...] | None = None) -> None:
        self.name = name
        self.help_text = help_text
        self._buckets = buckets or self.DEFAULT_BUCKETS
        self._counts: dict[tuple[tuple[str, str], ...], list[int]] = {}
        self._sums: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)
        self._totals: dict[tuple[tuple[str, str], ...], int] = defaultdict(int)
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        """Record an observed value into the appropriate buckets."""
        key = tuple(sorted(labels.items()))
        with self._lock:
            if key not in self._counts:
                self._counts[key] = [0] * len(self._buckets)
            for i, bound in enumerate(self._buckets):
                if value <= bound:
                    self._counts[key][i] += 1
            self._sums[key] += value
            self._totals[key] += 1

    def render(self) -> str:
        """Render histogram in Prometheus text exposition format."""
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} histogram",
        ]
        with self._lock:
            for labels in sorted(self._counts.keys()):
                label_str = _format_labels(labels)
                cumulative = 0
                for i, bound in enumerate(self._buckets):
                    cumulative += self._counts[labels][i]
                    le = "+Inf" if bound == float("inf") else str(bound)
                    le_labels = _format_labels(labels, le=le)
                    lines.append(f"{self.name}_bucket{le_labels} {cumulative}")
                lines.append(f"{self.name}_sum{label_str} {self._sums[labels]}")
                lines.append(f"{self.name}_count{label_str} {self._totals[labels]}")
        return "\n".join(lines)


def _format_labels(labels: tuple[tuple[str, str], ...], **extra: str) -> str:
    """Format labels as Prometheus label string."""
    all_labels = dict(labels)
    all_labels.update(extra)
    if not all_labels:
        return ""
    parts = [f'{k}="{v}"' for k, v in sorted(all_labels.items())]
    return "{" + ",".join(parts) + "}"


# Global metric instances
jobs_total = _Counter("critique_jobs_total", "Total critique jobs processed")
jobs_active = _Gauge("critique_jobs_active", "Currently active critique jobs")
scan_duration = _Histogram(
    "critique_scan_duration_seconds",
    "Time spent scanning",
    buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, float("inf")),
)
scanner_duration = _Histogram(
    "critique_scanner_duration_seconds",
    "Time spent per scanner",
)
findings_total = _Counter("critique_findings_total", "Total findings produced")
rate_limit_hits = _Counter("critique_rate_limit_hits_total", "Rate limit 429 responses")
webhook_deliveries = _Counter("critique_webhook_deliveries_total", "Webhook delivery attempts")
cache_hits = _Counter("critique_cache_hits_total", "Scan cache hits")
cache_misses = _Counter("critique_cache_misses_total", "Scan cache misses")


def render_prometheus() -> str:
    """Render all metrics in Prometheus text exposition format."""
    metrics = [
        jobs_total,
        jobs_active,
        scan_duration,
        scanner_duration,
        findings_total,
        rate_limit_hits,
        webhook_deliveries,
        cache_hits,
        cache_misses,
    ]
    sections = [m.render() for m in metrics if m.render().strip()]
    return "\n\n".join(sections) + "\n"
