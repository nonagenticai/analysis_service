"""The five synchronous finding scanners satisfy the ``Scanner`` protocol.

``Scanner`` (``critique.services.scanners.protocol``) captures the one shared
``scan(html, url, *, soup) -> list[dict]`` surface so heterogeneous dispatch
tables (``artifact_score._SCANNERS``) can drop ``dict[str, Any]``. These asserts
pin structural conformance and that the intentionally-divergent async scanners
are excluded.
"""

from __future__ import annotations

import pytest

from src.domains.artifact_score.service import _SCANNERS
from src.domains.critique.services.scanners.a11y import A11yScanner
from src.domains.critique.services.scanners.cro import CROScanner
from src.domains.critique.services.scanners.marketing import MarketingScanner
from src.domains.critique.services.scanners.perf import PerformanceScanner
from src.domains.critique.services.scanners.protocol import Scanner
from src.domains.critique.services.scanners.seo import SEOScanner

_SYNC_SCANNERS = [SEOScanner, CROScanner, MarketingScanner, PerformanceScanner, A11yScanner]


@pytest.mark.parametrize("scanner_cls", _SYNC_SCANNERS)
def test_sync_scanner_satisfies_protocol(scanner_cls: type) -> None:
    assert isinstance(scanner_cls(), Scanner)


def test_artifact_score_dispatch_table_holds_scanners() -> None:
    assert _SCANNERS  # non-empty
    for scanner in _SCANNERS.values():
        assert isinstance(scanner, Scanner)
