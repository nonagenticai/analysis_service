"""Guard against a settings section that nothing reads.

`CritiqueSettings` (env_prefix "CRITIQUE_") sat on `Settings.critique` and was
read by nothing — not src/, tests/, scripts/, the repo's k8s manifests,
the fleet deployment configuration, nor the live deployment. It looked authoritative because
every field name it declared IS read, but on a *different* section under a
different prefix (`scanners` / `scoring` / `external_tools` / `features`).
An operator setting `CRITIQUE_AXE_ENABLED=false` got silence and a still-running
scanner.

These tests make that class of defect fail loudly instead of shipping quietly.

⚠️ TWO vacuities have now been found in this file's own detector. Both let a
wrong answer be written down as fact, so read `test_reader_detection_*` before
trusting any verdict here:

  1. A text search for `".critique."` matched the import path
     `src.domains.critique.services`, so the detector passed against the very
     defect it existed to catch. Fixed by parsing instead of substring-matching.
  2. The AST version only understood a field read written as one expression
     (`settings.playwright.headless`). `src/domains/brand/stages/fetch.py` binds
     the section to a local first (`pw = get_settings().playwright`), so
     `playwright` — five live fields on the brand-extraction path — was reported
     unread and sat on the allowlist below labelled "legacy/inert". Deleting it
     on that authority would have broken brand extraction.

The lesson both share: this detector's FALSE NEGATIVE is the dangerous
direction, because its output gets copied into CLAUDE.md as ground truth.
"""

from __future__ import annotations

import ast
import os
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "src" / "config"
SRC_DIR = REPO_ROOT / "src"

# Sections with no `<obj>.<name>.<field>` read inside THIS repo's src/, each with
# the reason it is nonetheless kept.
#
# Exactly one reason now appears, and it is the one that is NOT a finding: the
# section is genuinely read, but by a shared library rather than by src/, which
# the detector cannot see. The other former reason — "legacy/inert, knowingly
# kept" — is gone, because keeping a knob nothing reads IS the defect. Every
# section that carried it has been deleted and its env prefix retired.
#
# The value of the list is that it is CLOSED: a NEW unread section fails this
# test until someone wires it up, deletes it, or writes down which case it is.
#
# ⚠️ Do not add an entry here to silence a failure. If a section looks unread,
# first suspect the detector (see the module docstring — it has been wrong twice
# in this exact way) and check for an alias binding or a read outside src/.
SECTIONS_WITHOUT_A_READER_IN_SRC: dict[str, str] = {
    "auth": (
        "blind spot, not a dead knob: read by w2v_common's app factory "
        "(w2v_common/asgi/factory.py -> settings.auth.enabled), not by src/"
    ),
}

#: Prefixes `src/config/__init__.py` refuses at startup. Kept here as an
#: independent literal rather than imported, so that editing the production
#: table is a RED test rather than a silently agreeing one.
#:
#: ⚠️ An independent copy only guards anything if something compares the two —
#: `test_retired_prefix_table_matches_the_declared_list` is that comparison.
#: Without it, asserting `"SCANNER_" not in RETIRED_ENV_PREFIXES` reads like a
#: guard on the live SCANNER_ surface and is really a guard on this literal:
#: adding SCANNER_ to the production table left it green. (Found by mutation.)
RETIRED_ENV_PREFIXES = (
    "CRITIQUE_",
    "SCREENSHOT_",
    "DALLE_",
    "FLUX_",
    "CONTENT_GENERATION_",
    "BRAND_",
    "JOB_QUEUE_",
)


def _settings_sections() -> dict[str, str]:
    """Return {attribute_name: class_name} for every section field on `Settings`."""
    source = (CONFIG_DIR / "__init__.py").read_text()
    body = source.split("class Settings(BaseSettings):", 1)[1]
    return dict(re.findall(r"^\s{4}(\w+):\s*(\w+Settings|FeatureFlags)\s*=\s*Field", body, re.M))


