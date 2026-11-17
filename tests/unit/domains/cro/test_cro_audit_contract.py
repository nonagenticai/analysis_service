"""CRO audit-report contract (design 2026-10-17 §2.2, CRO-G1..G5, §5.3).

Check inventory, the formless-page "not measured" form section, measured page
facts, the experiment catalog and the AI status. Each lever names the break that
would make its assertion fail (NEG), so none is a tautology.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from src.domains.critique.services.report_generator import deduplicate_findings
from src.domains.critique.services.scanners.cro import CROScanner, compute_cro_score
from src.domains.critique.services.scanners.cro.checks import (
    CRO_CHECK_IDS,
    CRO_CHECKS,
    NO_FORM_REASON,
)
from src.domains.critique.services.scanners.cro.experiments import CRO_EXPERIMENTS
from src.domains.critique.services.scanners.cro.facts import build_journey
from src.domains.critique.services.scanners.cro.scoring import SECTION_WEIGHTS
from src.domains.cro.schemas import CroScoreResponse
from src.domains.cro.service import score_cro
from tests.llm_test_doubles import FakeWMClient, RoutedWMClient

_SCN_CRO = Path(__file__).resolve().parents[4] / "src/domains/critique/services/scanners/cro"
_CHECK_ID_LITERAL = re.compile(
    r'"(cro-(?:form|hero|cta|social|valueprop|objection|tech|footer)-[a-z0-9-]+)"'
)

# No <form> at all; weak elsewhere so other sections lose points.
_FORMLESS = (
    "<html><body><p>We do things for teams.</p><a class='btn' href='/go'>Submit</a>"
    "<footer><p>Support: help@acme.test</p></footer></body></html>"
)

# CTAs in document order: link, button, input, verb-link. cta_elements collects
# buttons, then inputs, then links — so a non-sorted inventory would misorder it.
_FORM_PAGE = """
<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body>
<header><nav><a href="/">Home</a></nav></header>
<h1>Cut onboarding time by 40% today</h1>
<a class="btn" href="https://acme.test/start">Start my free trial</a>
<p>No credit card required.</p>
<h2>Talk to sales</h2>
<form id="lead">
  <label for="name">Your name</label><input id="name" name="name" autocomplete="name">
  <input name="email" type="text" placeholder="Email">
  <input name="phone" type="tel" required aria-label="Phone">
  <select name="size"><option>1</option></select>
  <select name="role"><option>x</option></select>
  <textarea name="msg"></textarea>
  <input type="submit" value="Submit">
