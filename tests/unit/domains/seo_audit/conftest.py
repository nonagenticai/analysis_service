"""Hermetic live probes for the SEO audit tests.

``audit_seo`` with a ``url`` runs the robots.txt / sitemap.xml / llms.txt live
probes (design SEO-G2). Unit tests must not emit real HTTP, so by default both
probes are replaced with a stub that observes a healthy origin (every file
200, no crawler blocked) and returns no findings — the same findings an
unreachable placeholder host produced before, so existing scores are unchanged.

A test module that exercises the REAL probes (with httpx mocked) overrides the
``stub_live_probes`` fixture with a no-op of the same name.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.domains.seo_audit import service


async def _healthy_technical(url: str, *, observe: dict | None = None) -> list[dict]:
    if observe is not None:
        observe.update(
            {
                "ran": True,
                "reason": None,
                "robots": {"url": "stub/robots.txt", "status": 200, "error": None},
                "sitemap": {"url": "stub/sitemap.xml", "status": 200, "error": None},
            }
        )
    return []


async def _healthy_geo(url: str, *, observe: dict | None = None) -> list[dict]:
    if observe is not None:
        observe.update(
            {
                "ran": True,
                "reason": None,
                "error": None,
                "robots": {
                    "url": "stub/robots.txt",
                    "status": 200,
                    "ai_bots_blocked": [],
                    "search_engines_blocked": False,
                },
                "llms_txt": {"url": "stub/llms.txt", "status": 200},
            }
        )
    return []


@pytest.fixture(autouse=True)
def stub_live_probes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(service._SCANNER, "scan_live", _healthy_technical)
    monkeypatch.setattr(service._SCANNER, "scan_audit_live", _healthy_geo)
    yield
