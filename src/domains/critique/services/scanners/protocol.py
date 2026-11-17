"""Structural type for the synchronous, HTML-in finding scanners.

``SEOScanner`` / ``CROScanner`` / ``MarketingScanner`` / ``PerformanceScanner`` /
``A11yScanner`` all expose the same ``scan(html, url, *, soup)`` entry point and
return a flat ``list`` of ``make_finding`` dicts. This ``Protocol`` captures that
one shared surface so dispatch tables that hold a mix of them (e.g.
``artifact_score``'s ``_SCANNERS``) can be typed as ``dict[str, Scanner]``
instead of ``dict[str, Any]``, and tests can substitute a duck-typed fake.

Deliberately narrow: the async / URL-first scanners (``SecurityScanner``,
``CodeQualityScanner``, ``UXReviewer``) do NOT match this shape and keep their
own contracts (``UXReviewer`` already has ``UXReviewerProtocol``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from bs4 import BeautifulSoup


@runtime_checkable
class Scanner(Protocol):
    """A synchronous finding scanner: HTML in, ``make_finding`` dicts out."""

    def scan(self, html: str, url: str = "", *, soup: BeautifulSoup | None = None) -> list[dict]:
        """Scan a rendered page and return findings.

        Args:
            html: Rendered HTML of the page.
            url: Source URL (blank for inline HTML).
            soup: Optional pre-parsed soup to avoid a re-parse.

        Returns:
            A flat list of standard ``make_finding`` dicts.
        """
        ...
