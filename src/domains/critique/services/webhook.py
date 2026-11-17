"""Webhook notification delivery for critique_service.

Delivers job completion notifications to callback URLs.
"""

from __future__ import annotations

import logging

import httpx

from src.config import get_settings
from src.shared.security.url_validator import validate_callback_url

logger = logging.getLogger(__name__)


async def deliver_webhook(callback_url: str, payload: dict) -> bool:
    """Deliver a webhook notification with retry.

    Args:
        callback_url: URL to POST the notification to.
        payload: JSON payload with job_id, status, scores, findings_count, summary.

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

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    callback_url,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "CritiqueService/1.0",
                    },
                )
                if response.status_code < 400:
                    logger.info("Webhook delivered to %s (attempt %d)", callback_url, attempt)
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

    logger.error("Webhook delivery exhausted retries for %s", callback_url)
    return False
