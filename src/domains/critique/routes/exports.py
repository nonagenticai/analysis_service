"""Critique export endpoints (WS-7): CSV, SARIF, report (markdown/html/json), PDF."""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.domains.critique.schemas import (
    CategoryScoreDetail,
    CritiqueJSONReportResponse,
    CritiqueReportResponse,
    FindingResponse,
)
from src.shared.database.session import get_db

from ._helpers import _get_critique_service, _get_tenant_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/critique/{job_id}/export/csv",
    responses={
        404: {"description": "Critique job not found"},
        409: {"description": "Job not completed"},
        501: {"description": "CSV export feature is not enabled"},
    },
    operation_id="export_critique_csv",
)
async def export_csv(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Export findings as CSV."""
    from src.config import get_settings
    from src.domains.critique.services.export_service import findings_to_csv

    settings = get_settings()
    if not settings.features.export_csv:
        raise HTTPException(status_code=501, detail="CSV export feature is not enabled")

    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="Job not completed",
            headers={"Retry-After": "30"},
        )

    logger.info("CSV export requested: job_id=%s", job_id)
    csv_content = findings_to_csv(job.findings or [], job.url)
    logger.info("CSV export completed: job_id=%s", job_id)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="critique-{job_id[:8]}.csv"'},
    )


@router.get(
    "/critique/{job_id}/export/sarif",
    responses={
        404: {"description": "Critique job not found"},
        409: {"description": "Job not completed"},
        501: {"description": "SARIF export feature is not enabled"},
    },
    operation_id="export_critique_sarif",
)
async def export_sarif(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Export findings as SARIF 2.1.0 JSON."""
    from src.config import get_settings
    from src.domains.critique.services.export_service import findings_to_sarif

    settings = get_settings()
    if not settings.features.export_sarif:
        raise HTTPException(status_code=501, detail="SARIF export feature is not enabled")

    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail="Job not completed",
            headers={"Retry-After": "30"},
        )

    logger.info("SARIF export completed: job_id=%s", job_id)
    return findings_to_sarif(job.findings or [], job.url)


@router.get(
    "/critique/{job_id}/report",
    response_model=None,
    responses={
        404: {"description": "Critique job or report not found"},
        409: {"description": "Job not completed"},
    },
    operation_id="get_critique_report",
)
async def get_critique_report(
    job_id: str,
    request: Request,
    fmt: str = Query(
        default="markdown", alias="format", description="Report format: markdown, html, or json"
    ),
    db: AsyncSession = Depends(get_db),
) -> CritiqueReportResponse | HTMLResponse | CritiqueJSONReportResponse:
    """Get the critique report in Markdown, HTML, or JSON format."""
    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Critique not completed yet (status: {job.status})",
            headers={"Retry-After": "30"},
        )

    if fmt == "json":
        from src.domains.critique.services.html_report_generator import _score_grade
        from src.domains.critique.services.report_generator import (
            _extract_domain,
            overall_is_unscored,
            unscored_categories,
        )

        findings = cast(list[dict[str, Any]], job.findings or [])
        unscored = unscored_categories(findings)
        scores = {c: s for c, s in (job.category_scores or {}).items() if c not in unscored}
        # No scored category => no overall number and no grade (same rule as HTML/PDF/MD).
        overall: float | None = (
            None if overall_is_unscored(scores, unscored) else (job.overall_score or 0.0)
        )

        cat_scores = {}
        for cat, score in scores.items():
            cat_scores[cat] = CategoryScoreDetail(score=score, grade=_score_grade(score))

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        sorted_findings = sorted(
            findings,
            key=lambda x: severity_order.get(x.get("severity", "info"), 5),
        )
        top_issues = [FindingResponse(**f) for f in sorted_findings[:10]]

        from collections import defaultdict

        by_cat: dict[str, list[FindingResponse]] = defaultdict(list)
        for f in findings:
            by_cat[f.get("category", "other")].append(FindingResponse(**f))

        return CritiqueJSONReportResponse(
            url=job.url,
            domain=_extract_domain(job.url),
            overall_score=overall,
            grade=None if overall is None else _score_grade(overall),
            category_scores=cat_scores,
            top_issues=top_issues,
            findings_by_category=dict(by_cat),
            summary=job.summary or "",
            metadata={
                "scan_duration": job.scan_duration,
                "pages_scanned": job.pages_scanned,
                "schema_version": job.schema_version or "2.0",
                "findings_count": len(findings),
                # Categories with findings but no verified evidence: no score, no grade.
                "unscored_categories": unscored,
            },
        )

    if fmt == "html":
        from src.domains.critique.services.html_report_generator import generate_html_report

        html_report = generate_html_report(
            url=job.url,
            findings=job.findings or [],
            scores=job.category_scores or {},
            summary=job.summary or "",
            overall_score=job.overall_score,
            scan_duration=job.scan_duration,
            github_repo=job.github_repo,
            confidence_scores=job.confidence_scores,
        )
        return HTMLResponse(content=html_report)

    if not job.report_markdown:
        raise HTTPException(status_code=404, detail="Report not available")

    return CritiqueReportResponse(
        job_id=job.job_id,
        url=job.url,
        report=job.report_markdown,
    )


@router.get(
    "/critique/{job_id}/report/pdf",
    responses={
        404: {"description": "Critique job not found"},
        409: {"description": "Job not completed"},
        501: {"description": "PDF generation failed"},
    },
    operation_id="get_critique_report_pdf",
)
async def get_critique_report_pdf(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Get the critique report as PDF."""
    service = _get_critique_service(request)
    job = await service.get_job(db, job_id, tenant_id=_get_tenant_id(request))
    if not job:
        raise HTTPException(status_code=404, detail=f"Critique job not found: {job_id}")
    if job.status != "completed":
        raise HTTPException(
            status_code=409,
            detail=f"Critique not completed yet (status: {job.status})",
            headers={"Retry-After": "30"},
        )

    from src.domains.critique.services.html_report_generator import generate_html_report
    from src.domains.critique.services.pdf_generator import html_to_pdf

    html_report = generate_html_report(
        url=job.url,
        findings=job.findings or [],
        scores=job.category_scores or {},
        summary=job.summary or "",
        overall_score=job.overall_score,
        scan_duration=job.scan_duration,
        github_repo=job.github_repo,
    )

    try:
        pdf_bytes = html_to_pdf(html_report)
    except RuntimeError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="critique-{job_id[:8]}.pdf"',
        },
    )
