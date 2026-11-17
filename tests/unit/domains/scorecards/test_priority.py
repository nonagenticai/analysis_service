"""Unit + service tests for the Tier-1 deterministic audit compute (``priority.py``).

Two layers:
* pure-function tests for ``priority_score`` / ``rank_findings`` / ``project_delta``
  / ``cap_depth`` / ``enrich_refs`` — no I/O, fully deterministic;
* service-level tests that the opt-in flags enrich the CRO / SEO / marketing
  responses and that the **default** (flags-off) response is byte-identical (none
  of the new fields appear).

Each lever carries a negative-control assertion (the shape that would fail if the
guard were neutered) — see the module-level comments on the individual tests.
"""

from __future__ import annotations

from src.domains.critique.services.report_generator import _get_severity_weights
from src.domains.critique.services.scanners.cro.scoring import (
    SECTION_WEIGHTS,
    _section_of,
    compute_cro_score,
)
from src.domains.cro.service import score_cro
from src.domains.marketing.service import score_marketing
from src.domains.scorecards.priority import (
    EFFORT_DIVISOR,
    cap_depth,
    enrich_refs,
    priority_score,
    project_delta,
    rank_findings,
)
from src.domains.seo_audit.service import audit_seo

_SEV = _get_severity_weights()  # {critical:30, high:15, medium:7, low:3, info:0}


def _f(check_id: str, severity: str, effort: str = "low", **extra: object) -> dict:
    # category defaults to the check_id namespace prefix (``cro-…`` → ``cro``);
    # override via ``extra`` when a test needs a specific category.
    return {
        "check_id": check_id,
        "category": check_id.split("-", 1)[0],
        "severity": severity,
        "remediation_effort": effort,
        **extra,
    }


# --------------------------------------------------------------------------
# priority_score
# --------------------------------------------------------------------------


class TestPriorityScore:
    def test_formula(self):
        # hero weight 0.18, high sev = 15, medium effort divisor 2.
        f = _f("cro-hero-x", "high", "medium")
        expected = round(_SEV["high"] * SECTION_WEIGHTS["hero"] / EFFORT_DIVISOR["medium"], 6)
        got = priority_score(
            f, weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV
        )
        assert got == expected == round(15 * 0.18 / 2.0, 6)

    def test_effort_lowers_priority(self):
        base = _f("cro-hero-x", "high", "low")
        pricey = _f("cro-hero-x", "high", "high")
        p_base = priority_score(
            base, weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV
        )
        p_pricey = priority_score(
            pricey, weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV
        )
        assert p_pricey < p_base  # a costlier fix of equal impact ranks lower

    def test_unknown_bucket_and_severity_are_safe(self):
        f = _f("unknown-check", "bogus", "weird")
        assert (
            priority_score(f, weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV)
            == 0.0
        )


# --------------------------------------------------------------------------
# rank_findings — deterministic tie-break
# --------------------------------------------------------------------------


class TestRankFindings:
    def test_orders_by_priority_desc(self):
        findings = [
            _f("cro-footer-a", "low"),  # weight 0.05
            _f("cro-form-b", "critical"),  # weight 0.20
            _f("cro-hero-c", "high"),  # weight 0.18
        ]
        ranked = rank_findings(
            findings, weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV
        )
        assert [r[1]["check_id"] for r in ranked] == ["cro-form-b", "cro-hero-c", "cro-footer-a"]
        assert [r[0] for r in ranked] == [1, 2, 3]

    def test_tie_break_is_check_id_and_stable(self):
        # Two findings with IDENTICAL priority (same bucket/severity/effort) must
        # order by check_id ascending — a total, reproducible order.
        # NEGATIVE CONTROL: drop the ``check_id`` secondary key in rank_findings
        # (sort key ``(-score,)``) and this assertion no longer holds for a list
        # presented in the reverse order.
        a = _f("cro-hero-aaa", "high")
        b = _f("cro-hero-zzz", "high")
        forward = rank_findings(
            [a, b], weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV
        )
        reverse = rank_findings(
            [b, a], weights=SECTION_WEIGHTS, bucket_of=_section_of, severity_weights=_SEV
        )
        assert [r[1]["check_id"] for r in forward] == ["cro-hero-aaa", "cro-hero-zzz"]
        assert [r[1]["check_id"] for r in reverse] == ["cro-hero-aaa", "cro-hero-zzz"]


# --------------------------------------------------------------------------
# project_delta — real exp-decay scorer, NON-additive
# --------------------------------------------------------------------------


def _cro_overall(fs: list[dict]) -> float:
    return compute_cro_score(fs)["overall_score"]


