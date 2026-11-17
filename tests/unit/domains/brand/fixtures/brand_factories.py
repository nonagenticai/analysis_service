"""Factory functions for creating test data."""

from src.domains.brand.schemas import AnalysisData, PageData, SiteData


def make_page(url: str = "https://example.com", title: str = "Example", **kwargs) -> PageData:
    """Create a test PageData."""
    return PageData(url=url, title=title, **kwargs)


def make_site(
    description: str = "",
    logo_url: str = "",
    favicon: str = "",
    metadata: dict | None = None,
    visual_identity: dict | None = None,
) -> SiteData:
    """Create a test SiteData."""
    return SiteData(
        description=description,
        logo_url=logo_url,
        favicon=favicon,
        metadata=metadata,
        visual_identity=visual_identity,
    )


def make_analysis(
    pages: list[PageData] | None = None,
    site: SiteData | None = None,
) -> AnalysisData:
    """Create a test AnalysisData."""
    if pages is None:
        pages = [make_page()]
    return AnalysisData(pages=pages, site=site)


def make_full_analysis(
    url: str = "https://example.com",
    title: str = "Example Corp | Home",
    description: str = "Example Corp",
    logo_url: str = "https://example.com/logo.png",
    primary_color: str = "#FF5733",
) -> AnalysisData:
    """Create a fully populated AnalysisData for testing."""
    return AnalysisData(
        pages=[make_page(url=url, title=title)],
        site=make_site(
            description=description,
            logo_url=logo_url,
            visual_identity={"primary_color": primary_color},
        ),
    )