def _dotted(node: ast.AST) -> str | None:
    """Flatten a pure Name/Attribute chain to `a.b.c`, else None."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    elif isinstance(cur, ast.Call):
        parts.append("()")  # e.g. get_settings().scoring.seo_weight
    else:
        return None
    return ".".join(reversed(parts))


def _is_module_path(dotted: str) -> bool:
    """`src.domains.brand.stages` is an import path, not a settings read."""
    return dotted.startswith(("src.", "domains.", "tests."))


def _direct_reads(tree: ast.AST) -> set[str]:
    """Shape 1 — `<obj>.<section>.<field>` written as one expression.

    The section name must not be the LAST component: `settings.dalle` on its own
    consults no knob, `settings.dalle.enabled` does.

    Known bias: the chain root is not checked, so a domain object that happens to
    carry an attribute named like a section (`self.cache.get(...)`) would count
    as a reader. That is deliberate — this detector's false NEGATIVE is the
    dangerous direction (it is what nearly got `playwright` deleted), so where
    the two trade off it errs toward reporting a read. On today's src/ the bias
    is inert: every such chain roots at `settings`, `get_settings()`, or `self`
    inside `Settings` itself.
    """
    read: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        dotted = _dotted(node)
        if dotted is None or _is_module_path(dotted):
            continue
        read.update(dotted.split(".")[1:-1])
    return read


def _alias_targets(tree: ast.AST) -> dict[str, str]:
    """Locals bound directly to a section: `pw = get_settings().playwright`."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not isinstance(value, ast.Attribute):
            continue
        dotted = _dotted(value)
        if dotted is None or _is_module_path(dotted):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = dotted.rsplit(".", 1)[-1]
    return aliases


def _alias_reads(tree: ast.AST) -> set[str]:
    """Shape 2 — a field read off a local that was bound to a section."""
    aliases = _alias_targets(tree)
    if not aliases:
        return set()
    return {
        aliases[node.value.id]
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
    }


def _sections_read(sources: Iterable[tuple[str, str]]) -> frozenset[str]:
    """Section names some expression reads a FIELD off, across `sources`.

    `sources` is (label, python_source) pairs so the analysis can be exercised
    on inline snippets — otherwise the only way to prove the detector works is
    to point it at src/ and trust the answer, which is how it shipped broken
    twice.

    Two shapes count as a read, and missing the second is what hid `playwright`:

        settings.scoring.decay_k          # direct: one expression
        pw = get_settings().playwright    # alias: bound to a local...
        pw.headless                       # ...then read off the local
    """
    read: set[str] = set()
    for _label, source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - src/ must parse
            continue
        read |= _direct_reads(tree)
        read |= _alias_reads(tree)
    return frozenset(read)


def _src_sources() -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(REPO_ROOT)), path.read_text())
        for path in SRC_DIR.rglob("*.py")
        if "migrations" not in path.parts
    ]


def _sections_read_in_src() -> frozenset[str]:
    return _sections_read(_src_sources())


# ---------------------------------------------------------------------------
# The detector's own controls. Everything below depends on these passing.
# ---------------------------------------------------------------------------

_DIRECT_SNIPPET = """
def run():
    settings = get_settings()
    return settings.scoring.decay_k
"""

_ALIAS_SNIPPET = """
async def fetch():
    pw_settings = get_settings().playwright
    browser = await pw.chromium.launch(headless=pw_settings.headless)
"""

_IMPORT_PATH_SNIPPET = """
from src.domains.critique.services import CritiqueService

def run():
    return src.domains.critique.services.thing
"""

_BOUND_BUT_UNREAD_SNIPPET = """
def run():
    section = get_settings().dalle
    return section
"""


def test_reader_detection_detects_a_direct_read() -> None:
    assert "scoring" in _sections_read([("<direct>", _DIRECT_SNIPPET)])


def test_reader_detection_detects_an_alias_bound_read() -> None:
    """Regression for vacuity #2 — the shape that hid `playwright`.

    Without this, the detector reports a live section as dead and the next
    audit deletes it.
    """
    assert "playwright" in _sections_read([("<alias>", _ALIAS_SNIPPET)])


def test_reader_detection_ignores_import_paths() -> None:
    """Regression for vacuity #1 — `src.domains.critique` is not a settings read."""
    assert "critique" not in _sections_read([("<import>", _IMPORT_PATH_SNIPPET)])


def test_reader_detection_does_not_fire_on_a_bare_binding() -> None:
    """False-positive control: binding a section without reading a FIELD off it.

    A detector that answered True for everything would pass every other
    assertion in this file. `section = settings.dalle; return section` never
    consults a knob, so it must not count as a reader.
    """
    assert "dalle" not in _sections_read([("<bound>", _BOUND_BUT_UNREAD_SNIPPET)])


def test_reader_detection_reports_nothing_for_an_invented_name() -> None:
    assert "definitely_not_a_real_section" not in _sections_read_in_src()


