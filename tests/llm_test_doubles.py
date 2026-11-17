"""Test doubles for the worker_management LLM gateway.

Every LLM call site in :mod:`analysis_service` goes through
:class:`w2v_common.clients.WMClient` — see memory ``wm-as-sole-llm-gateway``.
Tests inject a fake :class:`WMClient` whose ``message`` method returns
canned :class:`w2v_common.clients.WMResponse` payloads (or raises
configured errors) without any HTTP traffic.

Usage::

    from tests.llm_test_doubles import FakeWMClient, install_wm_factory

    fake = FakeWMClient(outputs=[{"description": "..."}])
    install_wm_factory(monkeypatch, lambda: fake)
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from w2v_common.clients import WMClient, WMResponse


@dataclass
class FakeWMCall:
    """A captured ``WMClient.message`` invocation."""

    worker_name: str
    message: str
    system_prompt: str | None
    output_schema: dict[str, Any] | None
    model_override: str | None


@dataclass
class FakeWMClient(WMClient):
    """Drop-in replacement for :class:`WMClient` used in unit tests.

    ``outputs`` is a sequence of payloads cycled per ``message`` call:
      * ``dict`` / ``list`` -> returned via ``WMResponse.output`` directly
        (structured-output path).
      * ``str``             -> returned as text (free-text path).

    ``raise_after`` and ``error`` together model transient failures:
      * ``raise_after="always"`` raises *error* on every call.
      * ``raise_after=N`` raises on the (1-indexed) Nth call only.
    """

    outputs: list[Any] = field(default_factory=list)
    raise_after: int | str | None = None
    error: BaseException | None = None
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
    )
    calls: list[FakeWMCall] = field(default_factory=list)
    base_url: str = "http://wm.test.invalid:8887"

    def __post_init__(self) -> None:
        if not self.outputs:
            self.outputs = [{}]

    async def message(
        self,
        worker_name: str,
        message: str | None = None,
        *,
        content_blocks: list[dict[str, Any]] | None = None,
        context: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        output_schema: dict[str, Any] | None = None,
        model_override: str | None = None,
        max_tokens: int | None = None,
        work_tier: Literal["small", "medium", "high"] | None = None,
        product: str | None = None,
        enable_reasoning: bool | None = None,
        disable_state_storage: bool = True,
        project_id: uuid.UUID | str | None = None,
        slot: Literal["llm", "vlm"] | None = None,
    ) -> WMResponse:
        del content_blocks, context, disable_state_storage, project_id, slot
        del max_tokens, work_tier, product, enable_reasoning
        self.calls.append(
            FakeWMCall(
                worker_name=worker_name,
                message=message or "",
                system_prompt=system_prompt,
                output_schema=output_schema,
                model_override=model_override,
            )
        )
        n = len(self.calls)
        if self.raise_after == "always":
            raise self.error or RuntimeError("FakeWMClient: forced failure")
        if isinstance(self.raise_after, int) and n == self.raise_after:
            raise self.error or RuntimeError("FakeWMClient: forced failure")

        payload = self.outputs[(n - 1) % len(self.outputs)]
        if isinstance(payload, (dict, list)):
            output_type = "object" if isinstance(payload, dict) else "array"
            return WMResponse(
                output=payload,
                output_type=output_type,
                output_schema_used=output_schema,
                metadata=None,
                usage_metrics=dict(self.usage),
            )
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return WMResponse(
            output=text,
            output_type="string",
            output_schema_used=None,
            metadata=None,
            usage_metrics=dict(self.usage),
        )

    async def aclose(self) -> None:  # pragma: no cover — symmetry with WMClient
        return None


def install_wm_factory(
    monkeypatch: Any,
    factory: Callable[..., WMClient],
    target: str = "src.llm_client.get_wm_client",
) -> None:
    """Install ``factory`` as ``src.llm_client.get_wm_client``.

    The factory is called once and the returned instance is reused across
    subsequent calls so retries and per-call counters accumulate inside a
    single test.

    Importantly, every consumer module that did
    ``from src.llm_client import get_wm_client`` rebinds the name in its
    own namespace at import time — patching only ``src.llm_client.get_wm_client``
    leaves those rebindings untouched. To make monkeypatching reliable,
    we patch each known consumer module too.
    """
    cached: dict[str, Any] = {}

    def wrapper(*_: object, **__: object) -> Any:
        if "client" not in cached:
            cached["client"] = factory()
        return cached["client"]

    monkeypatch.setattr(target, wrapper)
    # Also patch every consumer module's local rebinding so callsites that
    # imported ``get_wm_client`` directly into their namespace see the fake.
    consumer_modules = (
        "src.domains.analyze.services.analyzer_service",
        "src.domains.analyze.services.review_generator",
        "src.domains.brand.services.llm_brand_analyzer",
        "src.domains.critique.services.report_generator",
        "src.domains.critique.services.scanners.ux_reviewer",
        "src.domains.review.services.review_service",
    )
    import importlib

    for mod_name in consumer_modules:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        if hasattr(mod, "get_wm_client"):
            monkeypatch.setattr(f"{mod_name}.get_wm_client", wrapper, raising=False)


def make_wm_response(
    output: Any,
    *,
    usage: dict[str, int] | None = None,
    output_type: str | None = None,
) -> WMResponse:
    """Construct a :class:`WMResponse` for ad-hoc test fixtures."""
    if output_type is None:
        if isinstance(output, dict):
            output_type = "object"
        elif isinstance(output, list):
            output_type = "array"
        else:
            output_type = "string"
    return WMResponse(
        output=output,
        output_type=output_type,
        output_schema_used=None,
        metadata=None,
        usage_metrics=usage,
    )


def fake_wm_client(
    *,
    outputs: Iterable[Any] | None = None,
    raise_after: int | str | None = None,
    error: BaseException | None = None,
    usage: dict[str, int] | None = None,
) -> FakeWMClient:
    """Build a :class:`FakeWMClient` with the given canned outputs / errors."""
    return FakeWMClient(
        outputs=list(outputs) if outputs is not None else [{}],
        raise_after=raise_after,
        error=error,
        usage=usage or {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )


__all__ = [
    "FakeWMCall",
    "FakeWMClient",
    "fake_wm_client",
    "install_wm_factory",
    "make_wm_response",
]


# ---------------------------------------------------------------------------
# Worker-routed fake (audit-report contract, design 2026-10-17)
# ---------------------------------------------------------------------------

_CHECK_ID_RE = re.compile(r"- check_id: (\S+)")
_EVIDENCE_RE = re.compile(r"- evidence: (.+)")


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _audit_writer(message: str) -> dict[str, Any]:
    check_id = _first_group(_CHECK_ID_RE, message)
    evidence = _first_group(_EVIDENCE_RE, message)
    return {
        "check_id": check_id,
        "fix_prose": f"Fix this on the page where it shows {evidence[:80]}.",
        "rationale": "It blocks the visitor or the crawler.",
        "example": "Before: <old>; after: <new>.",
        "effort": "low",
    }


def _audit_grounding(message: str) -> dict[str, Any]:
    check_id = _first_group(_CHECK_ID_RE, message)
    return {"check_id": check_id, "supported": True, "evidence_ref": "quoted from the page"}


DEFAULT_AUDIT_ROUTES: dict[str, Callable[[str], Any]] = {
    "audit-recommendation-writer": _audit_writer,
    "audit-claim-grounding": _audit_grounding,
    "audit-exec-summary": lambda _m: {
        "summary": "The page scores below target; the top findings block conversion.",
        "so_what": "Fixing the top three recovers the most points.",
        "roadmap": [],
    },
    "audit-report-critic": lambda _m: {
        "score": 81,
        "generic_check_ids": [],
        "ungrounded_check_ids": [],
        "rerun_stages": [],
    },
    "cro-micro-judge": lambda _m: {
        "judgments": [
            {
                "element": "headline",
                "score": 55,
                "assessment": "The headline names a feature, not an outcome.",
                "suggestion": "Lead with the outcome the visitor gets.",
            }
        ]
    },
}


@dataclass
class RoutedWMClient(FakeWMClient):
    """FakeWMClient that answers per ``worker_name`` (a healthy WM, for wire tests).

    ``routes`` maps a worker name to ``fn(message) -> output``; unknown workers
    raise so a test never silently gets a wrong-shaped default.
    """

    routes: dict[str, Callable[[str], Any]] = field(
        default_factory=lambda: dict(DEFAULT_AUDIT_ROUTES)
    )

    async def message(  # type: ignore[override]
        self, worker_name: str, message: str | None = None, **kwargs: Any
    ) -> WMResponse:
        await super().message(worker_name, message, **kwargs)
        route = self.routes.get(worker_name)
        if route is None:
            raise RuntimeError(f"RoutedWMClient: no route for worker {worker_name!r}")
        return make_wm_response(route(message or ""), usage=dict(self.usage))
