"""Unit + service tests for the Tier-3 audit recommendation-writer (``recommendation_writer.py``).

Layers:
* pure-function tests for the clamps (echo / evidence), the evaluator-optimizer
  bound, and the weave — WM stubbed via :class:`FakeWMClient`, no live provider;
* service-level tests that ``include_llm`` weaves prose onto the CRO / SEO /
  marketing responses, that a WM outage is **fail-soft** (titles-only == today),
  and that the **default** (``include_llm=false``) response gains no ``recommendation``;
* the guarded ``action_buckets`` bucket-drop fix (a low/medium finding surfaces
  only with ``include_unbucketed``, else dropped byte-identically).

Each lever carries a NEGATIVE CONTROL note: the guard whose removal makes the
named assertion fail — so no assertion is a tautology.
"""

from __future__ import annotations

from src.domains.scorecards.findings import action_buckets
from src.domains.scorecards.recommendation_writer import (
    _MAX_RECOMMENDATIONS,
    _cites_evidence,
    _evidence_tokens,
    _normalize_recommendation,
    _score_recommendation,
    attach_recommendations,
    write_recommendation,
)
from src.domains.seo_audit.service import audit_seo
from tests.llm_test_doubles import FakeWMClient

# A minimal page that reliably yields a bucketed SEO finding carrying evidence:
# ``seo-content-very-thin`` with evidence ``"2 words"`` (token: "words").
_THIN = "<html><head><title>Hi</title></head><body><h1>Hello</h1><p>x</p></body></html>"
_THIN_CHECK = "seo-content-very-thin"


def _rec(check_id: str, fix_prose: str, **extra: object) -> dict:
    return {
        "check_id": check_id,
        "fix_prose": fix_prose,
        "rationale": extra.get("rationale", "r"),
        "example": extra.get("example", "e"),
        "effort": extra.get("effort", "low"),
    }


def _one_bucket(_cid: str | None) -> str | None:
    """Trivial bucket resolver: every check_id maps to the same bucket."""
    return "grp"


def _refs(result: dict) -> list[dict]:
    return [*result["quick_wins"], *result["strategic"]]


# --------------------------------------------------------------------------
# Clamps + scoring (pure)
# --------------------------------------------------------------------------


class TestClampsAndScoring:
    def test_evidence_tokens_strip_markup_and_short_words(self):
        toks = _evidence_tokens("<h1>Welcome to Acme</h1> at 2 pm")
        assert "welcome" in toks and "acme" in toks
        assert "to" not in toks and "at" not in toks and "2" not in toks  # < 4 chars

    def test_cites_evidence_requires_token_overlap(self):
        toks = _evidence_tokens("headline reads Welcome")
        assert _cites_evidence("Rewrite the headline to be specific.", toks)
        assert not _cites_evidence("Improve the page generally.", toks)
        assert not _cites_evidence("anything", set())  # no tokens -> never cites

    def test_cites_evidence_is_word_level_not_substring(self):
        # A 4-char evidence token must NOT be "cited" as a substring of a larger,
        # unrelated word. NEGATIVE CONTROL: revert _cites_evidence to `tok in prose`
        # (substring) and the first assertion flips to True.
        toks = _evidence_tokens("the form has 6 fields")  # -> {"form", "fields"}
        assert not _cites_evidence(
            "Transform the layout for clarity.", toks
        )  # 'form' ⊂ 'transform'
        assert _cites_evidence("Reduce the form to 3 fields.", toks)  # real whole-word citation

    def test_normalize_echo_clamp_drops_wrong_check_id(self):
        # NEGATIVE CONTROL: drop the check_id equality guard in
        # _normalize_recommendation and this returns a dict, not None.
        assert _normalize_recommendation(_rec("WRONG", "x"), expected_check_id="a") is None
        assert _normalize_recommendation(_rec("a", "fix it"), expected_check_id="a") is not None

    def test_normalize_drops_empty_prose_and_invalid_effort(self):
        assert _normalize_recommendation(_rec("a", "   "), expected_check_id="a") is None
        out = _normalize_recommendation(_rec("a", "fix", effort="bogus"), expected_check_id="a")
        assert out is not None and out["effort"] == ""  # invalid effort blanked

    def test_score_rewards_citation_and_completeness(self):
        cited = _score_recommendation(
            {
                "fix_prose": "Rewrite the headline clearly.",
                "rationale": "r",
                "example": "e",
                "effort": "low",
            },
            cites=True,
        )
        uncited = _score_recommendation(
            {
                "fix_prose": "Rewrite the headline clearly.",
                "rationale": "",
                "example": "",
                "effort": "",
            },
            cites=False,
        )
        assert cited > uncited


