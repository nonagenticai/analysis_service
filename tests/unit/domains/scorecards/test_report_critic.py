"""Unit tests for the audit report-critic worker (A11, bounded in-process re-run).

WM stubbed via :class:`FakeWMClient`. Covers the id-lock, the ship-gate that forces
``rerun_stages`` empty above threshold, the SINGLE bounded writer re-run over flagged
ids, and fail-soft. Each lever has a documented negative-control line.
"""

from __future__ import annotations

from src.domains.scorecards.report_critic import (
    _normalize_critique,
    critique_report,
    maybe_critique_report,
)
from tests.llm_test_doubles import FakeWMClient

_FINDINGS = [
    {
        "check_id": "cro-form-b",
        "title": "Long form",
        "evidence": "the signup form has email password phone fields",
    },
]
_SCORES = {"overall_score": 61.0, "grade": "C"}


def _ref_with_rec() -> dict:
    return {
        "check_id": "cro-form-b",
        "title": "Long form",
        "severity": "high",
        "recommendation": {
            "fix_prose": "Trim the form.",
            "rationale": "",
            "example": "",
            "effort": "low",
        },
    }


class TestNormalizeCritique:
    def test_missing_score_is_dropped(self):
        assert _normalize_critique({"generic_check_ids": []}, valid_check_ids={"x"}) is None

    def test_id_lock_on_all_lists(self):
        out = _normalize_critique(
            {
                "score": 40,
                "generic_check_ids": ["cro-form-b", "GHOST"],
                "ungrounded_check_ids": ["ALSO-FAKE"],
                "rerun_stages": ["cro-form-b", "GHOST"],
            },
            valid_check_ids={"cro-form-b"},
        )
        assert out is not None
        assert out["generic_check_ids"] == ["cro-form-b"]  # NEG: drop id-lock -> GHOST leaks
        assert out["ungrounded_check_ids"] == []
        assert out["rerun_stages"] == ["cro-form-b"]

    def test_ship_forces_rerun_empty_above_threshold(self):
        out = _normalize_critique(
            {"score": 90, "rerun_stages": ["cro-form-b"]}, valid_check_ids={"cro-form-b"}
        )
        assert out is not None
        # NEG: remove the `score < _SHIP_THRESHOLD` guard -> a high-scoring report still reruns
        assert out["rerun_stages"] == []


class TestCritiqueReport:
    async def test_below_threshold_flags_rerun(self):
        wm = FakeWMClient(
            outputs=[
                {"score": 40, "generic_check_ids": ["cro-form-b"], "rerun_stages": ["cro-form-b"]}
            ]
        )
        critique, _ = await critique_report("REPORT", _SCORES, _FINDINGS, wm=wm)
        assert critique is not None
        assert critique["rerun_stages"] == ["cro-form-b"]


class TestMaybeCritiqueReport:
    async def test_flag_off_is_noop(self):
        result: dict = {"overall_score": 61.0}
        await maybe_critique_report(
            result,
            refs=[_ref_with_rec()],
            findings=_FINDINGS,
            scores=_SCORES,
            include_critic=False,
            wm_client=FakeWMClient(),
        )
        assert "critique" not in result  # byte-identical path

    async def test_bounded_rerun_rewrites_flagged_rec(self):
        ref = _ref_with_rec()
        result: dict = {"overall_score": 61.0, "quick_wins": [ref], "strategic": []}
        wm = FakeWMClient(
            outputs=[
                # call 1: critic flags the rec for rewrite (score below threshold)
                {"score": 40, "generic_check_ids": ["cro-form-b"], "rerun_stages": ["cro-form-b"]},
                # call 2: the writer's rewrite — cites evidence tokens so it is accepted in 1 pass
                {
                    "check_id": "cro-form-b",
                    "fix_prose": "Cut the signup form to just email and password fields.",
                    "rationale": "Fewer fields convert better.",
                    "example": "Remove phone.",
                    "effort": "low",
                },
            ]
        )
        await maybe_critique_report(
            result,
            refs=[ref],
            findings=_FINDINGS,
            scores=_SCORES,
            include_critic=True,
            wm_client=wm,
        )
        assert result["critique"]["score"] == 40
        # Bounded: 1 critic call + <=2 writer passes, no recursion.
        assert len(wm.calls) <= 3
        assert wm.calls[0].worker_name == "audit-report-critic"
        assert any(c.worker_name == "audit-recommendation-writer" for c in wm.calls[1:])
        # The flagged rec was actually rewritten in place.
        assert "signup form" in ref["recommendation"]["fix_prose"]

    async def test_no_rerun_when_ship(self):
        ref = _ref_with_rec()
        result: dict = {"overall_score": 61.0, "quick_wins": [ref], "strategic": []}
        wm = FakeWMClient(outputs=[{"score": 95, "rerun_stages": ["cro-form-b"]}])
        await maybe_critique_report(
            result,
            refs=[ref],
            findings=_FINDINGS,
            scores=_SCORES,
            include_critic=True,
            wm_client=wm,
        )
        assert result["critique"]["rerun_stages"] == []
        assert len(wm.calls) == 1  # shipped -> no writer re-run
        assert ref["recommendation"]["fix_prose"] == "Trim the form."  # untouched

    async def test_fail_soft_swallows_wm_error(self):
        result: dict = {"overall_score": 61.0, "quick_wins": [_ref_with_rec()], "strategic": []}
        wm = FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        await maybe_critique_report(
            result,
            refs=result["quick_wins"],
            findings=_FINDINGS,
            scores=_SCORES,
            include_critic=True,
            wm_client=wm,
        )
        assert "critique" not in result  # NEG: let it propagate -> this test errors
