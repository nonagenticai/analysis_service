"""CRO (conversion-rate-optimization) scanner package."""

from src.domains.critique.services.scanners.cro.scanner import CROScanner
from src.domains.critique.services.scanners.cro.scoring import (
    SECTION_WEIGHTS,
    compute_cro_score,
)

__all__ = ["SECTION_WEIGHTS", "CROScanner", "compute_cro_score"]
