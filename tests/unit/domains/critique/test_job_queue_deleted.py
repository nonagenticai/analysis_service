"""The Redis critique job queue is DELETED. These tests pin why, and keep it gone.

The subsystem was dead but advertised itself as alive, which is the worse of the
two failure modes:

* **No producer.** `JobQueue.enqueue` had exactly zero callers — not in this
  repo, not in any sibling w2v service, not in `the fleet deployment configuration`. The only
  construction site was `asgi.py`, which then started `run_worker` against it.
  So two pods each ran a worker loop polling a list nothing ever wrote.
* **`job_queue.max_retries` was a lie.** The live path is
  `routes/_helpers.py::_process_job_background`, which marks a job failed on the
  first exception. The retry ladder the setting described never applied to a
  single real job.
* **The DLQ was permanently empty.** `queue_depth()["dlq"]` could only ever
  return 0, so any alert built on it would have looked armed while being blind
  to every failure. Verified 2026-08-25 against the live cluster: Redis db 1
  held `critique:scanner:*` / `critique:cache:*` / `critique:domain_keys:*`
  keys — proving the client was connected and the worker HAD started — and not
  one `critique:queue:*` key. The queue had never held a job.

Wiring it up instead was rejected on evidence, not taste:

1. Nothing consumes the metric. `analysis-service`'s Service carries no
   `prometheus.io/scrape` annotation, there is no ServiceMonitor, no
   PrometheusRule and no Grafana panel mentioning it. Deleting it disarms
   nothing, because nothing was ever armed.
2. The queue payload is strictly narrower than the live call. `enqueue` carries
   `job_id` / `tenant_id` / `force_rescan`; `_process_job_background` also
   carries `site_analysis`, `llm_config`, `html_input` and `artifact_ref`.
   Routing the live path through the queue would have silently dropped four
   request-body fields.
3. A retry that varies nothing cannot succeed. Re-issuing the same `job_id` for
   the same tenant with the same input reproduces the same LLM call, so the
   ladder would burn budget to fail identically.

The settings surface guards live in `tests/unit/shared/test_no_dead_settings_surface.py`
(`JOB_QUEUE_` is now a retired prefix, so setting a queue knob fails startup
loudly instead of being ignored). This file guards the code surface.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
SRC_DIR = REPO_ROOT / "src"

#: The Redis key prefix the deleted queue owned. Nothing in `src/` may touch it
#: again without also restoring a producer.
QUEUE_KEY_PREFIX = "critique:queue"

DELETED_MODULES = [
    "src.domains.critique.services.job_queue",
    "src.domains.critique.services.job_worker",
]


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_deleted_module_cannot_be_imported(module: str) -> None:
    """Importing the queue or its worker must fail, not resurrect a dead poller."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


@pytest.mark.parametrize("module", DELETED_MODULES)
def test_deleted_module_file_is_gone(module: str) -> None:
    """Control for the import test: a stale `.pyc`-only absence would not count."""
    path = SRC_DIR.joinpath(*module.split(".")[1:]).with_suffix(".py")
    assert not path.exists(), f"{path} is back on disk"


def test_settings_expose_no_job_queue_knob() -> None:
    """Reading the retry knob must be an AttributeError, not a plausible number.

    `settings.job_queue.max_retries` returning `3` is exactly the false signal
    this deletion removes: it reads as a live reliability control.
    """
    from src.config import get_settings

    settings = get_settings()
    # Indirect on purpose: `settings.job_queue` written literally is now a
    # static type error too (ty resolves it to "no attribute"), which would fail
    # the type gate before this ever ran. Going through the name keeps the
    # RUNTIME behaviour — an operator's read blows up — under test.
    section = "job_queue"
    assert not hasattr(settings, section)
    with pytest.raises(AttributeError):
        _ = getattr(settings, section).max_retries