def test_reader_detection_finds_the_known_live_sections_in_src() -> None:
    """Non-vacuity against the real tree, not just snippets."""
    read = _sections_read_in_src()
    assert read, "parsed zero section reads out of src/ — the walker has rotted"
    for section in ("scoring", "scanners", "external_tools", "features", "playwright"):
        assert section in read, f"{section} is live in src/ but the detector missed it"


# ---------------------------------------------------------------------------
# The guard proper
# ---------------------------------------------------------------------------


def test_every_settings_section_has_a_reader_or_is_declared_unread() -> None:
    sections = _settings_sections()
    assert sections, "failed to parse any section off Settings — the regex has rotted"

    read = _sections_read_in_src()
    unread = {name for name in sections if name not in read}
    undeclared = unread - set(SECTIONS_WITHOUT_A_READER_IN_SRC)

    assert not undeclared, (
        f"Settings sections with no reader in src/: {sorted(undeclared)}.\n"
        "A settings section nothing reads is a knob that does nothing: an operator "
        "who sets its env vars gets silence, and may believe they disabled "
        "something they did not.\n"
        "Either wire it up, delete it, or add it to SECTIONS_WITHOUT_A_READER_IN_SRC "
        "with the reason."
    )

    # Keep the allowlist honest in the other direction too: once a section gains
    # a reader (or is deleted), it must leave the list, or the list stops meaning
    # anything.
    stale = set(SECTIONS_WITHOUT_A_READER_IN_SRC) - unread
    assert not stale, (
        f"SECTIONS_WITHOUT_A_READER_IN_SRC is stale: {sorted(stale)} now have readers "
        "or no longer exist. Remove them from the allowlist."
    )


@pytest.mark.parametrize(
    "section",
    [
        "critique",
        "screenshot",
        "dalle",
        "content_generation",
        "flux",
        "brand",
        "scanner",
        "job_queue",
    ],
)
def test_deleted_section_is_not_reintroduced(section: str) -> None:
    """Each of these was a control panel wired to nothing. `scanner` was a
    singular duplicate of the live `scanners` under the same SCANNER_ prefix.
    """
    assert section not in _settings_sections(), (
        f"`Settings.{section}` is back. It was deleted because nothing read it; "
        "re-adding it without a reader recreates a knob that configures nothing."
    )


@pytest.mark.parametrize(
    "symbol",
    [
        "CritiqueSettings",
        "ScreenshotSettings",
        "DalleSettings",
        "ContentGenerationSettings",
        "FluxSettings",
        "BrandSettings",
        "JobQueueSettings",
    ],
)
def test_deleted_settings_class_is_gone(symbol: str) -> None:
    import src.config as config

    assert not hasattr(config, symbol)
    assert symbol not in config.__all__


def test_retired_env_prefix_is_not_reintroduced() -> None:
    offenders = [
        (p.relative_to(REPO_ROOT).as_posix(), prefix)
        for p in CONFIG_DIR.rglob("*.py")
        for prefix in RETIRED_ENV_PREFIXES
        if re.search(rf'env_prefix\s*=\s*["\']{re.escape(prefix)}["\']', p.read_text())
    ]
    assert not offenders, (
        f"A retired env prefix was reintroduced as a live env_prefix: {offenders}."
    )


# ---------------------------------------------------------------------------
# `playwright` — the section this audit nearly deleted by mistake
# ---------------------------------------------------------------------------


def test_playwright_section_is_live_and_must_not_be_deleted() -> None:
    """`playwright` sat on the unread allowlist labelled "legacy/inert". It is not.

    Pinned to the actual reader so that removing the reader, or the section,
    fails here rather than in brand extraction at runtime.
    """
    assert "playwright" in _settings_sections()

    fetch = (SRC_DIR / "domains" / "brand" / "stages" / "fetch.py").read_text()
    assert "get_settings().playwright" in fetch, (
        "the brand fetch stage no longer reads settings.playwright — if that is "
        "deliberate, the section is now dead and should be deleted with its "
        "PLAYWRIGHT_ prefix retired, not left as another knob nothing reads"
    )
    assert "playwright" in _sections_read([("fetch.py", fetch)])


def test_retired_prefix_table_matches_the_declared_list() -> None:
    """The production table and this file's literal must agree, both ways.

    This is what makes the independent copy above mean anything: without it,
    every `"X_" not in RETIRED_ENV_PREFIXES` assertion in this file is a
    statement about the literal, not about what the service will refuse.
    """
    from src.config import _RETIRED_ENV_PREFIXES

    assert set(_RETIRED_ENV_PREFIXES) == set(RETIRED_ENV_PREFIXES), (
        "src/config/__init__.py's retired-prefix table drifted from the list this "
        "test file declares. Reconcile them deliberately — an addition can break a "
        "live surface, and a removal reopens a silently-ignored knob."
    )