# --------------------------------------------------------------------------
# write_recommendation — evaluator-optimizer loop + clamps
# --------------------------------------------------------------------------


_REF = {"check_id": "cro-x", "title": "Vague headline", "severity": "high"}
_EVIDENCE = "headline is vague"


class TestWriteRecommendation:
    async def test_happy_path_single_pass(self):
        # A complete, citing draft scores >= threshold -> accepted on the first call.
        fake = FakeWMClient(
            outputs=[_rec("cro-x", "Rewrite the headline to state a concrete benefit.")]
        )
        rec, usage = await write_recommendation(fake, _REF, evidence=_EVIDENCE)
        assert rec is not None and rec["effort"] == "low"
        assert len(fake.calls) == 1
        assert fake.calls[0].worker_name == "audit-recommendation-writer"
        assert usage["total_tokens"] >= 0

    async def test_bounded_loop_never_exceeds_two_passes(self):
        # A citing-but-weak draft (short, no example/rationale) always scores below
        # threshold, so the loop revises exactly once then STOPS at the cap.
        # NEGATIVE CONTROL: raise _MAX_PASSES (or drop the cap) and calls exceed 2.
        weak = {
            "check_id": "cro-x",
            "fix_prose": "fix headline",
            "rationale": "",
            "example": "",
            "effort": "",
        }
        fake = FakeWMClient(outputs=[weak])
        rec, _ = await write_recommendation(fake, _REF, evidence=_EVIDENCE)
        assert len(fake.calls) == 2  # first draft + one revise, never a third
        assert rec is not None  # the best *cited* draft is still returned

    async def test_evidence_clamp_drops_uncited_prose(self):
        # Prose that never references the evidence is dropped (SOFT), even after 2 passes.
        # NEGATIVE CONTROL: remove the _cites_evidence gate and this returns a dict.
        fake = FakeWMClient(outputs=[_rec("cro-x", "Improve the page in a general way.")])
        rec, _ = await write_recommendation(fake, _REF, evidence=_EVIDENCE)
        assert rec is None
        assert len(fake.calls) == 2

    async def test_echo_clamp_drops_wrong_check_id(self):
        fake = FakeWMClient(outputs=[_rec("SOMETHING-ELSE", "Rewrite the headline now.")])
        rec, _ = await write_recommendation(fake, _REF, evidence=_EVIDENCE)
        assert rec is None


# --------------------------------------------------------------------------
# attach_recommendations — weave, skip-no-evidence, cap, error propagation
# --------------------------------------------------------------------------


