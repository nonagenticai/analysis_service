"""Guards for ``0013_tenant_id_uuid`` — the varchar → uuid flip of all 20 tenant columns.

Every property pinned here shipped GREEN somewhere in the fleet while being wrong,
or was demonstrated red-then-green on a throwaway postgres 18 while writing 0013:

1. ``as_uuid=False`` on every ``tenant_id`` column. ``as_uuid=True`` returns
   ``uuid.UUID`` objects and ``uuid.UUID(t) == t`` is False for every string
   ``t`` — ownership guards would 404 the owner on their own rows and scope
   predicates would match nothing, with ty and ruff staying green because
   ``UUID == str`` is not a type error. It is also why
   ``set_config('app.current_tenant', $2, true)`` still receives a ``str``:
   that function is ``(text, text, bool)`` and a uuid raises ``UndefinedFunction``.
2. The historical RLS migrations must render the predicate they were WRITTEN for.
   ``0005_rls`` used to read ``col.type`` off the live model, so the moment the
   models declared ``PgUUID`` a FRESH ``alembic upgrade head`` died at 0005 with
   ``operator does not exist: character varying = uuid`` — while an
   already-migrated database was completely unaffected. Reproduced, then fixed,
   then mutation-checked (pin removed → red, restored → green).
3. Both policy SHAPES survive the flip: 19 strict tables and the one nullable
   ``audit_logs`` with its 4-policy global-or-own set.
4. The request boundary rejects a non-uuid tenant with a 4xx. Once the column is
   ``uuid``, a legacy slug no longer degrades to "0 rows / 404": Postgres raises
   ``InvalidTextRepresentation`` and the caller gets a 500.
"""

from __future__ import annotations

import importlib
import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import Column, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID  # noqa: N811
from w2v_common.database.rls import TENANT_COLUMN

from src.domains.analyze import models as _analyze_models  # noqa: F401
from src.domains.analyze import review_job_models as _analyze_review_models  # noqa: F401
from src.domains.brand import models as _brand_models  # noqa: F401
from src.domains.critique import models as _critique_models  # noqa: F401
from src.domains.critique.routes._helpers import _get_tenant_id as critique_get_tenant_id
from src.domains.icp import models as _icp_models  # noqa: F401
from src.domains.review import models as _review_models  # noqa: F401
from src.domains.review.routes import _get_tenant_id as review_get_tenant_id
from src.domains.scorecards.http import tenant_id_from_request
from src.shared.database.base import Base

# Migration module names start with a digit, so they are only importable this way
# (the same shape ``test_migration_0009_not_null.py`` uses).
_M0005 = importlib.import_module("src.shared.database.migrations.versions.0005_rls")
_M0013 = importlib.import_module("src.shared.database.migrations.versions.0013_tenant_id_uuid")

#: The count is itself the assertion: 20 tenant-scoped tables, measured live on
#: the prod primary 2026-08-27. A 21st appearing without a policy is the failure
#: mode 0008 was written to end.
EXPECTED_TENANT_TABLE_COUNT = 20

#: The one table whose ``tenant_id`` is NULLABLE → global-or-own policy set.
NULLABLE_TENANT_TABLE = "audit_logs"


def _tenant_tables() -> dict[str, Column]:
    return {
        t.name: t.columns[TENANT_COLUMN]
        for t in Base.metadata.sorted_tables
        if TENANT_COLUMN in t.columns
    }


class TestEveryTenantColumnIsUuidAsStr:
    def test_all_twenty_tables_are_present(self) -> None:
        cols = _tenant_tables()
        assert len(cols) == EXPECTED_TENANT_TABLE_COUNT, (
            f"expected {EXPECTED_TENANT_TABLE_COUNT} tenant-scoped tables, got "
            f"{sorted(cols)} — a new one needs a policy AND a place in 0013's "
            f"TENANT_TABLES, or it silently sits outside RLS"
        )

    def test_every_tenant_column_is_postgres_uuid(self) -> None:
        for name, col in _tenant_tables().items():
            assert isinstance(col.type, PgUUID), (
                f"{name}.tenant_id must be a native uuid column (migration 0013), got {col.type!r}"
            )

    def test_every_tenant_column_uses_as_uuid_false(self) -> None:
        """THE load-bearing flag. See the module docstring — with ``as_uuid=True``
        every ``row.tenant_id == some_str`` comparison in this repo silently
        becomes False and ty/ruff stay green while production breaks."""
        for name, col in _tenant_tables().items():
            coltype = cast(PgUUID, col.type)
            assert coltype.as_uuid is False, (
                f"{name}.tenant_id must be PgUUID(as_uuid=False): as_uuid=True "
                f"hands back uuid.UUID objects, and uuid.UUID(t) == t is False for "
                f"every string t — every ownership guard in this repo would 404 "
                f"the owner on their own rows, and set_config('app.current_tenant', "
                f"$2, true) would raise UndefinedFunction"
            )

    def test_a_uuid_object_never_equals_its_own_string(self) -> None:
        """The property the flag exists for, demonstrated rather than asserted in
        a comment — so a future reader cannot re-derive 'as_uuid is just style'."""
        s = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"
        assert uuid.UUID(s) != s
        assert str(uuid.UUID(s)) == s

    def test_audit_logs_stays_nullable_and_the_rest_do_not(self) -> None:
        """Nullability selects the POLICY SHAPE in w2v_common.database.rls.
        ``ALTER COLUMN ... TYPE`` preserves it; if a future edit flips it, the
        global-or-own read escape appears or vanishes silently."""
        for name, col in _tenant_tables().items():
            expected = name == NULLABLE_TENANT_TABLE
            assert bool(col.nullable) is expected, (
                f"{name}.tenant_id nullable={col.nullable!r}; only "
                f"{NULLABLE_TENANT_TABLE} may be nullable (4-policy global-or-own set)"
            )


