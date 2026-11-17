"""Every scorecard domain error is one ``ScorecardError`` family.

``cro`` / ``seo_audit`` / ``marketing`` / ``artifact_score`` each keep their own
importable, ``pytest.raises``-able error name, but all now subclass the single
``ScorecardError`` base (in ``src.domains.scorecards.errors``) so the route layer
can catch the whole family with one ``except ScorecardError``. These asserts pin
that relationship — the same fail-closed-502 behavior the four now-shared
byte-identical ``class …Error(Exception)`` copies had.
"""

from __future__ import annotations

import pytest

from src.domains.artifact_score.service import ArtifactScoreError
from src.domains.cro.service import CroScoreError
from src.domains.marketing.service import MarketingScoreError
from src.domains.scorecards.errors import ScorecardError
from src.domains.seo_audit.service import SeoAuditError

_DOMAIN_ERRORS: list[type[ScorecardError]] = [
    ArtifactScoreError,
    CroScoreError,
    MarketingScoreError,
    SeoAuditError,
]


@pytest.mark.parametrize("err", _DOMAIN_ERRORS)
def test_domain_error_subclasses_scorecard_error(err: type[ScorecardError]) -> None:
    assert issubclass(err, ScorecardError)


@pytest.mark.parametrize("err", _DOMAIN_ERRORS)
def test_domain_error_caught_as_scorecard_error(err: type[ScorecardError]) -> None:
    with pytest.raises(ScorecardError):
        raise err("boom")