class TestProjectDelta:
    def test_matches_hand_recompute(self):
        findings = [_f("cro-hero-x", "critical"), _f("cro-hero-y", "critical")]
        proj = project_delta(_cro_overall, findings, findings)
        assert proj["baseline"] == round(_cro_overall(findings), 1)
        assert proj["projected"] == round(_cro_overall([]), 1) == 100.0
        assert proj["delta"] == round(proj["projected"] - proj["baseline"], 1)
        assert proj["removed"] == ["cro-hero-x", "cro-hero-y"]

    def test_delta_is_non_additive(self):
        # Two criticals in the SAME bucket. Under exponential decay the combined
        # delta is strictly greater than the sum of the individual deltas.
        # NEGATIVE CONTROL: if project_delta summed per-finding deltas instead of
        # removing the set together, ``combined`` would EQUAL ``sum_singles`` and
        # this assertion would fail.
        x = _f("cro-hero-x", "critical")
        y = _f("cro-hero-y", "critical")
        findings = [x, y]
        combined = project_delta(_cro_overall, findings, [x, y])["delta"]
        single_x = project_delta(_cro_overall, findings, [x])["delta"]
        single_y = project_delta(_cro_overall, findings, [y])["delta"]
        assert combined != round(single_x + single_y, 1)
        assert combined > round(single_x + single_y, 1)

    def test_fields_are_mutually_consistent_even_for_fine_grained_scorer(self):
        # baseline + delta must equal projected even if a scorer returns numbers
        # finer than 1 dp that straddle a rounding boundary (90.04 → 90.06). Prior
        # to rounding baseline/projected first, delta was computed off the raw
        # values and the three fields could disagree.
        a = {"check_id": "cro-hero-x", "category": "cro", "severity": "high"}
        scores = iter([90.06, 90.04])  # base (all), then projected (kept)

        def _fine(_fs: list[dict]) -> float:
            return next(scores)

        proj = project_delta(_fine, [a], [a])
        assert proj["baseline"] == 90.1
        assert proj["projected"] == 90.0
        assert round(proj["baseline"] + proj["delta"], 1) == proj["projected"]

    def test_removal_is_by_identity_not_check_id(self):
        # Two distinct findings sharing an empty check_id: removing one must keep
        # the other (identity), not drop both by a value match.
        a = {"check_id": "", "category": "cro", "severity": "high", "remediation_effort": "low"}
        b = {"check_id": "", "category": "cro", "severity": "high", "remediation_effort": "low"}
        seen: list[int] = []

        def _fake(fs: list[dict]) -> float:
            seen.append(len(fs))
            return float(len(fs))

        proj = project_delta(_fake, [a, b], [a])
        assert seen == [2, 1]  # scorer called on all(2) then kept(1)
        assert proj["baseline"] == 2.0 and proj["projected"] == 1.0


# --------------------------------------------------------------------------
# cap_depth
# --------------------------------------------------------------------------


class TestCapDepth:
    def test_caps_per_bucket(self):
        refs = [
            {"section": "hero", "check_id": "a"},
            {"section": "hero", "check_id": "b"},
            {"section": "hero", "check_id": "c"},
            {"section": "form", "check_id": "d"},
        ]
        # NEGATIVE CONTROL: bypass cap_depth (return refs unchanged) and the length
        # assertion below fails (3 hero refs survive, not 2).
        capped = cap_depth(refs, bucket_key="section", cap=2)
        assert len(capped) == 3
        assert [r["check_id"] for r in capped] == ["a", "b", "d"]

    def test_cap_zero_is_noop(self):
        refs = [{"section": "hero", "check_id": "a"}]
        assert cap_depth(refs, bucket_key="section", cap=0) == refs


# --------------------------------------------------------------------------
# enrich_refs — additive, opt-in
# --------------------------------------------------------------------------


