"""Runtime configuration overrides backed by Redis."""

from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger(__name__)

REDIS_KEY = "critique:config:overrides"


async def get_runtime_value(section: str, key: str) -> str | None:
    """Get a runtime override value. Returns None if not overridden."""
    try:
        from src.shared.cache.redis_client import get_redis_client

        client = get_redis_client()
        field = f"{section}.{key}"
        value = await client.hget(REDIS_KEY, field)
        return value if isinstance(value, str) else None
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        logger.debug("Runtime config read failed: %s", e)
        return None


async def set_runtime_value(section: str, key: str, value: str) -> None:
    """Set a runtime override value."""
    from src.shared.cache.redis_client import get_redis_client

    client = get_redis_client()
    field = f"{section}.{key}"
    await client.hset(REDIS_KEY, field, value)


async def delete_runtime_value(section: str, key: str) -> bool:
    """Delete a runtime override. Returns True if deleted."""
    from src.shared.cache.redis_client import get_redis_client

    client = get_redis_client()
    field = f"{section}.{key}"
    result = await client.hdel(REDIS_KEY, field)
    return bool(result)


async def get_all_overrides() -> dict[str, dict[str, str]]:
    """Get all runtime overrides as {section: {key: value}}."""
    try:
        from src.shared.cache.redis_client import get_redis_client

        client = get_redis_client()
        raw = await client.hgetall(REDIS_KEY)
        overrides: dict[str, dict[str, str]] = {}
        if raw and isinstance(raw, dict):
            for field, value in raw.items():
                field_str = field if isinstance(field, str) else field.decode()
                value_str = value if isinstance(value, str) else value.decode()
                parts = field_str.split(".", 1)
                if len(parts) == 2:
                    section, key = parts
                    overrides.setdefault(section, {})[key] = value_str
        return overrides
    except (OSError, ValueError, KeyError, RuntimeError) as e:
        logger.debug("Runtime config read-all failed: %s", e)
        return {}


async def get_scoring_weights() -> dict[str, float]:
    """Get current scoring weights (runtime overrides merged with static config)."""
    from src.config import get_settings

    settings = get_settings()
    weights = {
        "a11y_weight": settings.scoring.a11y_weight,
        "security_weight": settings.scoring.security_weight,
        "performance_weight": settings.scoring.performance_weight,
        "seo_weight": settings.scoring.seo_weight,
        "ux_weight": settings.scoring.ux_weight,
        "critical_weight": settings.scoring.critical_weight,
        "high_weight": settings.scoring.high_weight,
        "medium_weight": settings.scoring.medium_weight,
        "low_weight": settings.scoring.low_weight,
        "info_weight": settings.scoring.info_weight,
    }

    # Apply runtime overrides
    overrides = await get_all_overrides()
    scoring_overrides = overrides.get("scoring", {})
    for key, value in scoring_overrides.items():
        if key in weights:
            with contextlib.suppress(ValueError):
                weights[key] = float(value)

    return weights
