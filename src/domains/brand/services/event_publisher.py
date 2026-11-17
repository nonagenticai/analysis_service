"""Brand event publisher for downstream notification via Redis Pub/Sub."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# Everything a publish round-trip can fail with that must NOT reach the caller.
#
# The old tuple here was ``(OSError, ConnectionError, TimeoutError)`` -- all
# three builtins, and *none* of them is what a Redis outage raises. redis-py
# defines its own ``ConnectionError``/``TimeoutError`` under ``RedisError``,
# which shadow the builtin names at the call site but are unrelated to them, so
# the guard could only ever catch a socket error that escaped redis-py raw.
#
# * ``RedisError`` -- the redis-py hierarchy (connection refused, timeout,
#   read-only replica, auth). The one that actually fires.
# * ``OSError`` -- socket-level failures that escape below redis-py.
# * ``TypeError`` / ``ValueError`` -- ``json.dumps`` on an event whose payload
#   holds something ``default=str`` cannot render (circular reference). This is
#   inside the ``try``, so it was the only live escape route the old tuple half
#   covered, via ``ValueError``'s absence.
#
# Deliberately NOT ``Exception``: an ``AttributeError`` from a renamed event
# field is a programming error and must still surface in CI.
_PUBLISH_FAILURES = (RedisError, OSError, TypeError, ValueError)


class BrandEvent:
    """A brand extraction event for downstream consumers.

    Attributes:
        event_type: Event category (e.g. "brand.extracted", "brand.overridden").
        domain: The domain the event pertains to.
        tenant_id: Tenant that triggered the event.
        timestamp: ISO-8601 timestamp of event creation.
        brand: Optional brand data payload.
        quality_score: Extraction quality score (0.0-1.0).
        version: Brand version number at time of event.
    """

    def __init__(
        self,
        event_type: str,
        domain: str,
        tenant_id: str,
        brand: dict[str, Any] | None = None,
        quality_score: float = 0.0,
        version: int = 1,
    ) -> None:
        self.event_type = event_type
        self.domain = domain
        self.tenant_id = tenant_id
        self.timestamp = datetime.now(UTC).isoformat()
        self.brand = brand
        self.quality_score = quality_score
        self.version = version

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "domain": self.domain,
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp,
            "brand": self.brand,
            "quality_score": self.quality_score,
            "version": self.version,
        }


class BrandEventPublisher:
    """Publishes brand events to Redis Pub/Sub. Fire-and-forget."""

    CHANNEL = "brand:events"

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client

    async def publish(self, event: BrandEvent) -> bool:
        """Publish a brand event to Redis Pub/Sub.

        Args:
            event: The brand event to publish.

        Returns:
            True if published successfully, False on failure.
        """
        if not self._redis:
            logger.debug("No Redis client -- event not published")
            return False

        try:
            payload = json.dumps(event.to_dict(), default=str)
            await self._redis.publish(self.CHANNEL, payload)
            logger.debug(
                "Published event, type=%s, domain=%s, channel=%s",
                event.event_type,
                event.domain,
                self.CHANNEL,
            )
            return True
        except _PUBLISH_FAILURES as e:
            logger.warning(
                "Failed to publish event, type=%s, domain=%s: %s",
                event.event_type,
                event.domain,
                e,
            )
            return False

    async def publish_extraction(
        self,
        domain: str,
        tenant_id: str,
        brand: dict[str, Any],
        quality_score: float = 0.0,
        version: int = 1,
    ) -> bool:
        """Convenience method to publish a brand.extracted event."""
        event = BrandEvent(
            event_type="brand.extracted",
            domain=domain,
            tenant_id=tenant_id,
            brand=brand,
            quality_score=quality_score,
            version=version,
        )
        return await self.publish(event)

    async def publish_override(
        self,
        domain: str,
        tenant_id: str,
        overridden_fields: list[str],
        version: int = 1,
    ) -> bool:
        """Convenience method to publish a brand.overridden event."""
        event = BrandEvent(
            event_type="brand.overridden",
            domain=domain,
            tenant_id=tenant_id,
            brand={"overridden_fields": overridden_fields},
            version=version,
        )
        return await self.publish(event)