class TestEnrichRefs:
    def _findings_and_refs(self) -> tuple[list[dict], list[dict], list[dict]]:
        findings = [
            _f("cro-form-a", "critical", "high", evidence="6 countable fields", selector="form#a"),
            _f("cro-hero-b", "high", "low"),
        ]
        quick_wins = [
            {"check_id": "cro-hero-b", "section": "hero", "title": "t", "severity": "high"}
        ]
        strategic = [
            {"check_id": "cro-form-a", "section": "form", "title": "t", "severity": "critical"}
        ]
        return findings, quick_wins, strategic

    def _kwargs(self) -> dict:
        return {
            "weights": SECTION_WEIGHTS,
            "bucket_of": _section_of,
            "bucket_key": "section",
            "severity_weights": _SEV,
            "score_fn": _cro_overall,
        }

    def test_all_flags_off_is_noop(self):
        # NEGATIVE CONTROL: this is the byte-identity guard. If enrich_refs attached
        # anything unconditionally, the "no new keys" assertion fails.
        findings, qw, strat = self._findings_and_refs()
        out_qw, out_strat, projection = enrich_refs(qw, strat, findings, **self._kwargs())
        assert out_qw is qw and out_strat is strat  # same objects
        assert projection is None
        for ref in (*qw, *strat):
            assert not ({"priority", "rank", "evidence", "selector"} & set(ref))

    def test_include_priority_attaches_rank_and_priority(self):
        findings, qw, strat = self._findings_and_refs()
        enrich_refs(qw, strat, findings, include_priority=True, **self._kwargs())
        # ROI-aware: the low-effort hero-high fix (15*0.18/1 = 2.7) outranks the
        # HIGH-effort form-critical (30*0.20/3 = 2.0) — effort is the divisor.
        assert qw[0]["rank"] == 1
        assert strat[0]["rank"] == 2
        assert qw[0]["priority"] > strat[0]["priority"]
        assert qw[0]["priority"] == round(15 * 0.18 / 1.0, 6)
        assert strat[0]["priority"] == round(30 * 0.20 / 3.0, 6)

    def test_include_evidence_surfaces_only_existing(self):
        findings, qw, strat = self._findings_and_refs()
        enrich_refs(qw, strat, findings, include_evidence=True, **self._kwargs())
        assert strat[0]["evidence"] == "6 countable fields"
        assert strat[0]["selector"] == "form#a"
        # hero-b has no evidence → key stays absent (not None).
        assert "evidence" not in qw[0]

    def test_include_projection_present(self):
        findings, qw, strat = self._findings_and_refs()
        _, _, projection = enrich_refs(
            qw, strat, findings, include_projection=True, projection_top_k=2, **self._kwargs()
        )
        assert projection is not None
        assert projection["delta"] == round(projection["projected"] - projection["baseline"], 1)
        assert set(projection["removed"]) == {"cro-form-a", "cro-hero-b"}


# --------------------------------------------------------------------------
# Service-level: byte-identity default + opt-in enrichment (CRO/SEO/marketing)
# --------------------------------------------------------------------------

_WEAK = "<html><body><p>no headline</p><form><input><input><input><input><input><input></form></body></html>"

_NEW_REF_KEYS = {"priority", "rank", "evidence", "selector"}


def _all_refs(result: dict) -> list[dict]:
    return [*result.get("quick_wins", []), *result.get("strategic", [])]


# PR3 additive top-level keys — must be ABSENT on the default (byte-identical) path.
_NEW_TOP_KEYS = {"grounding", "exec_summary", "critique"}


class TestServiceByteIdentity:
    async def test_cro_default_has_no_new_fields(self):
        result = await score_cro(html=_WEAK, url=None)
        assert "projection" not in result
        assert not (_NEW_TOP_KEYS & set(result))
        assert all(not (_NEW_REF_KEYS & set(r)) for r in _all_refs(result))

    async def test_seo_default_has_no_new_fields(self):
        result = await audit_seo(html=_WEAK, url=None)
        assert "projection" not in result
        assert not (_NEW_TOP_KEYS & set(result))
        assert all(not (_NEW_REF_KEYS & set(r)) for r in _all_refs(result))

    async def test_marketing_default_has_no_new_fields(self):
        result = await score_marketing(html=_WEAK, url=None)
        assert "projection" not in result
        assert not (_NEW_TOP_KEYS & set(result))
        assert all(not (_NEW_REF_KEYS & set(r)) for r in _all_refs(result))


class TestServiceEnrichment:
    async def test_cro_priority_and_projection(self):
        result = await score_cro(
            html=_WEAK, url=None, include_priority=True, include_projection=True
        )
        refs = _all_refs(result)
        assert refs, "weak page should surface findings"
        assert all("rank" in r and "priority" in r for r in refs)
        # ranks are a contiguous 1..N with no duplicates (total order).
        ranks = sorted(r["rank"] for r in refs)
        assert ranks == list(range(1, len(ranks) + 1))
        assert result["projection"]["delta"] >= 0.0

    async def test_marketing_priority_composite_scorer(self):
        # Exercises the composite (_marketing_overall) projection path end-to-end.
        result = await score_marketing(
            html=_WEAK, url=None, include_priority=True, include_projection=True
        )
        assert all("rank" in r for r in _all_refs(result))
        assert result["projection"]["projected"] >= result["projection"]["baseline"]

    async def test_depth_cap_limits_per_bucket(self):
        capped = await score_cro(html=_WEAK, url=None, include_priority=True, bucket_depth_cap=1)
        for bucket_list in (capped["quick_wins"], capped["strategic"]):
            seen: dict[str, int] = {}
            for r in bucket_list:
                seen[r["section"]] = seen.get(r["section"], 0) + 1
            assert all(n <= 1 for n in seen.values())
