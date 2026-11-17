"""Service-level wiring tests for the PR3 WM workers (exec-summary / grounding / critic).

Proves the full chain (service -> worker -> merge) end-to-end with WM stubbed: the
opt-in flags actually surface the new top-level keys, and — critically — a WM outage
with EVERY flag on is fully fail-soft (the deterministic score returns unchanged, no
exception, no new keys). Complements the module-level tests, which prove the clamps.
"""

from __future__ import annotations

from src.domains.cro.service import score_cro
from src.domains.seo_audit.service import audit_seo
from tests.llm_test_doubles import FakeWMClient

# A page weak enough to surface findings on every scorer.
_WEAK = (
    "<html><body><p>no headline</p>"
    "<form><input><input><input><input><input><input></form></body></html>"
)


class TestExecSummaryWiring:
    async def test_cro_exec_summary_flag_surfaces_key(self):
        wm = FakeWMClient(
            outputs=[{"summary": "The page scores low; the form is the weak point.", "roadmap": []}]
        )
        result = await score_cro(html=_WEAK, url=None, include_exec_summary=True, wm_client=wm)
        assert result["exec_summary"]["summary"].startswith("The page scores low")
        assert wm.calls[0].worker_name == "audit-exec-summary"

    async def test_seo_exec_summary_flag_surfaces_key(self):
        # Proves seo's needs_wm gate was extended for the new flags (else it is inert).
        wm = FakeWMClient(outputs=[{"summary": "SEO health is poor.", "roadmap": []}])
        result = await audit_seo(html=_WEAK, url=None, include_exec_summary=True, wm_client=wm)
        assert "exec_summary" in result
        assert result["exec_summary"]["summary"] == "SEO health is poor."


class TestCritiqueWiring:
    async def test_cro_critique_flag_surfaces_key(self):
        wm = FakeWMClient(outputs=[{"score": 82, "rerun_stages": []}])
        result = await score_cro(html=_WEAK, url=None, include_critic=True, wm_client=wm)
        assert result["critique"]["score"] == 82
        assert len(wm.calls) == 1  # shipped -> no writer re-run


class TestFullChainFailSoft:
    async def test_all_flags_on_wm_down_is_fail_soft(self):
        wm = FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        # Every opt-in flag on, but WM raises on the first call of each worker.
        result = await score_cro(
            html=_WEAK,
            url=None,
            include_llm=True,
            include_recommendations=True,
            include_grounding=True,
            include_exec_summary=True,
            include_critic=True,
            wm_client=wm,
        )
        # Deterministic score still returned; NO new keys; NO exception escaped.
        assert isinstance(result["overall_score"], (int, float))
        assert "exec_summary" not in result
        assert "critique" not in result
        assert "grounding" not in result
