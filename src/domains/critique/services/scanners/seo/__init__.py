"""SEO scanner package."""

from src.domains.critique.services.scanners.seo.scanner import SEOScanner
from src.domains.critique.services.scanners.seo.scoring import (
    GROUP_WEIGHTS,
    compute_seo_audit_score,
    group_of,
)

__all__ = ["GROUP_WEIGHTS", "SEOScanner", "compute_seo_audit_score", "group_of"]
