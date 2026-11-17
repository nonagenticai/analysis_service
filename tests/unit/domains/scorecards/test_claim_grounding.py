"""Unit tests for the audit claim-grounding worker (A18, SOFT gate).

WM is stubbed via :class:`FakeWMClient` — no live provider. Each lever has an
executing assertion plus a documented negative-control line (neuter it in the
source and the named assertion flips), so none of these is a tautology.
"""

from __future__ import annotations

from src.domains.scorecards.claim_grounding import (
    _normalize_grounding,
    ground_claims,
    maybe_ground_claims,
)
from tests.llm_test_doubles import FakeWMClient


def _ref(check_id: str = "cro-form-a", *, with_rec: bool = True) -> dict:
    ref: dict = {"check_id": check_id, "title": "Form has too many fields", "severity": "high"}
    if with_rec:
        ref["recommendation"] = {
            "fix_prose": "Cut the signup form from 6 inputs to 2 (email + password).",
            "rationale": "Fewer fields lift completion.",
            "example": "Remove phone/company/address.",
            "effort": "low",
        }
    return ref


_PAGE = "Sign up form with email password phone company address fields on the pricing page."


class TestNormalizeGrounding:
    def test_echo_clamp_drops_wrong_check_id(self):
        out = _normalize_grounding(
            {"check_id": "SOMETHING-ELSE", "supported": True}, expected_check_id="cro-form-a"
        )
        assert out is None  # NEG: remove the echo clamp -> this returns a verdict

    def test_non_bool_supported_coerces_false_safe(self):
        out = _normalize_grounding(
            {"check_id": "cro-form-a", "supported": "nonsense"}, expected_check_id="cro-form-a"
        )
        assert out is not None
        assert out["supported"] is False  # malformed verdict fails safe (drop the rec)


class TestGroundClaims:
    async def test_supported_keeps_recommendation(self):
        ref = _ref()
        wm = FakeWMClient(
            outputs=[{"check_id": "cro-form-a", "supported": True, "evidence_ref": "form"}]
        )
        results, _ = await ground_claims([ref], page_text=_PAGE, wm=wm)
        assert results == [{"check_id": "cro-form-a", "supported": True, "evidence_ref": "form"}]
        assert "recommendation" in ref  # supported -> rec survives

    async def test_unsupported_drops_recommendation_soft(self):
        ref = _ref()
        wm = FakeWMClient(
            outputs=[{"check_id": "cro-form-a", "supported": False, "evidence_ref": ""}]
        )
        results, _ = await ground_claims([ref], page_text=_PAGE, wm=wm)
        # SOFT gate: verdict recorded, ungrounded rec dropped, NO exception raised.
        assert results[0]["supported"] is False
        assert "recommendation" not in ref  # NEG: remove the `ref.pop(...)` -> rec leaks

    async def test_skips_refs_without_recommendation(self):
        ref = _ref(with_rec=False)
        wm = FakeWMClient(outputs=[{"check_id": "cro-form-a", "supported": True}])
        results, _ = await ground_claims([ref], page_text=_PAGE, wm=wm)
        assert results == []
        assert wm.calls == []  # no WM call for a ref with nothing to ground

    async def test_midloop_failure_is_per_ref_soft_not_all_or_nothing(self):
        # Regression (adversarial finding): if an EARLIER ref is dropped as unsupported
        # and a LATER ref's WM call raises, ground_claims must NOT propagate — it must
        # keep the earlier (correct) verdict, leave the failed ref's rec intact, and
        # return the partial grounding list. Never strip-and-vanish.
        a = _ref("cro-form-a")
        b = _ref("cro-hero-b")
        wm = FakeWMClient(
            outputs=[{"check_id": "cro-form-a", "supported": False, "evidence_ref": ""}],
            raise_after=2,  # ref A's call succeeds (unsupported), ref B's call raises
        )
        results, _ = await ground_claims([a, b], page_text=_PAGE, wm=wm)
        assert "recommendation" not in a  # A was legitimately judged unsupported -> dropped
        assert "recommendation" in b  # B's check failed -> left exactly as the writer made it
        assert [r["check_id"] for r in results] == ["cro-form-a"]  # partial list surfaced, no crash

    async def test_fanout_capped(self):
        refs = [_ref(f"cro-{i}") for i in range(40)]
        wm = FakeWMClient(outputs=[{"check_id": "cro-0", "supported": True}])
        await ground_claims(refs, page_text=_PAGE, wm=wm)
        assert len(wm.calls) <= 25  # _MAX_GROUNDINGS


class TestMaybeGroundClaims:
    async def test_flag_off_is_noop(self):
        ref = _ref()
        out = await maybe_ground_claims(
            [ref], include_grounding=False, wm_client=FakeWMClient(), page_text=_PAGE
        )
        assert out is None
        assert "recommendation" in ref  # untouched (byte-identical path)

    async def test_no_recommendations_is_noop(self):
        ref = _ref(with_rec=False)
        wm = FakeWMClient(outputs=[{"check_id": "cro-form-a", "supported": True}])
        out = await maybe_ground_claims(
            [ref], include_grounding=True, wm_client=wm, page_text=_PAGE
        )
        assert out is None
        assert wm.calls == []

    async def test_fail_soft_swallows_wm_error(self):
        ref = _ref()
        wm = FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        # Must NOT raise; returns None; the recommendation is left as the writer made it.
        out = await maybe_ground_claims(
            [ref], include_grounding=True, wm_client=wm, page_text=_PAGE
        )
        assert out is None
        assert "recommendation" in ref  # NEG: let the exception propagate -> this test errors
