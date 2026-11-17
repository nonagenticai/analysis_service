"""A scanner subprocess that times out must be reaped, not orphaned.

``asyncio.wait_for(proc.communicate(), timeout=...)`` cancels the *await*; it
does not touch the child. Lighthouse launches a Chromium of its own and nuclei
forks workers, so every slow target used to leave a whole process tree behind
until the pod hit its memory limit and was OOMKilled.

Killing only ``proc.pid`` is not enough — that reaps the ``lighthouse`` node
process and leaves its Chromium children. The child must therefore be spawned
into its own session (``start_new_session=True``) and the whole *process group*
signalled. The session flag is asserted at each production spawn site: without
it, ``os.killpg`` would target this service's own group.
"""

from __future__ import annotations

import asyncio
import signal
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter import (
    LighthouseAdapter,
)
from src.domains.critique.services.scanners.tool_adapters.nuclei_adapter import NucleiAdapter
from src.shared.security.external_process_target import (
    ExternalScanTarget,
    terminate_process_group,
)

CHILD_PID = 424242
CHILD_PGID = 424242
OUR_PGID = 999


class _HangingProc:
    """A child whose ``communicate()`` never returns."""

    def __init__(self, *, wait_hangs: bool = False) -> None:
        self.pid = CHILD_PID
        self.returncode: int | None = None
        self.reaped = False
        self._wait_hangs = wait_hangs

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    async def wait(self) -> int:
        if self._wait_hangs:
            self._wait_hangs = False  # only stall the SIGTERM grace window
            await asyncio.sleep(30)
        self.reaped = True
        self.returncode = -9
        return -9

    def kill(self) -> None:  # pragma: no cover - fallback path only
        self.returncode = -9

    def terminate(self) -> None:  # pragma: no cover - fallback path only
        self.returncode = -15


def _patched_pgid(pid: int) -> int:
    return CHILD_PGID if pid == CHILD_PID else OUR_PGID


class _Target:
    """Stand-in for a resolved, validated scan target."""

    @staticmethod
    def make() -> ExternalScanTarget:
        return ExternalScanTarget(
            url="http://example.com/",
            hostname="example.com",
            pinned_ip="93.184.216.34",
        )


async def _run_with_hanging_child(runner, settings_patch: str, timeout_attr: str):
    """Drive an adapter whose spawned child hangs; return (proc, killpg calls)."""
    proc = _HangingProc()
    killpg_calls: list[tuple[int, int]] = []

    def _killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    spawn = AsyncMock(return_value=proc)

    with (
        patch(f"{settings_patch}.get_settings") as settings,
        patch(
            f"{settings_patch}.resolve_external_scan_target",
            AsyncMock(return_value=_Target.make()),
        ),
        patch("asyncio.create_subprocess_exec", spawn),
        patch("os.getpgid", _patched_pgid),
        patch("os.killpg", _killpg),
        patch("os.getpgrp", lambda: OUR_PGID),
    ):
        tools = settings.return_value.external_tools
        setattr(tools, timeout_attr, 0.05)
        tools.lighthouse_cli_path = "lighthouse"
        tools.nuclei_path = "nuclei"
        result = await runner()

    assert result == []
    return proc, killpg_calls, spawn


class TestLighthouseReaping:
    async def test_timeout_kills_the_child_process_group(self) -> None:
        adapter = LighthouseAdapter()
        proc, killpg_calls, _spawn = await _run_with_hanging_child(
            lambda: adapter._run_lighthouse("http://example.com/", "mobile"),
            "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter",
            "lighthouse_timeout",
        )

        assert killpg_calls, "timed-out lighthouse left its Chromium tree running"
        assert all(pgid == CHILD_PGID for pgid, _ in killpg_calls)
        assert proc.reaped, "the child was signalled but never awaited (zombie)"

    async def test_child_is_spawned_into_its_own_session(self) -> None:
        """Without this, ``killpg`` would signal analysis_service itself."""
        adapter = LighthouseAdapter()
        _proc, _calls, spawn = await _run_with_hanging_child(
            lambda: adapter._run_lighthouse("http://example.com/", "mobile"),
            "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter",
            "lighthouse_timeout",
        )

        assert spawn.await_args.kwargs.get("start_new_session") is True


class TestNucleiReaping:
    async def test_timeout_kills_the_child_process_group(self) -> None:
        adapter = NucleiAdapter()
        proc, killpg_calls, _spawn = await _run_with_hanging_child(
            lambda: adapter.scan("http://example.com/"),
            "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter",
            "nuclei_timeout",
        )

        assert killpg_calls, "timed-out nuclei left its worker tree running"
        assert proc.reaped

    async def test_child_is_spawned_into_its_own_session(self) -> None:
        adapter = NucleiAdapter()
        _proc, _calls, spawn = await _run_with_hanging_child(
            lambda: adapter.scan("http://example.com/"),
            "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter",
            "nuclei_timeout",
        )

        assert spawn.await_args.kwargs.get("start_new_session") is True


class TestTerminateProcessGroup:
    async def test_escalates_to_sigkill_when_sigterm_is_ignored(self) -> None:
        proc = _HangingProc(wait_hangs=True)
        sent: list[tuple[int, int]] = []

        with (
            patch("os.getpgid", _patched_pgid),
            patch("os.killpg", lambda pgid, sig: sent.append((pgid, sig))),
            patch("os.getpgrp", lambda: OUR_PGID),
        ):
            await terminate_process_group(cast(asyncio.subprocess.Process, proc), grace=0.05)

        assert (CHILD_PGID, signal.SIGTERM) in sent
        assert (CHILD_PGID, signal.SIGKILL) in sent
        assert proc.reaped

    async def test_refuses_to_signal_our_own_process_group(self) -> None:
        """A killpg against our own group would take the whole service down."""
        proc = _HangingProc()
        sent: list[tuple[int, int]] = []

        with (
            patch("os.getpgid", lambda pid: OUR_PGID),
            patch("os.killpg", lambda pgid, sig: sent.append((pgid, sig))),
            patch("os.getpgrp", lambda: OUR_PGID),
        ):
            await terminate_process_group(cast(asyncio.subprocess.Process, proc), grace=0.05)

        assert sent == [], "signalled analysis_service's own process group"

    async def test_already_exited_child_is_a_no_op(self) -> None:
        proc = MagicMock()
        proc.returncode = 0
        sent: list[tuple[int, int]] = []

        with patch("os.killpg", lambda pgid, sig: sent.append((pgid, sig))):
            await terminate_process_group(cast(asyncio.subprocess.Process, proc), grace=0.05)

        assert sent == []

    async def test_process_lookup_error_is_swallowed(self) -> None:
        """The child may exit between the timeout and the signal."""
        proc = _HangingProc()

        def _boom(pgid: int, sig: int) -> None:
            raise ProcessLookupError

        with (
            patch("os.getpgid", _patched_pgid),
            patch("os.killpg", _boom),
            patch("os.getpgrp", lambda: OUR_PGID),
        ):
            await terminate_process_group(
                cast(asyncio.subprocess.Process, proc), grace=0.05
            )  # must not raise


@pytest.mark.parametrize(
    "module",
    [
        "src.domains.critique.services.scanners.tool_adapters.lighthouse_adapter",
        "src.domains.critique.services.scanners.tool_adapters.nuclei_adapter",
    ],
)
def test_adapter_imports_the_shared_reaper(module: str) -> None:
    """Neither adapter may hand-roll its own kill path."""
    import importlib

    mod = importlib.import_module(module)
    assert hasattr(mod, "terminate_process_group")