class TestMigration0013RendersBothPolicyShapes:
    def test_revision_id_matches_module_and_fits_version_num(self) -> None:
        # ``alembic_version.version_num`` is varchar(32): an over-long id applies
        # the DDL and THEN fails the stamp, leaving a half-applied migration.
        assert _M0013.revision == "0013_tenant_id_uuid"
        assert len(_M0013.revision) <= 32
        assert _M0013.down_revision == "0012_schedule_tenant_source"

    def test_table_lists_match_the_models(self) -> None:
        assert set(_M0013.TENANT_TABLES) == set(_tenant_tables())
        assert _M0013.TENANT_TABLES_NULLABLE == (NULLABLE_TENANT_TABLE,)
        assert len(_M0013.TENANT_TABLES) == EXPECTED_TENANT_TABLE_COUNT

    def test_both_legacy_values_map_to_a_real_registered_tenant(self) -> None:
        """No sentinel, no nil uuid: every mapped target must be a real tenant.

        The nil uuid is the removed ``default-anon`` sentinel and is rejected by
        value in ``src.shared.tenant``; parking rows under it would make them
        readable by whoever holds it.
        """
        assert set(_M0013.LEGACY_TENANT_MAP) == {"n8n-dev", "anonymous"}
        for name, target in _M0013.LEGACY_TENANT_MAP.items():
            parsed = uuid.UUID(target)
            assert parsed.int != 0, f"{name} must not map to the nil sentinel"
            assert str(parsed) == target

    def test_uuid_metadata_renders_a_uuid_cast_for_both_shapes(self) -> None:
        from w2v_common.database.rls import upgrade_statements

        stmts = upgrade_statements(_M0013._scoped_metadata(as_uuid=True))
        creates = [s for s in stmts if s.startswith("CREATE POLICY")]
        # 19 strict tables x 1 policy + audit_logs x 4 = 23, the live count.
        assert len(creates) == 23, creates
        for s in creates:
            assert "::uuid" in s, (
                f"a policy without the ::uuid GUC cast returns ZERO rows against a "
                f"uuid column — silently, not as an error: {s}"
            )
        assert any(
            s.startswith(f"CREATE POLICY tenant_isolation_read ON {NULLABLE_TENANT_TABLE}")
            and "tenant_id IS NULL OR" in s
            for s in creates
        ), "audit_logs must keep its global-or-own read policy"

    def test_varchar_metadata_renders_the_text_predicate(self) -> None:
        """What ``downgrade()`` restores, and what 0005-0009 must keep emitting."""
        from w2v_common.database.rls import upgrade_statements

        stmts = upgrade_statements(_M0013._scoped_metadata(as_uuid=False))
        creates = [s for s in stmts if s.startswith("CREATE POLICY")]
        assert len(creates) == 23
        assert not any("::uuid" in s for s in creates)


