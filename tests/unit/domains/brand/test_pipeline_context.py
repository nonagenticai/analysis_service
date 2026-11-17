"""Unit tests for pipeline context."""

from src.domains.brand.pipeline.context import PipelineContext
from src.domains.brand.services.extraction_config import BrandExtractConfig


class TestPipelineContext:
    def test_default_values(self) -> None:
        ctx = PipelineContext()
        assert ctx.url == ""
        assert ctx.tenant_id == ""
        assert ctx.html == ""
        assert ctx.soup is None
        assert ctx.download_logo is False
        assert ctx.include_fonts is True
        assert ctx.include_colors is True
        assert ctx.include_metadata is True
        assert ctx.background_color == "#ffffff"
        assert ctx.schema_org_dict is None
        assert ctx.css_urls == []
        assert ctx.http_client is None
        assert ctx.logo_service is None

    def test_with_values(self) -> None:
        cfg = BrandExtractConfig(timeout=5.0)
        ctx = PipelineContext(
            url="https://example.com", tenant_id="7eed672d-a7ff-5f16-87d7-b6a493fee523", config=cfg
        )
        assert ctx.url == "https://example.com"
        assert ctx.tenant_id == "7eed672d-a7ff-5f16-87d7-b6a493fee523"
        assert ctx.config.timeout == 5.0

    def test_tracker_initialized(self) -> None:
        ctx = PipelineContext()
        ctx.tracker.record("test", "field", "value")
        assert ctx.tracker.result_count() == 1