@pytest.mark.parametrize("prefix", ["PLAYWRIGHT_", "SCANNER_", "SCORING_", "EXTERNAL_TOOL_"])
def test_live_env_prefix_is_not_in_the_production_retired_table(prefix: str) -> None:
    """Asserted against the PRODUCTION table, not this file's copy.

    `PLAYWRIGHT_` drives the brand pipeline's browser. `SCANNER_` is the live
    per-scanner timeout surface — the `scanner` singular alias that was deleted
    shared this exact prefix with `scanners`, so retiring it would have reset
    every timeout to its default while looking like tidy-up.
    """
    from src.config import _RETIRED_ENV_PREFIXES

    assert prefix not in _RETIRED_ENV_PREFIXES


def test_playwright_env_vars_still_bind() -> None:
    """PLAYWRIGHT_ must NOT be retired — the live browser knobs ride on it."""
    from src.config import PlaywrightSettings

    assert PlaywrightSettings(headless=False).headless is False


def test_scanner_settings_still_bind() -> None:
    """Deleting the `scanner` singular alias must not touch the SCANNER_ surface."""
    from src.config import ScannerSettings

    assert ScannerSettings(seo_timeout=77).seo_timeout == 77


# ---------------------------------------------------------------------------
# The trap: a retired var must be answered, not ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_var", "expected_replacement"),
    [
        # CRITIQUE_ knobs all moved somewhere real, so the message names the var.
        ("CRITIQUE_AXE_ENABLED", "EXTERNAL_TOOL_AXE_ENABLED"),
        ("CRITIQUE_LIGHTHOUSE_ENABLED", "EXTERNAL_TOOL_LIGHTHOUSE_ENABLED"),
        ("CRITIQUE_SEO_TIMEOUT", "SCANNER_SEO_TIMEOUT"),
        ("CRITIQUE_A11Y_WEIGHT", "SCORING_A11Y_WEIGHT"),
        ("CRITIQUE_BATCH_SCAN", "FEATURE_BATCH_SCAN"),
        # The rest have no replacement knob — the capability is elsewhere or
        # absent — so the message must name where it went instead.
        ("SCREENSHOT_ENABLED", "html_render_service"),
        ("DALLE_ENABLED", "ai_content_generation_service"),
        ("FLUX_ENABLED", "ai_content_generation_service"),
        ("CONTENT_GENERATION_ENABLED", "no ai_content_generation client"),
        ("BRAND_MAX_PAGES", "PLAYWRIGHT_"),
        # The queue knobs are the sharpest case: MAX_RETRIES read as a live
        # reliability control while the queue it tuned had no producer at all.
        ("JOB_QUEUE_MAX_RETRIES", "no producer"),
        ("JOB_QUEUE_MAX_CONCURRENCY", "_process_job_background"),
        ("JOB_QUEUE_ENABLED", "no producer"),
    ],
)
def test_retired_env_var_is_rejected_and_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, env_var: str, expected_replacement: str
) -> None:
    """The whole point: the operator must be told, not ignored."""
    from src.config import Settings

    monkeypatch.setenv(env_var, "false")
    with pytest.raises(ValueError, match=re.escape(expected_replacement)):
        Settings()


@pytest.mark.parametrize("prefix", RETIRED_ENV_PREFIXES)
def test_unknown_var_under_a_retired_prefix_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """A typo under a retired prefix is exactly as inert as the real name.

    Rejecting only the field names that used to exist would leave
    `DALLE_MDOEL` silently ignored — the same defect, one level down.
    """
    from src.config import Settings

    monkeypatch.setenv(f"{prefix}SOMETHING_INVENTED", "1")
    with pytest.raises(ValueError, match="configure NOTHING"):
        Settings()