</form>
<button type="button">Learn more</button>
<a href="https://other.example/docs">Get the guide</a>
<footer><p>Support: help@acme.test</p></footer>
</body></html>
"""


def _findings(html: str) -> list[dict]:
    return deduplicate_findings(CROScanner().scan(html, ""))


def _wm_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://wm.test.invalid/workers/x/message")
    response = httpx.Response(401, request=request)
    return httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)


class TestCheckCatalog:
    def test_every_cro_literal_in_the_scanner_is_catalogued_and_vice_versa(self):
        literals: set[str] = set()
        for path in _SCN_CRO.glob("*.py"):
            if path.name in {"checks.py", "experiments.py"}:
                continue
            literals |= set(_CHECK_ID_LITERAL.findall(path.read_text()))
        # NEG: add a check to a scanner without cataloguing it -> missing from the set.
        assert literals == set(CRO_CHECK_IDS)

    def test_forty_checks_with_the_documented_section_split(self):
        per_section: dict[str, int] = {}
        for spec in CRO_CHECKS:
            per_section[spec.bucket] = per_section.get(spec.bucket, 0) + 1
        assert len(CRO_CHECKS) == len(CRO_CHECK_IDS) == 40
        assert per_section == {
            "form": 8,
            "hero": 6,
            "cta": 4,
            "social": 5,
            "valueprop": 5,
            "objection": 4,
            "tech": 6,
            "footer": 2,
        }
        assert set(per_section) == set(SECTION_WEIGHTS)

    def test_only_form_checks_require_a_form(self):
        for spec in CRO_CHECKS:
            assert (spec.requires == "form") == (spec.bucket == "form"), spec.check_id
            assert spec.title, spec.check_id

    def test_every_experiment_key_is_a_real_check(self):
        # NEG: a typo'd key would silently never attach.
        assert set(CRO_EXPERIMENTS) <= CRO_CHECK_IDS
        for exp in CRO_EXPERIMENTS.values():
            assert exp["hypothesis"] and exp["primary_metric"] and exp["variant"]


class TestFormlessPageIsNotMeasured:
    """CRO-G5: no conversion form => the form section is NOT a clean 100."""

    async def test_form_checks_not_run_and_section_unmeasured(self):
        result = await score_cro(html=_FORMLESS, url=None)
        form_checks = [c for c in result["checks"] if c["section"] == "form"]
        assert len(form_checks) == 8
        # NEG: drop requires="form" -> these read "pass" on a page with no form.
        assert {c["status"] for c in form_checks} == {"not_run"}
        assert {c["reason"] for c in form_checks} == {NO_FORM_REASON}

        row = next(r for r in result["subsections"] if r["section"] == "form")
        assert row["score"] is None
        assert row["measured"] is False
        assert NO_FORM_REASON in row["note"]
        assert result["subsection_scores"]["form"] is None
        assert result["unmeasured_sections"] == ["form"]
        assert result["coverage"] == pytest.approx(0.8)
        CroScoreResponse(**result)  # nullable score validates

    async def test_overall_is_renormalised_over_measured_sections(self):
        findings = _findings(_FORMLESS)
        before = compute_cro_score(findings)["overall_score"]
        result = await score_cro(html=_FORMLESS, url=None)
        measured = {s: v for s, v in result["subsection_scores"].items() if v is not None}
        expected = round(
            sum(v * SECTION_WEIGHTS[s] for s, v in measured.items())
            / sum(SECTION_WEIGHTS[s] for s in measured),
            1,
        )
        assert result["overall_score"] == expected
        # NEG: keep form at a defaulted 100 -> the overall is lifted back to `before`.
        assert result["overall_score"] < before

    async def test_page_with_a_form_measures_the_form(self):
        result = await score_cro(html=_FORM_PAGE, url=None)
        form_checks = [c for c in result["checks"] if c["section"] == "form"]
        assert "not_run" not in {c["status"] for c in form_checks}
        assert result["unmeasured_sections"] == []
        assert result["coverage"] == 1.0
        assert result["subsection_scores"]["form"] is not None
        by_id = {c["check_id"]: c for c in result["checks"]}
        assert by_id["cro-form-field-count"]["status"] == "fail"
        assert by_id["cro-form-field-count"]["measured"] == "6"

    def test_default_compute_cro_score_is_unchanged_for_marketing(self):
        # Marketing calls compute_cro_score(findings) with no `unmeasured`.
        result = compute_cro_score(_findings(_FORMLESS))
        assert result["subsection_scores"]["form"] == 100.0
        assert "unmeasured" not in result
        assert "coverage" not in result


class TestFacts:
    async def test_cta_inventory_is_in_page_order(self):
        result = await score_cro(html=_FORM_PAGE, url="https://acme.test/page")
        ctas = result["facts"]["ctas"]
        assert [c["text"] for c in ctas] == [
            "Start my free trial",
            "Submit",
            "Learn more",
            "Get the guide",
        ]
        assert [c["position_index"] for c in ctas] == [0, 1, 2, 3]
        first, submit, learn, guide = ctas
        assert first["href_kind"] == "internal"
        assert first["first_person"] and first["has_microcopy"] and not first["generic"]
        assert submit["tag"] == "input" and submit["href_kind"] == "form_submit"
        assert submit["generic"] is True
        assert learn["href_kind"] == "button" and learn["generic"] is True
        assert guide["href_kind"] == "external"
        assert result["facts"]["cta_summary"]["count"] == 4
        assert result["facts"]["cta_summary"]["generic_ratio"] == 0.5

    async def test_form_field_table(self):
        result = await score_cro(html=_FORM_PAGE, url=None)
        form = result["facts"]["form"]
        assert form["present"] is True
        assert form["field_count"] == 6
        assert form["required_count"] == 1
        assert form["textareas"] == 1
        assert form["selects"] == 2
        assert form["placeholder_only_labels"] == 1
        assert form["email_type_ok"] is False
        assert form["autocomplete"] is True
        assert form["multistep"] is False
        assert [
            (f["type"], f["label"], f["required"], f["autocomplete"]) for f in form["fields"]
        ] == [
            ("text", "Your name", False, "name"),
            ("text", None, False, None),
            ("tel", "Phone", True, None),
            ("select", None, False, None),
            ("select", None, False, None),
            ("textarea", None, False, None),
        ]
        ids = {f["check_id"] for f in result["findings"]}
        # Facts and checks agree: each fact that says "bad" has its finding.
        assert "cro-form-input-type" in ids
        assert "cro-form-placeholder-label" in ids
        assert "cro-form-multiple-selects" in ids
        assert "cro-form-required-friction" in ids

    async def test_hero_value_prop_and_trust_facts(self):
        result = await score_cro(html=_FORM_PAGE, url=None)
        facts = result["facts"]
        hero = facts["hero"]
        assert hero["h1_text"] == "Cut onboarding time by 40% today"
        assert hero["h1_length"] == len("Cut onboarding time by 40% today")
        assert hero["has_cta_above_fold"] is None  # no layout geometry: never guessed
        assert hero["cta_before_first_h2"] is True
        # <nav> nested in <header> counts twice — the SAME number the
        # nav-distraction check compares (pre-existing scanner behaviour).
        assert hero["nav_link_count"] == 2
        vp = facts["value_prop"]
        assert vp["lead_text"] == "No credit card required."
        assert vp["quantified_benefit"] is True
        assert vp["reading_grade"] is None  # < 120 words
        trust = facts["trust"]
        assert trust["testimonials"] is False
        assert trust["named_testimonials"] is None  # nothing to attribute
        ids = {f["check_id"] for f in result["findings"]}
        for key, check_id in (
            ("testimonials", "cro-social-no-testimonials"),
            ("logos", "cro-social-no-logos"),
            ("ratings", "cro-social-no-ratings"),
            ("faq", "cro-objection-no-faq"),
            ("risk_reversal", "cro-objection-no-risk-reversal"),
            ("contact", "cro-objection-no-legitimacy"),
        ):
            assert trust[key] is (check_id not in ids), key

    async def test_formless_form_facts_are_null_not_zero(self):
        form = (await score_cro(html=_FORMLESS, url=None))["facts"]["form"]
        assert form["present"] is False
        assert form["field_count"] is None
        assert form["fields"] == []

    async def test_journey_maps_sections_to_six_stages(self):
        result = await score_cro(html=_FORMLESS, url=None)
        journey = result["facts"]["journey"]
        assert [s["stage"] for s in journey["stages"]] == [
            "attention",
            "understanding",
            "trust",
            "action",
            "commitment",
            "fallback",
        ]
        commitment = journey["stages"][4]
        assert commitment["score"] is None and commitment["measured"] is False
        assert journey["friction"]["section"] == "tech"
        assert journey["weakest_stage"] is not None
        CroScoreResponse(**result)

    def test_journey_trust_stage_is_weighted_and_weakest_needs_a_loss(self):
        scores = dict.fromkeys(SECTION_WEIGHTS, 100.0)
        assert build_journey(scores)["weakest_stage"] is None
        scores.update(social=40.0, objection=90.0)
        journey = build_journey(scores)
        trust = journey["stages"][2]
        # social 0.12, objection 0.10 -> (40*.12 + 90*.10) / .22
        assert trust["score"] == round((40 * 0.12 + 90 * 0.10) / 0.22, 1)
        assert journey["weakest_stage"] == "trust"

    async def test_a_facts_bug_never_fails_the_score(self, monkeypatch):
        def boom(*_a: object, **_k: object) -> dict:
            raise RuntimeError("facts bug")

        monkeypatch.setattr("src.domains.cro.service.build_cro_facts", boom)
        result = await score_cro(html=_FORM_PAGE, url=None)
        assert result["facts"] == {}
        assert result["overall_score"] > 0
        CroScoreResponse(**result)


class TestExperiments:
    async def test_experiment_attached_only_where_authored(self):
        result = await score_cro(html=_FORMLESS, url=None)
        rows = {f["check_id"]: f for f in result["findings"]}
        authored = [cid for cid in rows if cid in CRO_EXPERIMENTS]
        assert authored, "fixture must fire at least one authored check"
        for cid in authored:
            assert rows[cid]["experiment"] == CRO_EXPERIMENTS[cid]
        for cid, row in rows.items():
            if cid not in CRO_EXPERIMENTS:
                assert "experiment" not in row
        response = CroScoreResponse(**result)
        assert any(f.experiment is not None for f in response.findings)

    async def test_unauthored_check_has_no_experiment(self):
        html = "<html><body><h1>One two three four</h1><h1>Again here now</h1></body></html>"
        result = await score_cro(html=html, url=None)
        row = next(f for f in result["findings"] if f["check_id"] == "cro-hero-multiple-h1")
        assert "cro-hero-multiple-h1" not in CRO_EXPERIMENTS
        assert "experiment" not in row


class TestAiStatus:
    async def test_wm_401_marks_micro_judge_failed_and_ai_unavailable(self):
        wm = FakeWMClient(raise_after="always", error=_wm_401())
        result = await score_cro(html=_FORM_PAGE, url=None, include_llm=True, wm_client=wm)
        ai = result["ai"]
        assert ai["status"] == "unavailable"
        layer = next(entry for entry in ai["layers"] if entry["layer"] == "micro_judge")
        assert layer["status"] == "failed"
        assert "WM 401" in (layer["reason"] or "")
        assert result["llm_judgments"] == []
        assert result["llm_enriched"] is False
        CroScoreResponse(**result)

    async def test_healthy_wm_is_complete_with_judgments(self):
        result = await score_cro(
            html=_FORM_PAGE, url=None, include_llm=True, wm_client=RoutedWMClient()
        )
        assert result["ai"]["status"] == "complete"
        assert result["llm_enriched"] is True
        assert [j["element"] for j in result["llm_judgments"]] == ["headline"]
        CroScoreResponse(**result)

    async def test_not_requested(self):
        result = await score_cro(html=_FORM_PAGE, url=None)
        assert result["ai"]["status"] == "not_requested"
