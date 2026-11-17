"""Typed observation records shared by the SEO live probes, facts and audit service.

The live probes (``technical.scan_live``, ``geo.scan_geo_live``) fill a
:class:`ProbeObservation` with what they actually fetched, so the audit can tell
"checked and present" from "never checked". ``total=False``: a probe fills only
the keys it reached.
"""

from __future__ import annotations

from typing import TypedDict


class FileProbe(TypedDict, total=False):
    """One fetched file (robots.txt / sitemap.xml / llms.txt)."""

    url: str
    status: int | None
    error: str | None
    ai_bots_blocked: list[str]
    search_engines_blocked: bool


class ProbeObservation(TypedDict, total=False):
    """What one live probe observed."""

    ran: bool
    reason: str | None
    error: str | None
    origin: str
    robots: FileProbe
    sitemap: FileProbe
    llms_txt: FileProbe


class LiveObservation(TypedDict):
    """Both live probes of one audit (``None`` overall when no URL was given)."""

    technical: ProbeObservation
    geo: ProbeObservation


class CrawlObservation(TypedDict):
    """The cross-page crawl's state as seen by the audit."""

    requested: bool
    ran: bool
    findings: list[dict]
    note: str | None


def live_probe(live: LiveObservation, name: str) -> ProbeObservation:
    """``live[name]`` for a probe named at runtime (``"technical"`` / ``"geo"``)."""
    if name == "technical":
        return live["technical"]
    if name == "geo":
        return live["geo"]
    raise KeyError(name)


def probe_part(probe: ProbeObservation | None, part: str) -> FileProbe:
    """The observed file named at runtime (``robots`` / ``sitemap`` / ``llms_txt``), or ``{}``."""
    if probe is None:
        return {}
    if part == "robots":
        return probe.get("robots") or {}
    if part == "sitemap":
        return probe.get("sitemap") or {}
    if part == "llms_txt":
        return probe.get("llms_txt") or {}
    raise KeyError(part)
