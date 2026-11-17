"""Shared error base for the synchronous scorecard domains.

``cro`` / ``seo_audit`` / ``marketing`` / ``artifact_score`` each raise a
domain-specific error on any fetch/parse/scan failure, which the route layer
surfaces as a fail-closed ``502`` (a fabricated score is never returned). They
all subclass this base so a route can catch the whole family with one
``except ScorecardError`` while each domain keeps its own importable, testable
error name. Kept free of any web-framework import so ``service.py`` modules can
raise it without depending on FastAPI.
"""

from __future__ import annotations


class ScorecardError(Exception):
    """Raised on any fetch/parse/scan failure — surfaced as a fail-closed 502."""
