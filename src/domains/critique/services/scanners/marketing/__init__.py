"""Marketing scorecard scanner package (competitive / brand / growth dimensions)."""

from src.domains.critique.services.scanners.marketing.scanner import MarketingScanner
from src.domains.critique.services.scanners.marketing.scoring import (
    DIMENSION_WEIGHTS,
    compute_marketing_score,
    dimension_of,
)

__all__ = [
    "DIMENSION_WEIGHTS",
    "MarketingScanner",
    "compute_marketing_score",
    "dimension_of",
]
