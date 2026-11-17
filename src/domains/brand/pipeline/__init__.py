"""Pipeline package for brand extraction."""

from src.domains.brand.pipeline.base import BaseStage
from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.pipeline.runner import PipelineRunner, extract_from_analysis
from src.domains.brand.pipeline.types import StageIssue, StageResult

__all__ = [
    "BaseStage",
    "PipelineContext",
    "PipelineRunner",
    "StageIssue",
    "StageResult",
    "extract_from_analysis",
]
