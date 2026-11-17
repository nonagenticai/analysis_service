"""sources threading: ScorecardRequest.sources → service → maybe_ground_claims.

Additive, opt-in. A spy on each domain service's ``maybe_ground_claims`` captures
the ``sources`` kwarg to prove the request value threads all the way through; the
negative control proves an absent value reaches grounding as ``None``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domains.cro.service import score_cro
from src.domains.marketing.service import score_marketing
from src.domains.seo_audit.service import audit_seo

_HTML = "<html><body><h1>Hello</h1><p>Some copy.</p></body></html>"


async def _capture_sources(monkeypatch: pytest.MonkeyPatch, module: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _spy(_refs: object, **kwargs: object) -> None:
        captured["sources"] = kwargs.get("sources")
        captured["called"] = True
        return

    monkeypatch.setattr(f"src.domains.{module}.service.maybe_ground_claims", _spy)
    return captured


@pytest.mark.asyncio
async def test_score_cro_threads_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture_sources(monkeypatch, "cro")
    await score_cro(html=_HTML, url=None, include_grounding=True, sources=["SRC_ALPHA"])
    assert captured["called"] and captured["sources"] == ["SRC_ALPHA"]


@pytest.mark.asyncio
async def test_audit_seo_threads_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture_sources(monkeypatch, "seo_audit")
    await audit_seo(html=_HTML, url=None, include_grounding=True, sources=["SRC_BETA"])
    assert captured["called"] and captured["sources"] == ["SRC_BETA"]


@pytest.mark.asyncio
async def test_score_marketing_threads_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture_sources(monkeypatch, "marketing")
    await score_marketing(html=_HTML, url=None, include_grounding=True, sources=["SRC_GAMMA"])
    assert captured["called"] and captured["sources"] == ["SRC_GAMMA"]


@pytest.mark.asyncio
async def test_sources_absent_threads_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # NEG control: no sources on the request → None reaches grounding (default path).
    captured = await _capture_sources(monkeypatch, "cro")
    await score_cro(html=_HTML, url=None, include_grounding=True)
    assert captured["called"] and captured["sources"] is None