def test_settings_still_construct_without_any_retired_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the tests above: the guard must not reject a clean environment.

    Without this, a validator that raised unconditionally would satisfy every
    rejection assertion in this file.
    """
    from src.config import Settings

    for key in list(os.environ):
        if key.upper().startswith(RETIRED_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    assert Settings() is not None


def test_live_env_prefixes_are_not_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dangerous mutation: retiring a prefix that is still wired.

    Each of these binds a section src/ actually reads. If one ever lands in the
    retired table, the service refuses to boot on its own live configuration.
    """
    from src.config import Settings

    monkeypatch.setenv("SCANNER_SEO_TIMEOUT", "31")
    monkeypatch.setenv("PLAYWRIGHT_VIEWPORT_WIDTH", "1441")
    monkeypatch.setenv("SCORING_SCORE_FLOOR", "1")
    monkeypatch.setenv("EXTERNAL_TOOL_AXE_ENABLED", "false")

    settings = Settings()
    assert settings.scanners.seo_timeout == 31
    assert settings.playwright.viewport_width == 1441
    assert settings.external_tools.axe_enabled is False


def test_retired_var_in_a_dotenv_file_is_also_rejected(tmp_path: Path) -> None:
    """`model_config` declares `env_file=".env"`, so a dotenv key binds too.

    Scanning only `os.environ` would leave half the trap open: a retired key
    written into `.env` would be read by pydantic-settings, ignored, and never
    reported.
    """
    from src.config import _dotenv_keys, _retired_env_offenders

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "# a comment\nDATABASE_URL=postgresql+psycopg://x/y\nDALLE_ENABLED=true\nexport FLUX_DEVICE=cpu\n"
    )
    keys = _dotenv_keys(str(dotenv))

    assert "DATABASE_URL" in keys, "the dotenv scan missed a perfectly ordinary key"
    assert _retired_env_offenders(keys) == ["DALLE_ENABLED", "FLUX_DEVICE"]


def test_dotenv_scan_tolerates_a_missing_file() -> None:
    """Control: no `.env` must mean no offenders, never an exception."""
    from src.config import _dotenv_keys

    assert _dotenv_keys("definitely-not-a-file-here.env") == {}


# Floor for the retired-prefix scan below. Measured 2026-08-19: `.github` 1 file,
# `scripts` 4, plus `docker-compose.yml` = 6. Lower it only with a new measurement.
_MIN_SCANNED_FILES = 5


def test_no_retired_env_var_is_set_anywhere_in_the_repo() -> None:
    """A manifest/compose/env file setting one of these would now crash the service.

    This is the half of the change that keeps deletion from becoming an outage:
    the repo's old `k8s/base/configmap.yaml` really did set
    CONTENT_GENERATION_ENABLED and _DEFAULT_PROVIDER for a section with zero
    readers. That tree was DELETED 2026-08-19 (nothing applied it — the live
    config is `the deployment manifests`), so `k8s` is
    gone from the scan list below. The scan still matters: `.github`, `scripts`
    and the compose/env files can each set a retired prefix, and any one of them
    crashes `Settings()` at import.

    Deliberately a pure-Python scan: a `git` subprocess inherits `GIT_DIR` /
    `GIT_INDEX_FILE` from the environment, which outranks `cwd=` and can silently
    target a different repository when the suite runs under a git hook.
    """
    pattern = re.compile(
        rf"^\s*-?\s*({'|'.join(re.escape(p) for p in RETIRED_ENV_PREFIXES)})[A-Z_]+\s*[:=]",
        re.M,
    )
    searched = 0
    offenders = []
    for rel in (".github", "scripts"):
        for path in (REPO_ROOT / rel).rglob("*"):
            if path.is_file() and path.suffix in {".yaml", ".yml", ".env", ".sh", ".toml"}:
                searched += 1
                if pattern.search(path.read_text(errors="ignore")):
                    offenders.append(path.relative_to(REPO_ROOT).as_posix())
    for name in ("docker-compose.yml", ".env", ".env.example"):
        candidate = REPO_ROOT / name
        if candidate.is_file():
            searched += 1
            if pattern.search(candidate.read_text(errors="ignore")):
                offenders.append(name)

    # Non-vacuity. `> 0` was too weak to do its job: it still passed with a SINGLE
    # file scanned, so a path list rotted down to one entry read as a clean bill of
    # health. Real population after the k8s deletion is 6 (.github 1, scripts 4,
    # compose/env 1); 5 leaves room for one file to move without pretending the
    # scan is still whole.
    assert searched >= _MIN_SCANNED_FILES, (
        f"scanned only {searched} file(s), expected >= {_MIN_SCANNED_FILES} — "
        "the path list has rotted, so the assertion below proves nothing"
    )
    assert not offenders, (
        f"Retired-prefix env vars are set in: {offenders}. "
        "They configure nothing and now fail startup."
    )
