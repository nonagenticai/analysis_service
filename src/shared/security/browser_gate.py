"""Process-wide ceiling on concurrent headless-Chromium launches.

Why this is its own module, and why the gate is at the *launch* rather than at a
route: the resource being protected is the pod's memory, and every entry point
that renders a page spends it the same way. ``POST /api/brand/batch-extract``
is merely the most obvious offender (up to 50 URLs in one request);
``POST /api/brand/from-url`` and ``POST /api/brands/compare`` -- whose
``extract_if_missing`` synthesises ``https://{domain}`` -- run the *same*
``FetchStage`` and launch the *same* browser. Gating only the batch route caps a
single caller while leaving fifty simultaneous single-URL callers to OOM the pod
exactly as before, which is a ceiling in name only.

Putting the semaphore next to ``pw.chromium.launch`` means a new caller cannot
forget to take a slot: there is no ungated path to a browser.

Why this lives in ``shared`` and not under ``domains/brand``
------------------------------------------------------------
It was born in the brand domain, because brand's ``FetchStage`` was the only
Chromium launcher at the time. It is not any more: the critique domain's
``AxeAdapter`` launches its own Chromium to inject axe-core, and a critique scan
and a brand extraction can be in flight in the same pod at the same time. A
per-domain ceiling of four would then be a pod-wide ceiling of eight, growing
with every domain that learns to render a page -- i.e. exactly the unbounded
behaviour this exists to prevent. **The gate is shared across domains because
the memory it rations is shared across domains**: the pod has one limit, so
there is one gate, and its home is the shared layer alongside
``playwright_guard`` (which is likewise a property of the sink, not of a
domain).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from src.shared.concurrency import bounded_slot

# Chromium's binding constraint is memory, not cores, so this is sized against
# the pod's limit rather than on the assumption that more is faster. A
# module-level constant rather than a setting because ``BrandSettings``
# (``src/config/domains.py``) has no concurrency knob, and the one that exists
# elsewhere -- ``analyze.parallel_workers`` -- bounds an unrelated pool; reusing
# it would silently couple the browser ceiling to a tuning decision made for
# something else.
MAX_CONCURRENT_BROWSERS = 4

# Registry key for the gate. A single name for every domain: see the module
# docstring -- one pod, one memory budget, one ceiling.
_GATE = "chromium"


@asynccontextmanager
async def browser_slot() -> AsyncIterator[None]:
    """Hold one of the ``MAX_CONCURRENT_BROWSERS`` slots for the enclosed block.

    Wrap the browser's whole lifetime -- launch through close -- not just the
    launch call, or the slot is released while the browser is still resident and
    the ceiling stops meaning anything.

    The per-loop bookkeeping (a semaphore binds to the loop that first awaits
    it, and a module-level one leaks across pytest-asyncio's per-test loops)
    lives in :mod:`src.shared.concurrency`, shared with the scanner's TLS gate.

    Yields:
        ``None``; the slot is released on exit, including on exception.
    """
    async with bounded_slot(_GATE, MAX_CONCURRENT_BROWSERS):
        yield
