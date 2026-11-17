"""The shared url-XOR-html validator behaves identically across every model.

``SourceXorValidatorMixin`` (in ``src.domains.scorecards.schemas``) is the one
copy of the "exactly one of url / html" rule; ``ScorecardRequest`` (base for
cro / seo / marketing) and the ``artifact_score`` request models
(``ArtifactScoreRequest`` / ``ArtifactSource``) all mix it in. These asserts pin
that the rule is enforced identically wherever it is reused — the same behavior
the two now-deleted byte-identical artifact_score copies had.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from src.domains.artifact_score.schemas import ArtifactScoreRequest, ArtifactSource
from src.domains.scorecards.schemas import ScorecardRequest, SourceXorValidatorMixin

# Every model that mixes in the shared validator.
_MODELS: list[type[BaseModel]] = [ScorecardRequest, ArtifactScoreRequest, ArtifactSource]


def test_every_source_model_uses_the_shared_mixin() -> None:
    for model in _MODELS:
        assert issubclass(model, SourceXorValidatorMixin)


@pytest.mark.parametrize("model", _MODELS)
def test_url_only_is_accepted(model: type[BaseModel]) -> None:
    assert isinstance(model(url="https://example.com"), model)


@pytest.mark.parametrize("model", _MODELS)
def test_html_only_is_accepted(model: type[BaseModel]) -> None:
    assert isinstance(model(html="<html></html>"), model)


@pytest.mark.parametrize("model", _MODELS)
def test_both_sources_is_rejected(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError, match="exactly one of 'url' or 'html'"):
        model(url="https://example.com", html="<html></html>")


@pytest.mark.parametrize("model", _MODELS)
def test_neither_source_is_rejected(model: type[BaseModel]) -> None:
    with pytest.raises(ValidationError, match="exactly one of 'url' or 'html'"):
        model()


@pytest.mark.parametrize("model", _MODELS)
def test_blank_source_counts_as_absent(model: type[BaseModel]) -> None:
    # A whitespace-only url is not a source — with no html it must be rejected.
    with pytest.raises(ValidationError, match="exactly one of 'url' or 'html'"):
        model(url="   ")