class TestHistoricalMigrationsStayPinnedToVarchar:
    """REGRESSION GUARD for a defect that ships GREEN on an existing database.

    0005 rendered its RLS DDL from live ``Base.metadata``, so flipping the models
    to ``PgUUID`` made THAT revision emit a ``::uuid`` predicate against columns
    that stay ``varchar`` for another eight revisions. A fresh
    ``alembic upgrade head`` died at 0005 with ``operator does not exist:
    character varying = uuid``; an already-migrated database saw nothing. Only a
    from-scratch bootstrap can catch this class, so it is pinned here too.
    """

    def test_0005_renders_text_predicates_only(self) -> None:
        from w2v_common.database.rls import upgrade_statements

        stmts = upgrade_statements(_M0005._metadata_as_of_0005())
        assert stmts, "0005 must still render policies"
        offenders = [s for s in stmts if "::uuid" in s]
        assert not offenders, (
            "0005_rls must emit the varchar/text predicate it was written for. "
            "A ::uuid cast here breaks every fresh `alembic upgrade head` at 0005 "
            f"while leaving migrated databases untouched: {offenders}"
        )

    def test_0005_tenant_type_is_pinned_not_derived(self) -> None:
        meta = _M0005._metadata_as_of_0005()
        for table in meta.sorted_tables:
            col = table.columns[TENANT_COLUMN]
            assert isinstance(col.type, String), (
                f"0005 must describe {table.name}.tenant_id as varchar (its type at "
                f"that revision), not as whatever models.py says today"
            )


class TestNonUuidTenantIsAFourXxNotAFiveHundred:
    """Once ``tenant_id`` is ``uuid``, a legacy slug stops being a 404 and becomes
    a 500 (``InvalidTextRepresentation: invalid input syntax for type uuid``).
    All three previously-unvalidated boundaries now reject it at the door.

    The status is 401, not 400, and that is this repo's EXISTING convention, not
    an invention: ``request.state.tenant_id`` is written exclusively by
    ``AuthMiddleware`` from a verified license token
    (``X-Tenant-Id: Bearer <license>``), so a non-uuid value there means the
    requestor was never correctly identified. ``require_request_tenant`` — used by
    five other call sites in this service — already answers 401 for exactly this
    condition; see its docstring.
    """

    BOUNDARIES = (
        ("review", review_get_tenant_id),
        ("critique", critique_get_tenant_id),
        ("scorecards", tenant_id_from_request),
    )

    @pytest.mark.parametrize(("domain", "fn"), BOUNDARIES)
    @pytest.mark.parametrize("slug", ["n8n-dev", "anonymous", "default", "not-a-uuid"])
    def test_a_legacy_slug_is_refused_with_4xx(self, domain: str, fn, slug: str) -> None:
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=slug))
        with pytest.raises(HTTPException) as exc:
            fn(cast(Request, request))
        assert 400 <= exc.value.status_code < 500, (
            f"{domain}: a non-uuid tenant must be a client error, never a 500 — "
            f"got {exc.value.status_code}"
        )
        assert exc.value.status_code != 404, (
            f"{domain}: 404 would restore the pre-0013 'silently no rows' behaviour"
        )
        assert exc.value.status_code == 401, (
            f"{domain}: must match require_request_tenant's established 401"
        )

    @pytest.mark.parametrize(("domain", "fn"), BOUNDARIES)
    def test_absence_is_refused_not_substituted(self, domain: str, fn) -> None:
        """``review`` and ``critique`` both used to return the literal
        ``"anonymous"`` here — one of exactly two dirty values 0013 had to
        remediate out of the live tables."""
        request = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(HTTPException) as exc:
            fn(cast(Request, request))
        assert exc.value.status_code == 401

    @pytest.mark.parametrize(("domain", "fn"), BOUNDARIES)
    def test_a_real_tenant_passes_through_as_str(self, domain: str, fn) -> None:
        """Must stay ``str``: ``set_config('app.current_tenant', $2, true)`` is
        ``(text, text, bool)`` and a ``uuid.UUID`` raises ``UndefinedFunction``,
        killing every transaction under RLS."""
        real = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"
        request = SimpleNamespace(state=SimpleNamespace(tenant_id=real))
        got = fn(cast(Request, request))
        assert got == real
        assert isinstance(got, str)
        assert not isinstance(got, uuid.UUID)


class TestClaimResolverDeclaresUuid:
    def test_the_scheduler_ddl_returns_tenant_id_uuid(self) -> None:
        """``ALTER COLUMN TYPE`` does not revalidate a function body, so the
        resolver's declared type must be moved deliberately. 0013 DROPs and
        re-creates it (``CREATE OR REPLACE`` cannot change a return type)."""
        from src.domains.critique.services.scheduler import CLAIM_DUE_SCHEDULES_DDL

        assert "tenant_id uuid" in CLAIM_DUE_SCHEDULES_DDL
        assert "tenant_id varchar" not in CLAIM_DUE_SCHEDULES_DDL

    def test_the_claim_query_casts_back_to_text(self) -> None:
        """The one raw-SQL site that SELECTs ``tenant_id``. Without the cast
        psycopg hands back a ``uuid.UUID`` and every downstream consumer
        (``create_job``, the log fields, ``set_config``) receives the wrong type."""
        from src.domains.critique.services import scheduler

        assert "tenant_id::text AS tenant_id" in str(scheduler._CLAIM_SQL)
