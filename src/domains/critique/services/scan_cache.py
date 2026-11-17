"""Scan result caching for critique_service.

Caches scan results by URL + categories to avoid redundant scans.
Also supports per-scanner caching and domain-level invalidation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from src.config import get_settings
from src.domains.critique.schemas import LLMConfigOverride
from src.shared.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

CACHE_PREFIX = "critique:cache:"
SCANNER_CACHE_PREFIX = "critique:scanner:"
DOMAIN_KEYS_PREFIX = "critique:domain_keys:"


def _default_serializer(obj: object) -> object:
    """Deterministic JSON serializer for datetime, UUID, and other objects.

    ``isinstance`` rather than ``hasattr``: duck-typing here would silently
    serialize any object that happens to expose ``.hex`` (e.g. ``bytes``)
    through the wrong branch, and the cache key must be exactly reproducible.
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return repr(obj)


def _stable_hash(data: object) -> str:
    """SHA256 over a STABLE projection of ``data``.

    ``json.dumps(..., sort_keys=True)`` — never ``str(dict)``: dict iteration
    order and ``repr`` are not a contract, so a repr-derived key would silently
    change between runs (mass cache miss) or, worse, collide inconsistently.
    """
    serialized = json.dumps(data, sort_keys=True, default=_default_serializer)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


