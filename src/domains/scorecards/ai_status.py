"""Machine-readable AI-analysis status for the scorecard domains (design §3.2).

Every WM-backed layer of a scorecard (recommendation writer, claim grounding,
exec summary, report critic, the micro-judges, the SEO analyzers) used to fail
soft with a ``logger.warning`` and nothing else, so a WM 401 produced a normal
200 that was indistinguishable from "AI was never asked". This module is the
collector those layers now **report into**. They stay fail-soft for the
deterministic score — the collector changes what the response *says*, never
whether it returns.

Wire shape (``AiStatus`` in :mod:`.schemas`)::

    ai: {requested, status, reason, layers: [{layer, worker, status, reason}]}

``status`` rules (a missing observation is never "complete"):

* ``not_requested`` — the caller opted into no AI layer at all.
* ``complete``      — every recorded layer is ``ok`` or ``skipped`` by design.
* ``unavailable``   — no layer is ``ok`` and at least one failed (or was
  skipped because AI itself was unavailable, e.g. no WM client).
* ``partial``       — anything else (some layers delivered, some did not, or a
  layer succeeded but produced nothing — ``empty``).
* A request for AI that recorded **no layer at all** is ``unavailable`` — a
  layer that never reported is not evidence that it ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, TypedDict

AiLayerName = Literal[
    "recommendations",
    "grounding",
    "exec_summary",
    "critic",
    "micro_judge",
    "seo_intent",
    "seo_eeat",
    "content_gap",
]
AiLayerOutcome = Literal["ok", "failed", "skipped", "empty"]
AiOverallStatus = Literal["not_requested", "complete", "partial", "unavailable"]

# Reason text is bounded: it is rendered on a PDF and carried into an
# orchestrator degradation message, never a stack trace.
_MAX_REASON = 240

WM_NOT_CONFIGURED = "WM client not configured"


class AiLayerDict(TypedDict):
    """Wire shape of one ``ai.layers[]`` row (validated by ``schemas.AiLayerStatus``)."""

    layer: AiLayerName
    worker: str
    status: AiLayerOutcome
    reason: str | None


class AiAnnotationDict(TypedDict):
    """Wire shape of one ``ai.annotations[]`` row (``schemas.AiAnnotation``)."""

    layer: AiLayerName
    kind: str
    title: str
    summary: str
    data: dict[str, object]


class AiStatusDict(TypedDict):
    """Wire shape of the ``ai`` block (``schemas.AiStatus``)."""

    requested: bool
    status: AiOverallStatus
    reason: str | None
    layers: list[AiLayerDict]
    annotations: list[AiAnnotationDict]


def describe_exception(exc: BaseException) -> str:
    """Short, render-safe description of a WM/transport failure.

    ``httpx.HTTPStatusError`` → ``"WM 401 Unauthorized"`` (the case the owner hit:
    every LLM stage 401'd). Anything else → ``"<ExcClass>: <message>"``. Never a
    traceback, never request headers (they carry licence tokens).
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        phrase = str(getattr(response, "reason_phrase", "") or "").strip()
        text = f"WM {status_code} {phrase}".strip()
    else:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
        text = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return text[:_MAX_REASON]


@dataclass
class _Layer:
    layer: AiLayerName
    worker: str
    status: AiLayerOutcome
    reason: str | None
    # A skip caused by AI being unavailable (no WM client) is NOT a skip by design:
    # it counts against the overall status. Internal — not on the wire.
    by_design: bool = True


@dataclass
class AiStatusCollector:
    """Per-request accumulator the WM-backed layers record into.

    One instance per scorecard request. Recording the same ``layer`` twice
    replaces the earlier entry (last word wins), so a layer can record a
    provisional outcome and refine it.
    """

    requested: bool
    _layers: list[_Layer] = field(default_factory=list)
    _annotations: list[AiAnnotationDict] = field(default_factory=list)

    # --- recording -----------------------------------------------------------
    def record(
        self,
        layer: AiLayerName,
        worker: str,
        status: AiLayerOutcome,
        reason: str | None = None,
        *,
        by_design: bool = True,
    ) -> None:
        reason = reason[:_MAX_REASON] if reason else None
        self._layers = [entry for entry in self._layers if entry.layer != layer]
        self._layers.append(_Layer(layer, worker, status, reason, by_design))

    def ok(self, layer: AiLayerName, worker: str, reason: str | None = None) -> None:
        self.record(layer, worker, "ok", reason)

    def failed(self, layer: AiLayerName, worker: str, reason: str | BaseException) -> None:
        text = describe_exception(reason) if isinstance(reason, BaseException) else reason
        self.record(layer, worker, "failed", text)

    def empty(self, layer: AiLayerName, worker: str, reason: str) -> None:
        self.record(layer, worker, "empty", reason)

    def skipped(
        self, layer: AiLayerName, worker: str, reason: str, *, by_design: bool = True
    ) -> None:
        self.record(layer, worker, "skipped", reason, by_design=by_design)

    def wm_unavailable(self, layer: AiLayerName, worker: str) -> None:
        """The layer was requested but there is no WM client to run it on."""
        self.skipped(layer, worker, WM_NOT_CONFIGURED, by_design=False)

    def annotate(
        self,
        layer: AiLayerName,
        kind: str,
        *,
        title: str = "",
        summary: str = "",
        data: dict[str, object] | None = None,
    ) -> None:
        """Attach a structured AI observation (wire: ``ai.annotations[]``)."""
        self._annotations.append(
            {"layer": layer, "kind": kind, "title": title, "summary": summary, "data": data or {}}
        )

    # --- reading -------------------------------------------------------------
    def layer_status(self, layer: AiLayerName) -> AiLayerOutcome | None:
        for entry in self._layers:
            if entry.layer == layer:
                return entry.status
        return None

    def _degraded(self) -> list[_Layer]:
        return [
            e
            for e in self._layers
            if e.status in {"failed", "empty"} or (e.status == "skipped" and not e.by_design)
        ]

    def overall(self) -> AiOverallStatus:
        if not self.requested:
            return "not_requested"
        if not self._layers:
            return "unavailable"
        degraded = self._degraded()
        if not degraded:
            return "complete"
        any_ok = any(e.status == "ok" for e in self._layers)
        any_hard_failure = any(e.status != "empty" for e in degraded)
        if not any_ok and any_hard_failure:
            return "unavailable"
        return "partial"

    def reason(self) -> str | None:
        """One line naming every degraded layer and why (``None`` when healthy)."""
        if not self.requested:
            return None
        if not self._layers:
            return "no AI layer reported a status"
        parts = [f"{e.layer}: {e.reason or e.status}" for e in self._degraded()]
        return "; ".join(parts) if parts else None

    def to_dict(self) -> AiStatusDict:
        return {
            "requested": self.requested,
            "status": self.overall(),
            "reason": self.reason(),
            "layers": [
                {"layer": e.layer, "worker": e.worker, "status": e.status, "reason": e.reason}
                for e in self._layers
            ],
            "annotations": list(self._annotations),
        }


def ai_requested(
    *,
    include_llm: bool,
    include_recommendations: bool,
    include_grounding: bool,
    include_exec_summary: bool,
    include_critic: bool,
) -> bool:
    """True when the request opted into any WM-backed layer."""
    return (
        include_llm
        or include_recommendations
        or include_grounding
        or include_exec_summary
        or include_critic
    )
