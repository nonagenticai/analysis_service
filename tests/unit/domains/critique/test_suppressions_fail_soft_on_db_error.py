"""Pins that a database failure while loading suppressions keeps the findings.

``_apply_suppressions`` annotates findings with ``is_suppressed``; it does not
produce them. A failure to load the suppression rules is therefore a reason to
leave every finding unsuppressed, never a reason to lose the findings -- which
is exactly what happened while the handler named only builtins: nothing a real
database raises derives from ``OSError``/``ValueError``/``RuntimeError``/
``TimeoutError``, so the error escaped the "fail-soft" guard entirely and took
the caller's whole finding list with it.

``db`` is an ordinary parameter, so a stub whose ``execute`` raises is the whole
fixture -- no database, no event loop beyond pytest-asyncio, no load.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.services.critique_service.helpers import CritiqueHelpersMixin


class _ExplodingSession:
    """An ``AsyncSession`` stand-in whose every query fails.

    ``OperationalError`` is the realistic shape: a dropped connection or a
    pgbouncer reset mid-statement. It derives from ``SQLAlchemyError`` and from
    none of the builtins the old handler named.
    """

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        """Fail the way a dropped connection does.

        Raises:
            OperationalError: Always.
        """
        raise OperationalError("SELECT findingsuppression", {}, Exception("connection reset"))


@pytest.mark.asyncio
async def test_findings_survive_a_database_failure_loading_suppressions() -> None:
    """The findings come back, marked unsuppressed, rather than being lost."""
    findings = [
        {"check_id": "sec-exposed-env", "severity": "critical", "title": "Exposed .env"},
        {"check_id": "sec-missing-hsts", "severity": "medium", "title": "No HSTS"},
    ]

    result = await CritiqueHelpersMixin()._apply_suppressions(
        cast(AsyncSession, _ExplodingSession()),
        findings,
        tenant_id="fr-test-user",
        domain="example.com",
    )

    assert [f["check_id"] for f in result] == ["sec-exposed-env", "sec-missing-hsts"], (
        f"findings were lost when the suppression query failed; got {result}"
    )
    assert all(f["is_suppressed"] is False for f in result), (
        "a finding must not be reported as suppressed when the suppression rules "
        f"could not be read; got {result}"
    )