def _steering_fingerprint(
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> str | None:
    """Stable fingerprint of the output-STEERING inputs, or None when neither steers.

    ``llm_config`` selects WHICH LLM produces the findings (it reaches the UX
    scanner as a per-call override) and ``site_analysis`` is fed to the security
    and UX scanners — BOTH change the cached payload. Neither was in the key, so
    a job that set ``llm_config.model=<premium>`` hit the entry written by an
    earlier DEFAULT-model job on the same URL/categories/instructions: it was
    served the cheap model's findings and score, reported ``completed``, and was
    BILLED for a premium model that never ran. Same class content_service fixed
    in ``src/cache/cache_keys.py`` — a premium-model body must never be served
    to a request billed for a cheaper model.

    Folded in ONLY when truthy, exactly as the content_service precedent does:
    an absent/empty override hashes as the pre-fix scheme did, so model-agnostic
    entries already in Redis are not invalidated and this is not a
    cache-bust-everything change.
    """
    data: dict[str, object] = {}
    if llm_config is not None:
        cfg = {k: v for k, v in llm_config.model_dump().items() if v}
        if cfg:
            data["llm_config"] = cfg
    if site_analysis:
        data["site_analysis"] = site_analysis
    if not data:
        return None
    return _stable_hash(data)


def _compute_cache_key(
    tenant_id: str,
    url: str,
    categories: list[str] | None = None,
    instructions: str | None = None,
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> str:
    """Compute a cache key from tenant, URL, sorted categories, instructions, and steering.

    ``tenant_id`` is the FIRST key component so two tenants scanning the same URL never
    share cached findings — the cached payload is a security/SEO/a11y audit of a
    user-supplied (possibly private) URL, so it must stay tenant-scoped under RLS-enforce.
    Mirrors content_service's ``shotlist_cache_key(tenant_id, …)`` / tts_service's
    ``_compute_cache_key(tenant_id, …)``.

    ``llm_config`` / ``site_analysis`` steer the produced findings — see
    ``_steering_fingerprint`` for why omitting them was a billing defect.
    """
    cats = sorted(categories) if categories else ["all"]
    raw = f"{tenant_id}|{url}|{','.join(cats)}"
    if instructions:
        raw += f"|{hashlib.sha256(instructions.encode()).hexdigest()[:16]}"
    steering = _steering_fingerprint(llm_config, site_analysis)
    if steering:
        raw += f"|{steering}"
    return CACHE_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


async def get_cached_result(
    tenant_id: str,
    url: str,
    categories: list[str] | None = None,
    instructions: str | None = None,
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> dict | None:
    """Get cached scan result if available.

    Args:
        tenant_id: Owning tenant (scopes the cache key).
        url: Scanned URL.
        categories: Category list used during the scan.
        instructions: Optional custom instructions (affects cache key).
        llm_config: Per-request LLM override that steers the findings (affects key).
        site_analysis: Site context fed to the security/UX scanners (affects key).

    Returns:
        Cached result dict, or None if not cached or cache disabled.
    """
    try:
        settings = get_settings()
        if not settings.cache.enabled:
            return None
        client = get_redis_client()
        key = _compute_cache_key(
            tenant_id, url, categories, instructions, llm_config, site_analysis
        )
        result = await client.get(key)
        if result is not None:
            logger.debug("Cache hit: url=%s", url)
        else:
            logger.debug("Cache miss: url=%s", url)
        return result
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Cache get failed: %s", e)
        return None


async def set_cached_result(
    tenant_id: str,
    url: str,
    categories: list[str] | None,
    result: dict[str, Any],
    instructions: str | None = None,
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> bool:
    """Cache scan result with TTL.

    Args:
        tenant_id: Owning tenant (scopes the cache key).
        url: Scanned URL.
        categories: Category list used during the scan.
        result: Result dict to cache.
        instructions: Optional custom instructions (affects cache key).
        llm_config: Per-request LLM override that steered these findings (affects key).
        site_analysis: Site context that steered these findings (affects key).

    Returns:
        True if cached successfully, False otherwise.
    """
    try:
        settings = get_settings()
        if not settings.cache.enabled:
            return False
        client = get_redis_client()
        key = _compute_cache_key(
            tenant_id, url, categories, instructions, llm_config, site_analysis
        )
        ttl = settings.cache.ttl
        success = await client.set(key, result, ttl=ttl)
        logger.debug("Cache set: url=%s ttl=%d success=%s", url, ttl, success)
        return success
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Cache set failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Per-scanner caching
# ---------------------------------------------------------------------------


def _compute_scanner_cache_key(
    tenant_id: str,
    url: str,
    scanner_name: str,
    instructions: str | None = None,
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> str:
    """Compute a cache key for a single scanner's results (tenant-scoped).

    ``llm_config`` / ``site_analysis`` are folded in for the same reason as
    ``_compute_cache_key``: the ``ux`` scanner takes ``llm_config`` as a
    per-call model override and the ``sec``/``ux`` scanners take
    ``site_analysis``, so both steer the cached findings. Omitting them let a
    premium-model request be served the default model's per-scanner findings.
    """
    raw = f"{tenant_id}|{url}|{scanner_name}"
    if instructions:
        raw += f"|{hashlib.sha256(instructions.encode()).hexdigest()[:16]}"
    steering = _steering_fingerprint(llm_config, site_analysis)
    if steering:
        raw += f"|{steering}"
    return SCANNER_CACHE_PREFIX + hashlib.sha256(raw.encode()).hexdigest()


def _extract_domain(url: str) -> str:
    """Extract domain from URL for domain-key tracking."""
    try:
        hostname = urlparse(url).hostname or ""
        if hostname.startswith("www."):
            hostname = hostname[4:]
        return hostname
    except OSError, ConnectionError, TimeoutError:
        return ""


async def get_scanner_cached_result(
    tenant_id: str,
    url: str,
    scanner_name: str,
    instructions: str | None = None,
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> list[dict] | None:
    """Get cached result for a specific scanner.

    Args:
        tenant_id: Owning tenant (scopes the cache key).
        url: Scanned URL.
        scanner_name: Name of the scanner (e.g. "seo", "a11y").
        instructions: Optional custom instructions.
        llm_config: Per-request LLM override that steers the findings (affects key).
        site_analysis: Site context fed to the security/UX scanners (affects key).

    Returns:
        List of findings or None on cache miss.
    """
    try:
        settings = get_settings()
        if not settings.cache.enabled:
            return None
        client = get_redis_client()
        key = _compute_scanner_cache_key(
            tenant_id, url, scanner_name, instructions, llm_config, site_analysis
        )
        result = await client.get(key)
        if result is not None:
            logger.debug("Scanner cache hit: scanner=%s url=%s", scanner_name, url)
        return result
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Scanner cache get failed: %s", e)
        return None


async def set_scanner_cached_result(
    tenant_id: str,
    url: str,
    scanner_name: str,
    findings: list[dict],
    instructions: str | None = None,
    llm_config: LLMConfigOverride | None = None,
    site_analysis: dict | None = None,
) -> bool:
    """Cache a single scanner's results with TTL.

    Also tracks the key in a per-domain Redis SET for invalidation.

    Args:
        tenant_id: Owning tenant (scopes the cache key).
        url: Scanned URL.
        scanner_name: Name of the scanner.
        findings: Scanner findings to cache.
        instructions: Optional custom instructions.
        llm_config: Per-request LLM override that steered these findings (affects key).
        site_analysis: Site context that steered these findings (affects key).

    Returns:
        True if cached successfully.
    """
    try:
        settings = get_settings()
        if not settings.cache.enabled:
            return False
        client = get_redis_client()
        key = _compute_scanner_cache_key(
            tenant_id, url, scanner_name, instructions, llm_config, site_analysis
        )
        ttl = settings.cache.scanner_ttl
        success = await client.set(key, findings, ttl=ttl)

        # Track key in domain set for invalidation
        if success and settings.cache.domain_invalidation_enabled:
            domain = _extract_domain(url)
            if domain:
                domain_key = DOMAIN_KEYS_PREFIX + domain
                await client.sadd(domain_key, key)
                await client.expire(domain_key, ttl * 2)

        logger.debug(
            "Scanner cache set: scanner=%s url=%s ttl=%d success=%s",
            scanner_name,
            url,
            ttl,
            success,
        )
        return success
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Scanner cache set failed: %s", e)
        return False


async def invalidate_domain_cache(domain: str) -> int:
    """Invalidate all cached scanner results for a domain.

    Args:
        domain: Domain name (e.g. "example.com").

    Returns:
        Number of keys deleted.
    """
    try:
        settings = get_settings()
        if not settings.cache.enabled:
            return 0
        client = get_redis_client()
        domain_key = DOMAIN_KEYS_PREFIX + domain
        keys = await client.smembers(domain_key)
        if not keys:
            return 0

        deleted = 0
        for key in keys:
            if await client.delete(key):
                deleted += 1
        await client.delete(domain_key)
        logger.info("Domain cache invalidated: domain=%s keys=%d", domain, deleted)
        return deleted
    except (OSError, ConnectionError, TimeoutError) as e:
        logger.debug("Domain invalidation failed: %s", e)
        return 0
