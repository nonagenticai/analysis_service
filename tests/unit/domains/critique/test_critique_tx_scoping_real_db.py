"""Real-DB regression test for critique transaction scoping.

``_phase_score_and_summarize`` reads active suppression rules (opening a read
transaction) and then makes the summary LLM call. Without releasing the
connection in between, the session sits idle-in-transaction across that call —
the same reap-the-backend hazard fixed in the redesign generation path. This
asserts the connection is not held while the summary is generated.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.domains.critique.models import CritiqueJob
from src.domains.critique.services.critique_service import CritiqueService


@pytest.mark.asyncio
async def test_score_and_summarize_releases_connection_before_summary(db_session) -> None:
    svc = CritiqueService()
    job = CritiqueJob(url="https://example.com", domain="example.com", max_pages=1)

    in_tx_during_summary: list[bool] = []

    async def fake_summary(
        _active: object, _url: object, _scores: object, *, llm_config: object = None
    ) -> tuple[str, None]:
        in_tx_during_summary.append(db_session.in_transaction())
        return "summary", None

    with patch(
        "src.domains.critique.services.critique_service.generate_summary",
        side_effect=fake_summary,
    ):
        await svc._phase_score_and_summarize(
            db_session,
            job,
            "7eed672d-a7ff-5f16-87d7-b6a493fee523",
            [],
            start_time=0.0,
            github_repo=None,
            llm_config=None,
        )

    assert in_tx_during_summary == [False], (
        "a DB transaction was held open across the summary LLM call"
    )
