"""A cancelled brand extraction must not leave a Chromium alive.

`fetch.py`'s nested `finally` blocks closed page, context and browser with BARE
awaits. Every one is a cancellation point, and the `finally` is reached ON the
cancellation path — a stage timeout (`wait_for`) or a run cancel. A cancel
delivered at `page.close()` abandoned `context.close()` AND `browser.close()`,
leaving a headless Chromium alive on the pod while `browser_slot()` accounting
reported the slot free. Slots then leak one per cancelled extraction until
nothing can acquire one.
"""

from __future__ import annotations

import asyncio

import pytest

from src.domains.brand.stages.fetch import _close_quietly


class _Recorder:
    def __init__(self, name: str, log: list[str]) -> None:
        self._name = name
        self._log = log

    async def close(self) -> None:
        await asyncio.sleep(0)  # a real cancellation point
        self._log.append(self._name)


class _Exploder:
    async def close(self) -> None:
        raise RuntimeError("browser already gone")


@pytest.mark.asyncio
async def test_every_closeable_is_closed_despite_a_cancel() -> None:
    log: list[str] = []
    page, context, browser = (_Recorder(n, log) for n in ("page", "context", "browser"))

    async def _teardown() -> None:
        await asyncio.shield(_close_quietly(page, context, browser))

    task = asyncio.create_task(_teardown())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(6):
        await asyncio.sleep(0)

    assert log == ["page", "context", "browser"], (
        f"teardown was abandoned mid-way: {log}. A surviving Chromium leaks a "
        f"browser_slot that accounting reports as free."
    )


@pytest.mark.asyncio
async def test_a_teardown_error_does_not_mask_the_real_failure() -> None:
    """This runs in a `finally` under the real exception — it must not raise."""
    log: list[str] = []
    ok = _Recorder("browser", log)

    raised: BaseException | None = None
    try:
        await _close_quietly(_Exploder(), ok)
    except BaseException as exc:  # noqa: BLE001 — the assertion IS "nothing escapes"
        raised = exc

    assert raised is None, f"teardown raised {raised!r} and would replace the run's real cause"
    assert log == ["browser"], "a failing close must not stop the remaining closes"
