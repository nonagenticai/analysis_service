"""Shared helpers and background-task plumbing for critique routes."""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException, Request

from src.config import get_settings
from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.critique_service import CritiqueService
from src.shared.tenant import require_request_tenant

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def _get_tenant_id(request: Request) -> str:
    """The caller's tenant from request state, validated as a real uuid, or HTTP 401.

    Was ``getattr(request.state, "tenant_id", "anonymous")`` — it fabricated a
    sentinel that is not a tenant, and since migration 0013 made ``tenant_id`` a
    native ``uuid`` column a non-uuid slug no longer degrades to "0 rows / 404":
    Postgres raises ``InvalidTextRepresentation`` and the caller gets a 500.
    Malformed identity must be a 4xx. See ``src.shared.tenant`` for why the
    answer is 401 rather than 400 here.
    """
    return str(require_request_tenant(request))


def validate_artifact_ref(artifact_ref: str | None, tenant_id: str) -> None:
    """Reject a confinement-violating ``artifact_ref`` with a 4xx, pre-persistence.

    Same chokepoint discipline as the SSRF guard on ``body.url`` in
    ``jobs.py::create_critique``, and for the same reason. Without this, a
    traversal attempt is answered with a normal ``job_id`` and a ``200``: the
    ``ValueError`` raised by ``_artifact_object_key`` is swallowed by the broad
    ``except (OSError, ValueError, RuntimeError, TimeoutError)`` in
    ``process_job``, which finalizes the job as *failed* and persists the
    rejection text into ``error_message``. That is the wrong shape three times
    over — a rejected request should not create a row, should not be reported
    as an accepted job, and should not persist guard internals.

    The deep guard in :meth:`_artifact_object_key` stays exactly as it is; this
    is belt-and-braces, not a relocation. It must be, because ``process_job``
    is also reachable from paths that never pass through this route.
    """
    if not artifact_ref:
        return
    try:
        CritiqueService._artifact_object_key(artifact_ref, tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid artifact_ref: {exc}") from exc


def require_operator(request: Request) -> None:
    """Gate a route to operator/admin identities (FastAPI dependency).

    The scoring-weights + runtime-config-override endpoints read/write process-GLOBAL
    runtime config shared across every tenant, so a normal tenant token must not reach
    them. ``AuthMiddleware`` populates ``request.state.roles`` from a Keycloak JWT's realm
    roles; license-token and in-cluster identities have no roles. Admit only when those
    roles intersect ``settings.authz.operator_roles``, else 403.
    """
    roles = set(getattr(request.state, "roles", None) or [])
    allowed = set(get_settings().authz.operator_roles)
    if not roles & allowed:
        raise HTTPException(status_code=403, detail="Operator role required")


def _get_critique_service(request: Request) -> CritiqueService:
    """Get CritiqueService from app state."""
    return request.app.state.critique_service


# How long the mark-failed write gets to land while the task is being cancelled,
# and how many further cancellations it will absorb before giving up. Both are
# small on purpose: the caller is a shutdown path, and the reaper
# (``services/reaper.py``) is the backstop if the write does not make it.
_CANCEL_FINALIZE_TIMEOUT_SECONDS = 5.0
_CANCEL_FINALIZE_ABSORB_LIMIT = 3


async def _mark_failed(service: CritiqueService, job_id: str, tenant_id: str, detail: str) -> None:
    """Persist ``failed`` + ``detail`` on ``job_id``; log and swallow write errors.

    Swallowing is deliberate: the sole callers are error/cancellation paths that
    have nothing better to do with a second failure, and if this ran as a Task
    whose exception nobody retrieved, asyncio would emit a bare "Task exception
    was never retrieved" with no job id in it.
    """
    from src.shared.database.session import get_db_session

    try:
        async with get_db_session() as db:
            await service.mark_failed(db, job_id, detail, tenant_id=tenant_id)
    except Exception:
        logger.exception("Failed to mark job %s as failed", job_id)


async def _mark_failed_during_cancel(
    service: CritiqueService, job_id: str, tenant_id: str, detail: str
) -> None:
    """Land the mark-failed write even though THIS task is already cancelled.

    Bounded absorb-and-retry, not a bare ``asyncio.shield``. A bare shield does
    NOT make the caller wait: it re-raises the cancellation immediately while the
    shielded work continues detached, so the enclosing task unwinds and the
    process can exit before the UPDATE reaches Postgres — which is the exact
    stranding this is here to prevent. So the write runs as its own Task and this
    goes back to waiting on it after each cancellation, up to a hard deadline.

    The deadline matters as much as the retry: this runs inside lifespan
    shutdown, which is itself racing the pod's termination grace period. An
    unbounded wait would trade a stranded row for a pod that will not die.
    """
    write = asyncio.ensure_future(_mark_failed(service, job_id, tenant_id, detail))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CANCEL_FINALIZE_TIMEOUT_SECONDS

    for _ in range(_CANCEL_FINALIZE_ABSORB_LIMIT):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(asyncio.shield(write), timeout=remaining)
            return
        except asyncio.CancelledError:
            # Re-cancelled while the write was in flight. shield() re-raises here
            # but the write itself is untouched — absorb and go back to waiting.
            if write.done():
                return
            continue
        except TimeoutError:
            break

    write.cancel()
    logger.error(
        "Could not finalize cancelled critique job %s within %.1fs; "
        "leaving it for the stale-job reaper",
        job_id,
        _CANCEL_FINALIZE_TIMEOUT_SECONDS,
    )


async def _process_job_background(
    service: CritiqueService,
    job_id: str,
    tenant_id: str,
    *,
    force_rescan: bool = False,
    site_analysis: dict | None = None,
    llm_config: LLMConfigOverride | None = None,
    html_input: str | None = None,
    artifact_ref: str | None = None,
) -> None:
    """Background task to process a critique job."""
    from src.shared.database.session import get_db_session

    try:
        async with get_db_session() as db:
            await service.process_job(
                db,
                job_id,
                tenant_id,
                force_rescan=force_rescan,
                site_analysis=site_analysis,
                llm_config=llm_config,
                html_input=html_input,
                artifact_ref=artifact_ref,
            )
    except asyncio.CancelledError:
        # MUST come before `except Exception`, and must exist at all:
        # `asyncio.CancelledError` is a BaseException, so the handler below never
        # saw it. A pod rollout / lifespan shutdown (`asgi.py::_shutdown` cancels
        # every background task) therefore SKIPPED mark_failed entirely and left
        # the row at `status='running'` forever — with the orchestrator polling
        # that row forever, because `running` is not terminal.
        #
        # `failed` is the terminal state chosen here, not a resumable one: there
        # is no resume machinery on this path. The route is fire-and-forget
        # (`asyncio.create_task`), `process_job` has no checkpoints, and nothing
        # re-dispatches a `pending` row — so parking it back at `pending` would
        # just move the stranding from one non-terminal state to another, with
        # the added lie that something is still going to pick it up.
        #
        # Then RE-RAISE. Cancellation is not this task's to swallow; `_shutdown`
        # gathers these tasks and a swallowed cancel makes shutdown look clean
        # while the loop is being torn down underneath it.
        logger.warning("Background critique job %s cancelled; finalizing as failed", job_id)
        await _mark_failed_during_cancel(
            service,
            job_id,
            tenant_id,
            "CancelledError: job cancelled before completion (service shutdown)",
        )
        raise
    except Exception as exc:
        logger.exception("Background critique job %s failed", job_id)
        # Some exceptions (notably httpx.ConnectError) stringify to "", which
        # would persist a blank reason and surface upstream as "... failed:"
        # with nothing after the colon. Always include the exception type so the
        # orchestrator/UI shows an actionable detail.
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        await _mark_failed(service, job_id, tenant_id, detail)