def test_no_source_file_touches_the_queue_key_prefix() -> None:
    """The DLQ key can only exist if some code names it. None may.

    Asserted over string *literals* parsed out of `src/`, so the prose in
    `asgi.py` and `src/config/__init__.py` explaining the deletion does not
    count as a resurrection.
    """
    offenders: list[str] = []
    scanned = 0
    for path in SRC_DIR.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        scanned += 1
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:  # pragma: no cover - src/ must parse
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and QUEUE_KEY_PREFIX in node.value
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {node.value!r}")

    # Non-vacuity: an empty scan would satisfy the assertion below for free.
    assert scanned > 50, f"only parsed {scanned} src/ files — the walk has rotted"
    assert not offenders, (
        f"`{QUEUE_KEY_PREFIX}:*` is named again in src/: {offenders}. "
        "Reintroducing the key without a producer recreates a DLQ that can only "
        "ever read 0."
    )


class _RecordingRedis:
    """An AVAILABLE redis client that records every key it is asked about.

    Deliberately not a strict mock asserting call counts: the property under
    test is *which keys the service touches*, and a count says nothing about
    that. Any list/set primitive returns an empty-ish value so a resurrected
    worker would poll happily rather than crash — i.e. the test fails because
    the queue was touched, not because the fake was too thin.
    """

    is_available = True

    def __init__(self) -> None:
        self.keys_touched: list[str] = []

    def _record(self, key: str) -> None:
        self.keys_touched.append(key)

    async def rpop(self, key: str) -> None:
        self._record(key)
        return

    async def lpush(self, key: str, *_: Any) -> int:
        self._record(key)
        return 1

    async def llen(self, key: str) -> int:
        self._record(key)
        return 0

    async def sadd(self, key: str, *_: Any) -> int:
        self._record(key)
        return 1

    async def srem(self, key: str, *_: Any) -> int:
        self._record(key)
        return 1

    async def scard(self, key: str) -> int:
        self._record(key)
        return 0


async def test_critique_startup_starts_no_queue_worker() -> None:
    """With Redis fully AVAILABLE, startup must still poll no queue.

    This is the regression that matters: the old wiring keyed the worker off
    `redis.is_available` alone (it never even consulted `job_queue.enabled`), so
    a healthy Redis was sufficient to start a poller against an empty list. The
    fake below reports available and answers every list primitive, so a
    resurrected worker WOULD succeed in polling — and would be caught here by
    the key it touched.
    """
    from src.asgi import _init_critique_domain

    redis = _RecordingRedis()
    app: Any = SimpleNamespace(state=SimpleNamespace(redis_client=redis))
    background_tasks: list[asyncio.Task[Any]] = []

    _init_critique_domain(app, background_tasks)
    # Give any task that startup created a chance to run its first poll.
    await asyncio.sleep(0.05)

    try:
        # Order matters: assert the BEHAVIOUR (which Redis keys startup actually
        # went to) before the structural `app.state` check. Reversed, the state
        # assertion trips first and the failure output never shows that a real
        # poll happened — which is the thing worth seeing.
        assert not [key for key in redis.keys_touched if key.startswith(QUEUE_KEY_PREFIX)], (
            f"startup polled the deleted critique queue: {redis.keys_touched}"
        )
        assert not hasattr(app.state, "critique_job_queue"), (
            "startup published a JobQueue on app.state again"
        )
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)


def test_the_live_path_still_carries_what_the_queue_could_not() -> None:
    """The reason (b) beat (a): `_process_job_background` is strictly richer.

    `enqueue` could only carry `job_id`/`tenant_id`/`force_rescan`. If someone
    reintroduces a queue by routing this call through it, these four parameters
    are what silently disappears — so pin them to the surviving path.
    """
    import inspect

    from src.domains.critique.routes._helpers import _process_job_background

    params = inspect.signature(_process_job_background).parameters
    for name in ("site_analysis", "llm_config", "html_input", "artifact_ref"):
        assert name in params, (
            f"`{name}` left _process_job_background. It is one of the fields the "
            "deleted queue payload could not carry; losing it here loses it "
            "everywhere."
        )