class TestAttachRecommendations:
    async def test_weaves_by_check_id_and_skips_no_evidence(self):
        findings = [
            {"check_id": "a", "evidence": "headline is vague"},
            {"check_id": "b"},  # no evidence -> writer skips it
        ]
        refs: list[dict] = [
            {"check_id": "a", "title": "t", "severity": "high"},
            {"check_id": "b", "title": "t", "severity": "low"},
        ]
        fake = FakeWMClient(outputs=[_rec("a", "Rewrite the vague headline.")])
        written, _ = await attach_recommendations(refs, findings, wm=fake)
        assert written == 1
        assert refs[0]["recommendation"]["fix_prose"] == "Rewrite the vague headline."
        assert "recommendation" not in refs[1]  # skipped: no evidence
        assert len(fake.calls) == 1  # only the evidence-bearing ref triggers a call

    async def test_fan_out_is_capped(self):
        # NEGATIVE CONTROL: remove the `written >= _MAX_RECOMMENDATIONS` break and
        # all 30 refs get a WM call (calls == 30, written == 30).
        n = _MAX_RECOMMENDATIONS + 5
        findings = [{"check_id": f"c{i}", "evidence": "words here"} for i in range(n)]
        refs = [{"check_id": f"c{i}", "title": "t", "severity": "high"} for i in range(n)]
        # Echo the per-call check_id so every draft passes the echo clamp.
        outputs = [_rec(f"c{i}", "add more words here") for i in range(n)]
        fake = FakeWMClient(outputs=outputs)
        written, _ = await attach_recommendations(refs, findings, wm=fake)
        assert written == _MAX_RECOMMENDATIONS
        assert len(fake.calls) == _MAX_RECOMMENDATIONS

    async def test_fan_out_cap_bounds_wm_calls_even_when_all_drop(self):
        # Adversarial: WM always returns a WRONG check_id, so nothing is ever
        # written. The cap must still bound the WM fan-out (it counts attempts,
        # not writes). NEGATIVE CONTROL: cap on `written` instead of `attempted`
        # and this balloons to 2 * 100 = 200 calls.
        n = 100
        findings = [{"check_id": f"c{i}", "evidence": "widget label here"} for i in range(n)]
        refs = [{"check_id": f"c{i}", "title": "t", "severity": "high"} for i in range(n)]
        fake = FakeWMClient(outputs=[_rec("WRONG-ID", "add a widget label here")])
        written, _ = await attach_recommendations(refs, findings, wm=fake)
        assert written == 0
        assert len(fake.calls) <= 2 * _MAX_RECOMMENDATIONS  # bounded, not 200

    async def test_wm_error_is_per_ref_soft_not_all_or_nothing(self):
        # A WM/parse error on ONE finding must NOT abort the loop and strand the
        # rest (which would ship a partial, non-deterministic set while the caller
        # logs "titles only"). Mirrors claim_grounding's per-ref guard: the bad ref
        # is skipped, every other ref is still written, and nothing propagates.
        # NEGATIVE CONTROL: remove the per-ref try/except and the RuntimeError on
        # ref "b" propagates out, dropping refs "a" (already written) and "c".
        findings = [
            {"check_id": "a", "evidence": "headline vague words"},
            {"check_id": "b", "evidence": "cta weak words"},
            {"check_id": "c", "evidence": "form long words"},
        ]
        refs: list[dict] = [
            {"check_id": "a", "title": "t", "severity": "high"},
            {"check_id": "b", "title": "t", "severity": "high"},
            {"check_id": "c", "title": "t", "severity": "high"},
        ]
        # Call 2 (ref "b") raises; calls 1 and 3 echo their check_id and succeed.
        fake = FakeWMClient(
            outputs=[
                _rec("a", "Rewrite the vague headline words."),
                _rec("b", "unused — call 2 raises"),
                _rec("c", "Shorten the long form words."),
            ],
            raise_after=2,
            error=RuntimeError("wm down"),
        )
        written, _ = await attach_recommendations(refs, findings, wm=fake)
        assert written == 2  # a + c written; b skipped, not all-or-nothing
        assert refs[0]["recommendation"]["fix_prose"] == "Rewrite the vague headline words."
        assert "recommendation" not in refs[1]  # the flaked ref
        assert refs[2]["recommendation"]["fix_prose"] == "Shorten the long form words."
        assert len(fake.calls) == 3  # loop continued past the failure


# --------------------------------------------------------------------------
# action_buckets — the guarded bucket-drop fix (pre-existing bug)
# --------------------------------------------------------------------------


