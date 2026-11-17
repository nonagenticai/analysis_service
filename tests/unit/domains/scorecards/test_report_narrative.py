"""Unit tests for the audit exec-summary worker (A2, id-locked roadmap).

WM stubbed via :class:`FakeWMClient`. Each lever has an executing assertion plus a
documented negative-control line so none is a tautology.
"""

from __future__ import annotations

from src.domains.scorecards.report_narrative import (
    _lock_roadmap,
    maybe_narrate_report,
    narrate_report,
)
from tests.llm_test_doubles import FakeWMClient

_FINDINGS = [
    {"check_id": "cro-hero-a", "title": "Weak hero"},
    {"check_id": "cro-form-b", "title": "Long form"},
]
_REFS = [{"check_id": "cro-hero-a", "title": "Weak hero", "severity": "high"}]
_SCORES = {"overall_score": 61.0, "grade": "C"}


class TestLockRoadmap:
    def test_drops_alien_ids_keeps_real_ones_in_order(self):
        valid = {"cro-hero-a", "cro-form-b"}
        out = _lock_roadmap(["cro-form-b", "HALLUCINATED", "cro-hero-a"], valid)
        assert out == ["cro-form-b", "cro-hero-a"]  # NEG: bypass the id-lock -> HALLUCINATED leaks

    def test_dedupes(self):
        out = _lock_roadmap(["cro-hero-a", "cro-hero-a"], {"cro-hero-a"})
        assert out == ["cro-hero-a"]


class TestNarrateReport:
    async def test_happy_path_id_locks_roadmap(self):
        wm = FakeWMClient(
            outputs=[
                {
                    "summary": "The page scores a C; the hero and form are the weak points.",
                    "so_what": "Weak conversion loses signups.",
                    "roadmap": ["cro-hero-a", "GHOST", "cro-form-b"],
                }
            ]
        )
        exec_summary, _ = await narrate_report(_SCORES, _REFS, None, _FINDINGS, wm=wm)
        assert exec_summary is not None
        assert exec_summary["roadmap"] == ["cro-hero-a", "cro-form-b"]  # GHOST dropped
        assert exec_summary["summary"].startswith("The page scores")

    async def test_empty_summary_is_dropped(self):
        wm = FakeWMClient(outputs=[{"summary": "", "roadmap": ["cro-hero-a"]}])
        exec_summary, _ = await narrate_report(_SCORES, _REFS, None, _FINDINGS, wm=wm)
        assert (
            exec_summary is None
        )  # NEG: drop the empty-summary guard -> a summary-less block returns

    async def test_prose_length_clamped(self):
        wm = FakeWMClient(outputs=[{"summary": "x" * 5000, "so_what": "y" * 5000}])
        exec_summary, _ = await narrate_report(_SCORES, _REFS, None, _FINDINGS, wm=wm)
        assert exec_summary is not None
        assert (
            len(exec_summary["summary"]) <= 600
        )  # NEG: remove the clamp -> a 5000-char ramble ships
        assert len(exec_summary["so_what"]) <= 600


class TestMaybeNarrateReport:
    async def test_flag_off_is_noop(self):
        result: dict = {"overall_score": 61.0}
        await maybe_narrate_report(
            result,
            refs=_REFS,
            findings=_FINDINGS,
            scores=_SCORES,
            grounding=None,
            include_exec_summary=False,
            wm_client=FakeWMClient(),
        )
        assert "exec_summary" not in result  # byte-identical path

    async def test_produced_summary_merged(self):
        result: dict = {"overall_score": 61.0}
        wm = FakeWMClient(outputs=[{"summary": "A short grounded summary.", "roadmap": []}])
        await maybe_narrate_report(
            result,
            refs=_REFS,
            findings=_FINDINGS,
            scores=_SCORES,
            grounding=None,
            include_exec_summary=True,
            wm_client=wm,
        )
        assert result["exec_summary"]["summary"] == "A short grounded summary."

    async def test_fail_soft_swallows_wm_error(self):
        result: dict = {"overall_score": 61.0}
        wm = FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        await maybe_narrate_report(
            result,
            refs=_REFS,
            findings=_FINDINGS,
            scores=_SCORES,
            grounding=None,
            include_exec_summary=True,
            wm_client=wm,
        )
        assert "exec_summary" not in result  # NEG: let it propagate -> this test errors
