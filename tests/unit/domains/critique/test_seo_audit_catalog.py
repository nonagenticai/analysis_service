"""Guard: the static SEO check catalog cannot rot (design 2026-10-17 X-G1).

Every ``seo-*`` id literal in the SEO scanner package must be catalogued (or be
a declared "could not check" status marker). A new check added without a
catalog row would otherwise be invisible in the inventory whenever it passes.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.domains.critique.services.scanners.seo import catalog
from src.domains.critique.services.scanners.seo.catalog import (
    CATALOG_IDS,
    LIVE_CHECK_PROBES,
    SEO_CHECK_CATALOG,
    STATUS_MARKER_IDS,
)
from src.domains.critique.services.scanners.seo.scoring import GROUP_WEIGHTS, LEGACY_CHECK_GROUPS

_PKG = Path(catalog.__file__).parent
# Any keyword argument naming an emitted id (`check_id=`, `errored_id=`, …).
# Worker names (`WORKER_NAME = "seo-intent-classify"`) are not check ids.
_ID_RE = re.compile(r"""\b\w*_id\s*=\s*["'](seo-[a-z0-9]+(?:-[a-z0-9]+)+)["']""")
# Modules whose literals are mappings/registries, not emitted checks.
_REGISTRIES = {"catalog.py", "scoring.py"}


def _emitted_ids() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(_PKG.glob("*.py")):
        if path.name in _REGISTRIES:
            continue
        for match in _ID_RE.finditer(path.read_text()):
            found.setdefault(match.group(1), path.name)
    return found


def test_the_scan_finds_literals_at_all():
    # NEG guard for the regex itself: an empty scan would pass vacuously.
    assert len(_emitted_ids()) >= 60


def test_every_emitted_check_id_is_catalogued():
    known = CATALOG_IDS | set(STATUS_MARKER_IDS)
    missing = {cid: mod for cid, mod in _emitted_ids().items() if cid not in known}
    assert not missing, f"add these to scanners/seo/catalog.py: {missing}"


def test_every_legacy_mapped_id_is_catalogued():
    known = CATALOG_IDS | set(STATUS_MARKER_IDS)
    assert set(LEGACY_CHECK_GROUPS) <= known


def test_catalog_ids_are_unique_and_grouped():
    ids = [spec.check_id for spec in SEO_CHECK_CATALOG]
    assert len(ids) == len(set(ids))
    assert {spec.bucket for spec in SEO_CHECK_CATALOG} <= set(GROUP_WEIGHTS)


def test_llm_ids_are_not_checks():
    # SEO-G7: LLM output is an ai annotation, never a check or a finding.
    assert not {"seo-keyword-intent", "seo-eeat-depth", "seo-content-gap"} & CATALOG_IDS
    assert not {"seo-keyword-intent", "seo-eeat-depth", "seo-content-gap"} & set(_emitted_ids())


def test_live_checks_require_a_url_and_have_a_probe():
    live = {spec.check_id for spec in SEO_CHECK_CATALOG if spec.requires == "url"}
    assert live == set(LIVE_CHECK_PROBES)
    for marker, parent in STATUS_MARKER_IDS.items():
        assert parent in LIVE_CHECK_PROBES, marker
