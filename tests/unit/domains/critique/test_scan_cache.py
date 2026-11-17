"""Unit tests for scan cache."""

from src.domains.critique.schemas import LLMConfigOverride
from src.domains.critique.services.scan_cache import (
    _compute_cache_key,
    _compute_scanner_cache_key,
)

_T = "2e960681-0187-5dad-ba0a-2b95a67ecfaa"
_URL = "https://example.com"


class TestSteeringInThekey:
    """Regression: a premium-model request must not be served a cheap-model result.

    ``llm_config`` (selects WHICH LLM produces the findings) and
    ``site_analysis`` (fed to the security/UX scanners) both steer the cached
    payload but were absent from the key. Job A ran the DEFAULT model on a URL;
    job B — same url/categories/instructions — set
    ``llm_config.model=<premium>``, hit A's entry, returned A's findings and
    score, reported ``completed``, and was BILLED for a model that never ran.
    """

    def test_different_llm_config_different_key(self) -> None:
        cheap = _compute_cache_key(_T, _URL, ["seo"], None, LLMConfigOverride(model="cheap-model"))
        premium = _compute_cache_key(
            _T, _URL, ["seo"], None, LLMConfigOverride(model="premium-model")
        )
        assert cheap != premium

    def test_default_model_entry_is_not_served_to_an_override(self) -> None:
        """The exact billing collision: no override vs a premium override."""
        default_run = _compute_cache_key(_T, _URL, ["seo"])
        premium_run = _compute_cache_key(
            _T, _URL, ["seo"], None, LLMConfigOverride(model="premium-model")
        )
        assert default_run != premium_run

    def test_identical_llm_config_still_hits(self) -> None:
        """Not a cache-bust-everything change: identical steering shares one key."""
        cfg_a = LLMConfigOverride(model="m", base_url="https://llm", api_key_env="K")
        cfg_b = LLMConfigOverride(model="m", base_url="https://llm", api_key_env="K")
        assert _compute_cache_key(_T, _URL, ["seo"], "instr", cfg_a) == _compute_cache_key(
            _T, _URL, ["seo"], "instr", cfg_b
        )

    def test_empty_override_preserves_the_legacy_key(self) -> None:
        """An absent/empty override must hash as the pre-fix scheme did, so
        entries already in Redis for model-agnostic requests stay valid."""
        legacy = _compute_cache_key(_T, _URL, ["seo"])
        assert _compute_cache_key(_T, _URL, ["seo"], None, None) == legacy
        assert _compute_cache_key(_T, _URL, ["seo"], None, LLMConfigOverride()) == legacy
        assert _compute_cache_key(_T, _URL, ["seo"], None, None, {}) == legacy

    def test_base_url_steers_the_key(self) -> None:
        """base_url reroutes the call to a different provider — output-determining."""
        a = _compute_cache_key(_T, _URL, ["seo"], None, LLMConfigOverride(base_url="https://a"))
        b = _compute_cache_key(_T, _URL, ["seo"], None, LLMConfigOverride(base_url="https://b"))
        assert a != b

    def test_different_site_analysis_different_key(self) -> None:
        a = _compute_cache_key(_T, _URL, ["security"], None, None, {"stack": "django"})
        b = _compute_cache_key(_T, _URL, ["security"], None, None, {"stack": "rails"})
        assert a != b

    def test_site_analysis_key_is_order_stable(self) -> None:
        """Keyed on a sorted-json sha256, NOT ``str(dict)``: dict ordering and
        ``repr`` are not a contract, so an equal-but-differently-built dict must
        produce the same key."""
        a = _compute_cache_key(_T, _URL, ["security"], None, None, {"x": 1, "y": 2})
        b = _compute_cache_key(_T, _URL, ["security"], None, None, {"y": 2, "x": 1})
        assert a == b

    def test_scanner_key_different_llm_config_different_key(self) -> None:
        cheap = _compute_scanner_cache_key(
            _T, _URL, "ux", None, LLMConfigOverride(model="cheap-model")
        )
        premium = _compute_scanner_cache_key(
            _T, _URL, "ux", None, LLMConfigOverride(model="premium-model")
        )
        assert cheap != premium

    def test_scanner_key_identical_steering_still_hits(self) -> None:
        cfg = LLMConfigOverride(model="m")
        assert _compute_scanner_cache_key(_T, _URL, "ux", None, cfg) == _compute_scanner_cache_key(
            _T, _URL, "ux", None, LLMConfigOverride(model="m")
        )

    def test_scanner_key_empty_override_preserves_legacy(self) -> None:
        legacy = _compute_scanner_cache_key(_T, _URL, "ux")
        assert _compute_scanner_cache_key(_T, _URL, "ux", None, LLMConfigOverride()) == legacy

    def test_scanner_key_different_site_analysis_different_key(self) -> None:
        a = _compute_scanner_cache_key(_T, _URL, "sec", None, None, {"stack": "django"})
        b = _compute_scanner_cache_key(_T, _URL, "sec", None, None, {"stack": "rails"})
        assert a != b


class TestScanCache:
    def test_cache_key_deterministic(self) -> None:
        key1 = _compute_cache_key(_T, "https://example.com", ["seo", "security"])
        key2 = _compute_cache_key(_T, "https://example.com", ["seo", "security"])
        assert key1 == key2

    def test_cache_key_sorted_categories(self) -> None:
        key1 = _compute_cache_key(_T, "https://example.com", ["security", "seo"])
        key2 = _compute_cache_key(_T, "https://example.com", ["seo", "security"])
        assert key1 == key2

    def test_cache_key_different_urls(self) -> None:
        key1 = _compute_cache_key(_T, "https://a.com")
        key2 = _compute_cache_key(_T, "https://b.com")
        assert key1 != key2

    def test_cache_key_tenant_scoped(self) -> None:
        # Cross-tenant isolation: same URL + categories MUST NOT collide across tenants
        # (cached payload is a security/SEO/a11y audit of a user-supplied URL).
        key1 = _compute_cache_key(
            "ee0a5549-a6f9-5c7b-861c-bdb1d029ce6d", "https://example.com", ["seo", "security"]
        )
        key2 = _compute_cache_key(
            "4151df14-4770-5a91-ab9f-cbf2d14e68be", "https://example.com", ["seo", "security"]
        )
        assert key1 != key2

    def test_cache_key_has_prefix(self) -> None:
        key = _compute_cache_key(_T, "https://example.com")
        assert key.startswith("critique:cache:")

    def test_different_instructions_different_key(self) -> None:
        key1 = _compute_cache_key(_T, "https://example.com", ["seo"], "instructions A")
        key2 = _compute_cache_key(_T, "https://example.com", ["seo"], "instructions B")
        assert key1 != key2

    def test_none_instructions_matches_omitted(self) -> None:
        key_omitted = _compute_cache_key(_T, "https://example.com", ["seo"])
        key_none = _compute_cache_key(_T, "https://example.com", ["seo"], None)
        assert key_omitted == key_none

    def test_instructions_deterministic(self) -> None:
        key1 = _compute_cache_key(_T, "https://example.com", ["seo"], "focus on UX")
        key2 = _compute_cache_key(_T, "https://example.com", ["seo"], "focus on UX")
        assert key1 == key2
