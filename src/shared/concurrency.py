"""Loop-keyed concurrency ceilings for process-wide, non-CPU resources.

Why a shared helper rather than a semaphore per module
------------------------------------------------------
Two independent things in this service need the *same* awkward primitive: a
ceiling that is process-wide in production but must not leak across event
loops. The headless-Chromium gate (``src/shared/security/browser_gate.py``)
needs it because Chromium's binding constraint is the pod's memory; the TLS
probe gate (``critique``'s security scanner) needs it because
``asyncio.to_thread`` dispatches into the *default* executor, which is shared
with every other ``to_thread`` caller in the process.

A plain module-level ``asyncio.Semaphore`` is the obvious implementation and is
wrong for both: a semaphore binds to the loop that first awaits it, so under
pytest-asyncio -- where every test gets a fresh loop -- a module-level
semaphore still holding waiters from a finished loop is a hang waiting to
happen. The correct shape is "one semaphore per (loop, resource)", held in a
weak-keyed map so a closed loop's entry is collected with it. That shape is
fiddly enough, and now duplicated enough, that copying it a second time would
guarantee the two copies drift.

What this deliberately does NOT do
----------------------------------
It does not bound CPU or a connection pool. Both current consumers protect a
resource whose cost is *memory or a blocking OS thread*, where the right
ceiling is a small constant sized against the pod, not against core count. A
future caller that wants a pool should reach for the pool's own bound.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

# ``loop -> {resource name -> semaphore}``. Weak on the loop so a finished test
# loop takes its semaphores with it; a plain dict here would retain every loop
# the process has ever run, along with any tasks parked on those semaphores.
_registries: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = (
    WeakKeyDictionary()
)


def loop_semaphore(name: str, limit: int) -> asyncio.Semaphore:
    """Return the semaphore for *name* on the running loop, creating it on first use.

    In production there is exactly one loop, so this is a single process-wide
    ceiling per *name* -- which is the point: the resource being rationed
    (pod memory, the default thread executor) is shared by the whole process,
    not by one request or one domain.

    *limit* is read only when the semaphore is created. A caller that raises its
    constant later will not retroactively widen an existing gate, which is
    intentional: silently resizing a live ceiling while tasks are parked on it
    is not something any consumer here wants.

    Args:
        name: Stable identifier for the rationed resource. Distinct names get
            distinct, independent ceilings on the same loop.
        limit: Maximum number of concurrent holders.

    Returns:
        The semaphore guarding *name* on the currently running loop.
    """
    loop = asyncio.get_running_loop()
    registry = _registries.get(loop)
    if registry is None:
        registry = {}
        _registries[loop] = registry
    semaphore = registry.get(name)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        registry[name] = semaphore
    return semaphore


@asynccontextmanager
async def bounded_slot(name: str, limit: int) -> AsyncIterator[None]:
    """Hold one of *limit* slots on the *name* gate for the enclosed block.

    Wrap the resource's whole lifetime, not merely its acquisition. Releasing at
    the point of launch (or of dispatch) would let N holders be resident while
    only one held a slot, and the ceiling would stop meaning anything.

    Args:
        name: The rationed resource, as passed to :func:`loop_semaphore`.
        limit: Maximum number of concurrent holders.

    Yields:
        ``None``; the slot is released on exit, including on exception.
    """
    async with loop_semaphore(name, limit):
        yield


def reset_for_testing() -> None:
    """Drop every cached semaphore.

    Exists so a test that changes one of the ``MAX_*`` constants can force the
    next acquisition to build a semaphore at the new size -- without it, a gate
    already created on that loop keeps its original limit (see
    :func:`loop_semaphore`). Never called from production code.
    """
    _registries.clear()
