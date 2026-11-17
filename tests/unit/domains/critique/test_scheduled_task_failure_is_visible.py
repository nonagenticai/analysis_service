"""A scheduled scan that raises must not vanish.

`_dispatch_job_processing._process` had `try/finally` with **no `except`**. The
task it wraps is referenced only by
`task.add_done_callback(_scheduled_tasks.discard)`, so nothing awaits it and
nothing retrieves its exception. A cron-fired scan that raised produced:

* no log line,
* no metric,
* a job row left `running` forever.

The HTTP path for the identical work has always had a handler
(`routes/_helpers.py`), so this was a gap between two doors into one room.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from w2v_common.tenant import TenantSource

from src.domains.critique.services.scheduler import _dispatch_job_processing


async def _drive(side_effect: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    service = MagicMock()
    service.process_job = AsyncMock(side_effect=side_effect)

    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.shared.database.session.get_db_session", return_value=db),
        patch("src.domains.critique.services.scheduler.bind_session_tenant", new=AsyncMock()),
        caplog.at_level(logging.ERROR),
    ):
        await _dispatch_job_processing(
            service,
            "job-1",
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            TenantSource.LICENSE_TOKEN,
        )
        # The task is fire-and-forget; give it a turn to run and finish.
        for _ in range(10):
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_raising_scheduled_job_is_logged_not_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _drive(RuntimeError("scanner exploded"), caplog)

    assert any("FAILED" in r.message for r in caplog.records), (
        "a scheduled scan that raised produced NO log at all — the exception was "
        "never retrieved because nothing awaits the task"
    )
    assert any(r.exc_info for r in caplog.records), (
        "logger.exception, not .error — the traceback is the only place the cause "
        "could ever appear on this path"
    )


@pytest.mark.asyncio
async def test_a_db_error_is_caught_too(caplog: pytest.LogCaptureFixture) -> None:
    """`SQLAlchemyError` subclasses none of (OSError, RuntimeError, ValueError).

    A narrow tuple here would look correct and still let a pgbouncer blip vanish.
    """
    from sqlalchemy.exc import OperationalError

    await _drive(OperationalError("SELECT 1", {}, Exception("pgbouncer went away")), caplog)

    assert any("FAILED" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_cancellation_still_propagates(caplog: pytest.LogCaptureFixture) -> None:
    """Shutdown is not a failure. Swallowing `CancelledError` breaks drain."""
    service = MagicMock()
    service.process_job = AsyncMock(side_effect=asyncio.CancelledError())

    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("src.shared.database.session.get_db_session", return_value=db),
        patch("src.domains.critique.services.scheduler.bind_session_tenant", new=AsyncMock()),
        caplog.at_level(logging.ERROR),
    ):
        await _dispatch_job_processing(
            service,
            "job-2",
            "2e960681-0187-5dad-ba0a-2b95a67ecfaa",
            TenantSource.LICENSE_TOKEN,
        )
        for _ in range(10):
            await asyncio.sleep(0)

    assert not any("FAILED" in r.message for r in caplog.records), (
        "a cancelled task must not be reported as a job failure"
    )
