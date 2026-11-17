"""Unit tests for webhook delivery service.

Tests deliver_webhook with mocked httpx.AsyncClient including retries,
timeouts, and error handling.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.domains.review.services.webhook import deliver_webhook


@pytest.fixture
def mock_webhook_settings() -> MagicMock:
    """Mock settings for webhook configuration."""
    settings = MagicMock()
    settings.webhook.max_retries = 3
    settings.webhook.timeout = 10
    return settings


@pytest.fixture
def sample_payload() -> dict:
    """Sample webhook payload."""
    return {
        "job_id": "test-job-123",
        "status": "completed",
        "review_types": ["design_review"],
    }


@pytest.mark.unit
class TestDeliverWebhook:
    """Tests for webhook delivery function."""

    async def test_successful_delivery_200(
        self, mock_webhook_settings: MagicMock, sample_payload: dict
    ) -> None:
        """Test successful webhook delivery with 200 response."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "src.domains.review.services.webhook.get_settings",
                return_value=mock_webhook_settings,
            ),
            patch(
                "src.domains.review.services.webhook.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await deliver_webhook("https://example.com/webhook", sample_payload)

            assert result is True
            mock_client.post.assert_called_once()

    async def test_server_error_retries(
        self, mock_webhook_settings: MagicMock, sample_payload: dict
    ) -> None:
        """Test that 500 response triggers retries."""
        mock_response_500 = MagicMock(spec=httpx.Response)
        mock_response_500.status_code = 500

        mock_response_200 = MagicMock(spec=httpx.Response)
        mock_response_200.status_code = 200

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return mock_response_500
            return mock_response_200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "src.domains.review.services.webhook.get_settings",
                return_value=mock_webhook_settings,
            ),
            patch(
                "src.domains.review.services.webhook.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await deliver_webhook("https://example.com/webhook", sample_payload)

            assert result is True
            assert call_count == 3

    async def test_connection_timeout_retries(
        self, mock_webhook_settings: MagicMock, sample_payload: dict
    ) -> None:
        """Test that connection timeout triggers retries then succeeds."""
        mock_response_200 = MagicMock(spec=httpx.Response)
        mock_response_200.status_code = 200

        call_count = 0

        async def mock_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectTimeout("Connection timed out")
            return mock_response_200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=mock_post)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "src.domains.review.services.webhook.get_settings",
                return_value=mock_webhook_settings,
            ),
            patch(
                "src.domains.review.services.webhook.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await deliver_webhook("https://example.com/webhook", sample_payload)

            assert result is True
            assert call_count == 2

    async def test_non_2xx_response_handling(
        self, mock_webhook_settings: MagicMock, sample_payload: dict
    ) -> None:
        """Test that non-2xx responses (e.g. 403) trigger retries and ultimately fail."""
        mock_response_403 = MagicMock(spec=httpx.Response)
        mock_response_403.status_code = 403

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response_403)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "src.domains.review.services.webhook.get_settings",
                return_value=mock_webhook_settings,
            ),
            patch(
                "src.domains.review.services.webhook.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await deliver_webhook("https://example.com/webhook", sample_payload)

            assert result is False
            assert mock_client.post.call_count == 3  # max_retries

    async def test_max_retries_exceeded_returns_false(
        self, mock_webhook_settings: MagicMock, sample_payload: dict
    ) -> None:
        """Test that exhausting all retries returns False."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "src.domains.review.services.webhook.get_settings",
                return_value=mock_webhook_settings,
            ),
            patch(
                "src.domains.review.services.webhook.httpx.AsyncClient", return_value=mock_client
            ),
        ):
            result = await deliver_webhook("https://example.com/webhook", sample_payload)

            assert result is False
            assert mock_client.post.call_count == 3  # max_retries

    async def test_ssrf_private_ip_blocked(self, sample_payload: dict) -> None:
        """Test that callback to private IP is blocked by SSRF protection."""
        result = await deliver_webhook("http://192.168.1.1/webhook", sample_payload)
        assert result is False

    async def test_ssrf_localhost_blocked(self, sample_payload: dict) -> None:
        """Test that callback to localhost is blocked by SSRF protection."""
        result = await deliver_webhook("http://localhost/webhook", sample_payload)
        assert result is False
