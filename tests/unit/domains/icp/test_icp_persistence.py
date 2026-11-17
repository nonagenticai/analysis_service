"""Persistence tests for the ICP audit table.

The model round-trip and the RLS-policy assertion need a live database (they use
the ``db_session`` fixture and run in CI). The best-effort swallow test is pure
unit (no DB).
"""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import select, text

from src.domains.icp.models import IcpExtraction
from src.domains.icp.routes import _persist_icp
from src.domains.icp.schemas import ICPResponse


class TestIcpExtractionModel:
    async def test_roundtrip(self, db_session) -> None:
        row = IcpExtraction(
            tenant_id="ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
            domain="merqi.nl",
            url="https://merqi.nl",
            response={"company": "Merqi", "value_props": ["fast"]},
            confidence=0.18,
        )
        db_session.add(row)
        await db_session.commit()

        fetched = (
            await db_session.execute(
                select(IcpExtraction).where(
                    IcpExtraction.tenant_id == "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d"
                )
            )
        ).scalar_one()
        assert fetched.domain == "merqi.nl"
        assert fetched.response["company"] == "Merqi"
        assert fetched.confidence == 0.18
        assert fetched.created_at is not None


class TestIcpRlsPolicy:
    async def test_tenant_isolation_policy_present(self, db_session) -> None:
        # register_rls_ddl (create_all path in conftest) applies the standard
        # tenant_isolation policy to every tenant_id table, including this one.
        policies = (
            (
                await db_session.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = 'icp_extractions'")
                )
            )
            .scalars()
            .all()
        )
        assert "tenant_isolation" in policies


class TestPersistBestEffort:
    async def test_swallows_db_failure(self) -> None:
        # A DB failure in the best-effort audit write must never propagate.
        with patch("src.domains.icp.routes.get_db_session", side_effect=RuntimeError("db down")):
            await _persist_icp(
                "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d",
                "https://merqi.nl",
                ICPResponse(company="Merqi"),
            )
        # No exception == pass.
