"""Guard: every SECURITY DEFINER resolver must be covered by an EXECUTE grant.

This test exists because the same defect shipped twice. A migration creates an
owner-owned SECURITY DEFINER resolver and revokes EXECUTE from ``PUBLIC``; nothing
grants it to the restricted runtime role ``w2v_analysis_app``; the background loop
that calls it then fails with ``InsufficientPrivilege`` on every tick, forever,
while the pod stays Ready and nothing alerts. It happened to
``claim_due_critique_schedules`` (13,523 errors over 9.4 days) and then to
``reap_stale_critique_jobs`` (~91/day on both replicas, never once succeeded).

Adding a resolver without adding its signature to ``RESOLVER_SIGNATURES`` now fails
here instead of in production.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from src.shared.database.resolver_grants import (
    APP_ROLE,
    RESOLVER_SIGNATURES,
    ROLE_EXISTS_PREDICATE,
    guarded_grant_statement,
)

_SRC = Path(__file__).resolve().parents[3] / "src"

#: This module declares the grants; it quotes ``SECURITY DEFINER`` in prose only.
_SELF = "resolver_grants.py"

_SECDEF_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:public\.)?(?P<name>\w+)\s*\(",
    re.IGNORECASE,
)


def _declared_names() -> set[str]:
    return {sig.split("(", 1)[0].strip() for sig in RESOLVER_SIGNATURES}


def _load(path: Path) -> object:
    """Import a source file by path — migration modules are not importable by name."""
    name = "_secdef_scan_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _defined_secdef_names() -> set[str]:
    """Names of SECURITY DEFINER functions, read from the RENDERED DDL constants.

    The DDL is built with ``str.replace`` on ``__FN__`` placeholders, so a scan of the
    raw source text would see the placeholder and not the function name. Importing the
    module and reading its string constants sees exactly the SQL that reaches Postgres.
    """
    found: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == _SELF or "SECURITY DEFINER" not in path.read_text(encoding="utf-8"):
            continue
        module = _load(path)
        for value in vars(module).values():
            if not isinstance(value, str) or "SECURITY DEFINER" not in value:
                continue
            for chunk in re.split(r"(?i)CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION", value)[1:]:
                head = "CREATE FUNCTION" + chunk
                if not re.search(r"(?i)SECURITY\s+DEFINER", head.split("$fn$", 1)[0]):
                    continue
                match = _SECDEF_RE.match(head)
                if not match:
                    continue
                name = match.group("name").lower()
                # ``__fn__`` is the unrendered placeholder in the DDL *template* that
                # sits beside the rendered constant in the same module. Skipping it
                # loses no coverage: the rendered constant is scanned too.
                if name.startswith("__") and name.endswith("__"):
                    continue
                found.add(name)
    return found


def test_every_security_definer_resolver_is_declared_for_grant() -> None:
    """No SECURITY DEFINER function may exist in src/ without a declared EXECUTE grant."""
    defined = _defined_secdef_names()
    assert defined, "regex found no SECURITY DEFINER functions — the scanner itself is broken"
    missing = defined - _declared_names()
    assert not missing, (
        f"SECURITY DEFINER resolver(s) {sorted(missing)} are not in RESOLVER_SIGNATURES, so "
        f"{APP_ROLE} will get 'permission denied for function' on every call. Add the "
        "signature and a migration running guarded_grant_statement()."
    )


def test_declared_signatures_all_exist_in_src() -> None:
    """The inverse: a stale signature would grant on a function that no longer exists."""
    stale = _declared_names() - _defined_secdef_names()
    assert not stale, f"RESOLVER_SIGNATURES lists function(s) not defined in src/: {sorted(stale)}"


def test_grant_statement_is_role_guarded_and_covers_every_resolver() -> None:
    """CI has no ``w2v_analysis_app`` role — an unguarded GRANT would abort the migration."""
    stmt = guarded_grant_statement()
    assert ROLE_EXISTS_PREDICATE + "'" + APP_ROLE + "'" in stmt
    for sig in RESOLVER_SIGNATURES:
        assert f"GRANT EXECUTE ON FUNCTION {sig} TO {APP_ROLE};" in stmt