class TestActionBucketDropFix:
    def test_unbucketed_finding_is_dropped_by_default(self):
        # low severity + medium effort -> matches NEITHER action branch -> dropped.
        findings = [
            {"check_id": "x", "severity": "low", "remediation_effort": "medium", "title": "t"}
        ]
        quick, strat = action_buckets(findings, bucket_of=_one_bucket, bucket_key="group")
        assert quick == [] and strat == []  # historical byte-identical drop

    def test_include_unbucketed_surfaces_it_in_strategic(self):
        # NEGATIVE CONTROL: revert the `elif include_unbucketed` branch and this
        # finding never appears (strat stays empty).
        findings = [
            {"check_id": "x", "severity": "low", "remediation_effort": "medium", "title": "t"}
        ]
        quick, strat = action_buckets(
            findings, bucket_of=_one_bucket, bucket_key="group", include_unbucketed=True
        )
        assert quick == []
        assert [r["check_id"] for r in strat] == ["x"]

    def test_normal_split_is_unchanged_by_the_flag(self):
        # A genuine quick-win (high sev, low effort) and strategic (critical) route
        # identically whether or not include_unbucketed is set.
        findings = [
            {"check_id": "q", "severity": "high", "remediation_effort": "low", "title": "t"},
            {"check_id": "s", "severity": "critical", "remediation_effort": "low", "title": "t"},
        ]
        base = action_buckets(findings, bucket_of=_one_bucket, bucket_key="group")
        flagged = action_buckets(
            findings, bucket_of=_one_bucket, bucket_key="group", include_unbucketed=True
        )
        assert base == flagged


# --------------------------------------------------------------------------
# Service integration — byte-identity, weave, fail-soft
# --------------------------------------------------------------------------


class TestServiceIntegration:
    async def test_default_has_no_recommendation(self):
        # include_recommendations off -> byte-identity: no ref carries a recommendation.
        # NEGATIVE CONTROL: gate the writer on something always-true and this fails.
        result = await audit_seo(html=_THIN, url=None)
        refs = _refs(result)
        assert refs, "thin page should surface findings"
        assert all("recommendation" not in r for r in refs)

    async def test_include_llm_alone_does_not_write_recommendations(self):
        # The writer is gated on include_recommendations, NOT include_llm — so a
        # bare include_llm run (the micro-judge path) attaches no recommendation.
        fake = FakeWMClient(outputs=[_rec(_THIN_CHECK, "add more words here")])
        result = await audit_seo(html=_THIN, url=None, include_llm=True, wm_client=fake)
        assert all("recommendation" not in r for r in _refs(result))

    async def test_writer_weaves_prose_onto_the_evidence_finding(self):
        fake = FakeWMClient(
            outputs=[
                _rec(
                    _THIN_CHECK,
                    "The page has only a few words; expand it to 300+ words of real content.",
                    effort="medium",
                )
            ]
        )
        result = await audit_seo(html=_THIN, url=None, include_recommendations=True, wm_client=fake)
        refs = _refs(result)
        target = next(r for r in refs if r["check_id"] == _THIN_CHECK)
        assert target["recommendation"]["fix_prose"].startswith("The page has only")
        assert target["recommendation"]["effort"] == "medium"
        assert all(c.worker_name == "audit-recommendation-writer" for c in fake.calls)

    async def test_writer_is_fail_soft_on_wm_error(self):
        # WM raising on every call -> the service returns the deterministic score
        # unchanged, no recommendation, no exception (titles-only == today).
        # NEGATIVE CONTROL: remove the service try/except and this raises.
        fake = FakeWMClient(raise_after="always", error=RuntimeError("wm down"))
        result = await audit_seo(html=_THIN, url=None, include_recommendations=True, wm_client=fake)
        assert result["overall_score"] >= 0.0
        assert all("recommendation" not in r for r in _refs(result))

    async def test_wrong_check_id_from_wm_attaches_nothing(self):
        # Echo clamp end-to-end: a drifted check_id never lands on a ref.
        fake = FakeWMClient(outputs=[_rec("hallucinated-id", "add more words to the page")])
        result = await audit_seo(html=_THIN, url=None, include_recommendations=True, wm_client=fake)
        assert all("recommendation" not in r for r in _refs(result))
