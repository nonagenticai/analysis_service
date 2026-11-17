"""Alembic revision ids must fit ``alembic_version.version_num``.

Alembic creates that column as ``VARCHAR(32)``. A longer revision id does not
fail when the migration is written, nor when its DDL runs — it fails on the
*stamp* afterwards::

    sqlalchemy.exc.DataError: (psycopg.errors.StringDataRightTruncation)
    value too long for type character varying(32)
    [SQL: UPDATE alembic_version SET version_num='0012_critique_schedule_tenant_source' ...]

That is the worst possible shape: on a real database the schema change has
already been applied and committed, but the version row still points at the
previous revision — so the next ``upgrade head`` replays a migration whose DDL
is already in place and dies on ``DuplicateColumn``. Caught here (2026-08-27) by
CI rather than by a scratch-schema proof, because proving the DDL in isolation
never exercises alembic's own bookkeeping write.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# Alembic's own schema: alembic_version.version_num is VARCHAR(32).
MAX_REVISION_ID_LENGTH = 32

_VERSIONS_DIR = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "shared"
    / "database"
    / "migrations"
    / "versions"
)
_REVISION_RE = re.compile(
    r"""^revision(?::\s*str)?\s*=\s*["'](?P<id>[^"']+)["']""",
    re.MULTILINE,
)


def _revision_files() -> list[Path]:
    return sorted(p for p in _VERSIONS_DIR.glob("*.py") if p.name != "__init__.py")


def test_versions_directory_is_found() -> None:
    """Guard the guard: a wrong path would make every check below vacuous."""
    assert _VERSIONS_DIR.is_dir(), f"migrations dir not found at {_VERSIONS_DIR}"
    assert _revision_files(), f"no migration files under {_VERSIONS_DIR}"


@pytest.mark.parametrize("path", _revision_files(), ids=lambda p: p.stem)
def test_revision_id_fits_alembic_version_column(path: Path) -> None:
    match = _REVISION_RE.search(path.read_text(encoding="utf-8"))
    assert match is not None, f'{path.name}: no `revision = "..."` assignment found'

    revision_id = match.group("id")
    assert len(revision_id) <= MAX_REVISION_ID_LENGTH, (
        f"{path.name}: revision id {revision_id!r} is {len(revision_id)} chars, "
        f"over the {MAX_REVISION_ID_LENGTH}-char alembic_version.version_num limit. "
        "The DDL would apply and then the stamp would fail, leaving the database "
        "migrated but unstamped."
    )
