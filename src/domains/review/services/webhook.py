"""Webhook notification delivery for review domain.

Delivers job completion notifications to callback URLs with retry logic.
Pattern follows critique_service/src/services/webhook.py.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from src.config import get_settings
from src.shared.security.url_validator import validate_callback_url

logger = logging.getLogger(__name__)


async def deliver_webhook(callback_url: str, payload: dict) -> bool:
    """Deliver a webhook notification with retry.

    Args:
        callback_url: URL to POST the notification to.
        payload: JSON payload with job_id, status, review_types, results_summary.

    Returns:
        True if delivery succeeded, False otherwise.
    """
    # Defense-in-depth: validate URL before delivery
    try:
        validate_callback_url(callback_url)
    except ValueError as exc:
        logger.warning("SSRF blocked webhook delivery to %s: %s", callback_url, exc)
        return False

    settings = get_settings()
    max_retries = settings.webhook.max_retries
    timeout = settings.webhook.timeout

    start_time = time.monotonic()
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "Webhook delivery starting: url=%s, attempt=%d/%d",
                callback_url,
                attempt,
                max_retries,
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    callback_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "ReviewService/1.0",
                    },
                )
                if response.status_code < 400:
                    duration = time.monotonic() - start_time
                    logger.info(
                        "Webhook delivered to %s (attempt %d, %.2fs)",
                        callback_url,
                        attempt,
                        duration,
                    )
                    return True
                logger.warning(
                    "Webhook delivery got %d from %s (attempt %d/%d)",
                    response.status_code,
                    callback_url,
                    attempt,
                    max_retries,
                )
        except (OSError, TimeoutError, RuntimeError, httpx.HTTPError) as e:
            logger.warning(
                "Webhook delivery failed for %s (attempt %d/%d): %s",
                callback_url,
                attempt,
                max_retries,
                e,
            )

        if attempt < max_retries:
            backoff = 2.0 ** (attempt - 1)  # 1s, 2s, 4s...
            await asyncio.sleep(backoff)

    logger.error("Webhook delivery exhausted retries for %s", callback_url)
    return False
