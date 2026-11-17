"""Full findings + check inventory builders (design 2026-10-17 X-G1 / X-G2)."""

from __future__ import annotations

from src.domains.scorecards.findings import CheckSpec, build_checks, build_findings

_WEIGHTS = {"a": 0.6, "b": 0.4}
_SEV = {"critical": 10, "high": 6, "medium": 3, "low": 1, "info": 0}


def _bucket(cid: str | None) -> str | None:
    if not cid:
        return None
    return cid.split("-")[1] if cid.count("-") >= 2 else None


def _f(cid: str, sev: str, effort: str = "low") -> dict:
    return {
        "check_id": cid,
        "title": f"title {cid}",
        "severity": sev,
        "description": f"desc {cid}",
        "recommendation": f"fix {cid}",
        "remediation_effort": effort,
        "evidence": f"ev {cid}",
        "selector": None,
    }


class TestBuildFindings:
    def test_keeps_low_and_info_that_action_buckets_drop(self):
        findings = [_f("x-a-1", "high"), _f("x-a-2", "low"), _f("x-b-3", "info")]
        rows = build_findings(
            findings, bucket_of=_bucket, bucket_key="group", weights=_WEIGHTS, severity_weights=_SEV
        )
        # NEG: reuse action_buckets' severity filter -> the low/info rows vanish.
        assert {r["check_id"] for r in rows} == {"x-a-1", "x-a-2", "x-b-3"}

    def test_carries_description_and_deterministic_fix(self):
        rows = build_findings(
            [_f("x-a-1", "high")],
            bucket_of=_bucket,
            bucket_key="group",
            weights=_WEIGHTS,
            severity_weights=_SEV,
        )
        assert rows[0]["description"] == "desc x-a-1"
        assert rows[0]["fix"] == "fix x-a-1"
        assert rows[0]["group"] == "a"
        assert rows[0]["effort"] == "low"

    def test_rank_is_total_and_deterministic(self):
        findings = [_f("x-b-2", "high"), _f("x-a-1", "critical"), _f("x-a-3", "high")]
        rows = build_findings(
            findings, bucket_of=_bucket, bucket_key="group", weights=_WEIGHTS, severity_weights=_SEV
        )
        assert [r["rank"] for r in rows] == [1, 2, 3]
        assert rows[0]["check_id"] == "x-a-1"

    def test_action_bucket_and_recommendation_copied_from_refs(self):
        findings = [_f("x-a-1", "high"), _f("x-a-2", "critical"), _f("x-a-3", "low")]
        quick = [{"check_id": "x-a-1", "recommendation": {"fix_prose": "do it"}}]
        strategic = [{"check_id": "x-a-2"}]
        rows = {
            r["check_id"]: r
            for r in build_findings(
                findings,
                bucket_of=_bucket,
                bucket_key="group",
                weights=_WEIGHTS,
                severity_weights=_SEV,
                quick_wins=quick,
                strategic=strategic,
            )
        }
        assert rows["x-a-1"]["action_bucket"] == "quick_win"
        assert rows["x-a-1"]["recommendation"] == {"fix_prose": "do it"}
        assert rows["x-a-2"]["action_bucket"] == "strategic"
        assert rows["x-a-3"]["action_bucket"] == "none"
        assert "recommendation" not in rows["x-a-3"]

    def test_keep_filters_buckets(self):
        rows = build_findings(
            [_f("x-a-1", "high"), _f("x-b-2", "high")],
            bucket_of=_bucket,
            bucket_key="dimension",
            weights=_WEIGHTS,
            severity_weights=_SEV,
            keep=lambda b: b == "b",
        )
        assert [r["check_id"] for r in rows] == ["x-b-2"]

    def test_dedupes_by_check_id(self):
        rows = build_findings(
            [_f("x-a-1", "high"), _f("x-a-1", "high")],
            bucket_of=_bucket,
            bucket_key="group",
            weights=_WEIGHTS,
            severity_weights=_SEV,
        )
        assert len(rows) == 1


_CATALOG = [
    CheckSpec("x-a-1", "a", "Title present"),
    CheckSpec("x-a-2", "a", "robots.txt reachable", requires="url"),
    CheckSpec("x-b-3", "b", "No orphans", requires="crawl"),
    CheckSpec("x-b-4", "b", "Clean"),
]


class TestBuildChecks:
    def test_pass_fail_not_run(self):
        rows = {
            r["check_id"]: r
            for r in build_checks(
                _CATALOG,
                [_f("x-a-1", "high")],
                bucket_key="group",
                available={"crawl"},
                not_run_reasons={"url": "no URL — inline HTML cannot fetch robots.txt"},
            )
        }
        assert rows["x-a-1"]["status"] == "fail"
        assert rows["x-a-1"]["severity"] == "high"
        # NEG: ignore `requires` -> a never-fetched robots.txt reads as "pass".
        assert rows["x-a-2"]["status"] == "not_run"
        assert "robots.txt" in str(rows["x-a-2"]["reason"])
        assert rows["x-b-3"]["status"] == "pass"
        assert rows["x-b-4"]["status"] == "pass"

    def test_unmeasured_bucket_is_not_run_even_without_requirement(self):
        rows = {
            r["check_id"]: r
            for r in build_checks(
                _CATALOG,
                [],
                bucket_key="group",
                available={"url", "crawl"},
                not_run_buckets={"b": "crawl timed out"},
            )
        }
        assert rows["x-b-4"]["status"] == "not_run"
        assert rows["x-b-4"]["reason"] == "crawl timed out"

    def test_uncatalogued_failure_is_still_reported(self):
        rows = build_checks(
            _CATALOG,
            [_f("x-a-schema-product-incomplete", "medium")],
            bucket_key="group",
            available={"url", "crawl"},
            bucket_of=lambda _cid: "a",
        )
        extra = [r for r in rows if r["check_id"] == "x-a-schema-product-incomplete"]
        # NEG: only iterate the catalog -> a dynamic-id failure disappears.
        assert extra and extra[0]["status"] == "fail" and extra[0]["group"] == "a"

    def test_measured_and_target_surface(self):
        rows = build_checks(
            [CheckSpec("x-a-1", "a", "Title length", target="50-60 chars")],
            [],
            bucket_key="group",
            measured={"x-a-1": "42 chars"},
        )
        assert rows[0]["measured"] == "42 chars"
        assert rows[0]["target"] == "50-60 chars"
