"""Protocol definition for external tool adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class AdapterScanError(Exception):
    """An adapter could not MEASURE the target -- distinct from "found nothing".

    An adapter raises this instead of returning ``[]`` when its scan failed
    (browser launch, navigation, script injection, ...). Returning ``[]`` made a
    failed axe-core scan indistinguishable from a clean page: the live
    2026-10-18 nonagentic.ai audit logged ``Page.evaluate: Event`` and then
    reported 0 accessibility violations. ``ToolAdapterRegistry`` turns this into
    a per-adapter ``status="failed"`` record that the critique job surfaces.
    """


@runtime_checkable
class ToolAdapter(Protocol):
    """Protocol for external tool adapters.

    Implementations must provide tool_name, category, availability check,
    and a scan method that returns standardized findings.
    """

    @property
    def tool_name(self) -> str:
        """Unique name identifying this tool (e.g. 'axe-core', 'lighthouse')."""
        ...

    @property
    def category(self) -> str:
        """Finding category this adapter produces (e.g. 'accessibility', 'security')."""
        ...

    async def is_available(self) -> bool:
        """Return True if the external tool is installed and enabled."""
        ...

    async def scan(self, url: str, html: str | None = None) -> list[dict]:
        """Run the external tool against a URL and return findings.

        Args:
            url: Target URL to scan.
            html: Optional pre-fetched HTML content.

        Returns:
            List of finding dicts in the standard format. ``[]`` means the
            scan RAN and found nothing.

        Raises:
            AdapterScanError: the scan could not measure the target. Never
                return ``[]`` for a failure -- that reports it as clean.
        """
        ...
