"""AI-analysis status contract (design 2026-10-17 §3.2): no silent AI failure.

Each lever has an executing assertion plus a negative-control note, so none is a
tautology: break the rule named in the NEG comment and the assertion fails.
"""

from __future__ import annotations

import httpx
import pytest

from src.domains.cro.service import score_cro
from src.domains.marketing.service import score_marketing
from src.domains.scorecards.ai_status import (
    WM_NOT_CONFIGURED,
    AiStatusCollector,
    describe_exception,
)
from src.domains.scorecards.schemas import AiStatus
from src.domains.seo_audit.service import audit_seo
from tests.llm_test_doubles import FakeWMClient, RoutedWMClient

_WEAK = (
    "<html><head><title>Acme</title></head><body>"
    "<h1>We do things</h1><p>Our product is software for teams.</p>"
    "<form><input><input><input><input><input><input></form>"
    "<img src='a.png'></body></html>"
)

_ALL_SHARED = {
    "include_recommendations": True,
    "include_grounding": True,
    "include_exec_summary": True,
    "include_critic": True,
    "include_evidence": True,
}


def _wm_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://wm.test.invalid/workers/x/message")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


class TestCollectorRules:
    def test_not_requested(self):
        ai = AiStatusCollector(requested=False)
        assert ai.to_dict()["status"] == "not_requested"
        assert ai.to_dict()["reason"] is None

    def test_requested_but_nothing_recorded_is_unavailable_not_complete(self):
        # NEG: return "complete" for an empty layer list -> a missing status reads green.
        assert AiStatusCollector(requested=True).overall() == "unavailable"

    def test_all_ok_is_complete(self):
        ai = AiStatusCollector(requested=True)
        ai.ok("exec_summary", "audit-exec-summary")
        ai.skipped("grounding", "audit-claim-grounding", "no written recommendation to ground")
        assert ai.overall() == "complete"

    def test_mixed_is_partial_and_reason_names_the_failed_layer(self):
        ai = AiStatusCollector(requested=True)
        ai.ok("exec_summary", "audit-exec-summary")
        ai.failed("critic", "audit-report-critic", "WM 401 Unauthorized")
        assert ai.overall() == "partial"
        assert ai.reason() == "critic: WM 401 Unauthorized"

    def test_all_failed_is_unavailable(self):
        ai = AiStatusCollector(requested=True)
        ai.failed("exec_summary", "audit-exec-summary", "boom")
        ai.failed("critic", "audit-report-critic", "boom")
        assert ai.overall() == "unavailable"

    def test_wm_not_configured_is_not_a_skip_by_design(self):
        ai = AiStatusCollector(requested=True)
        ai.wm_unavailable("seo_intent", "seo-intent-classify")
        # NEG: treat it as by-design -> "complete" with no AI at all.
        assert ai.overall() == "unavailable"
        assert WM_NOT_CONFIGURED in (ai.reason() or "")

    def test_empty_alone_is_partial(self):
        ai = AiStatusCollector(requested=True)
        ai.empty("exec_summary", "audit-exec-summary", "the model returned no summary")
        assert ai.overall() == "partial"

    def test_rerecording_a_layer_replaces_it(self):
        ai = AiStatusCollector(requested=True)
        ai.failed("critic", "audit-report-critic", "x")
        ai.ok("critic", "audit-report-critic")
        assert [layer["status"] for layer in ai.to_dict()["layers"]] == ["ok"]

    def test_wire_shape_validates(self):
        ai = AiStatusCollector(requested=True)
        ai.failed("micro_judge", "cro-micro-judge", _wm_401())
        parsed = AiStatus.model_validate(ai.to_dict())
        assert parsed.status == "unavailable"
        assert parsed.layers[0].reason == "WM 401 Unauthorized"

    def test_describe_http_401(self):
        assert describe_exception(_wm_401()) == "WM 401 Unauthorized"

    def test_describe_plain_exception(self):
        assert describe_exception(RuntimeError("wm down")) == "RuntimeError: wm down"


_SERVICES = [
    pytest.param(score_cro, {"include_llm": True}, id="cro"),
    pytest.param(score_marketing, {"include_llm": True}, id="marketing"),
    pytest.param(audit_seo, {}, id="seo"),
]


class TestServicesReportAiStatus:
    @pytest.mark.parametrize(("service", "extra"), _SERVICES)
    async def test_ai_off_is_not_requested(self, service, extra):
        del extra
        result = await service(html=_WEAK, url=None)
        assert result["ai"] == {
            "requested": False,
            "status": "not_requested",
            "reason": None,
            "layers": [],
            "annotations": [],
        }

    @pytest.mark.parametrize(("service", "extra"), _SERVICES)
    async def test_wm_401_is_unavailable_with_reasons_and_still_200(self, service, extra):
        wm = FakeWMClient(raise_after="always", error=_wm_401())
        result = await service(html=_WEAK, url=None, wm_client=wm, **_ALL_SHARED, **extra)
        # The deterministic score still returns (fail-soft) ...
        assert isinstance(result["overall_score"], (int, float))
        # ... but the response now SAYS the AI failed. NEG: drop the recording in
        # any maybe_* helper -> status flips to complete/partial.
        ai = result["ai"]
        assert ai["status"] == "unavailable", ai
        by_layer = {layer["layer"]: layer for layer in ai["layers"]}
        for layer in ("recommendations", "exec_summary", "critic"):
            assert by_layer[layer]["status"] == "failed"
            assert "WM 401" in (by_layer[layer]["reason"] or "")
        assert "WM 401" in (ai["reason"] or "")

    @pytest.mark.parametrize(("service", "extra"), _SERVICES)
    async def test_healthy_wm_is_complete(self, service, extra):
        wm = RoutedWMClient()
        result = await service(html=_WEAK, url=None, wm_client=wm, **_ALL_SHARED, **extra)
        ai = result["ai"]
        assert ai["status"] == "complete", ai
        assert result["exec_summary"]["summary"]
        assert result["critique"]["score"] == 81

    async def test_cro_micro_judge_failure_alone_is_partial(self):
        wm = FakeWMClient(raise_after=1, error=_wm_401(), outputs=[{"summary": "ok"}])
        result = await score_cro(
            html=_WEAK, url=None, wm_client=wm, include_llm=True, include_exec_summary=True
        )
        # Order: exec-summary runs AFTER the micro-judge; call 1 (judge) fails.
        layers = {layer["layer"]: layer["status"] for layer in result["ai"]["layers"]}
        assert layers["micro_judge"] == "failed"
        assert layers["exec_summary"] == "ok"
        assert result["ai"]["status"] == "partial"
        assert result["llm_enriched"] is False

    async def test_cro_no_snippets_is_skipped_by_design(self):
        html = "<html><body><div>x</div></body></html>"
        result = await score_cro(html=html, url=None, wm_client=RoutedWMClient(), include_llm=True)
        layers = {layer["layer"]: layer for layer in result["ai"]["layers"]}
        assert layers["micro_judge"]["status"] == "skipped"
        assert result["ai"]["status"] == "complete"
