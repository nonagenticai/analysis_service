"""Unit tests for webhook delivery."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.domains.critique.services.webhook import deliver_webhook


class TestWebhook:
    def test_module_imports(self) -> None:
        """Verify the webhook module can be imported."""
        assert deliver_webhook is not None

    @pytest.mark.asyncio
    async def test_webhook_success(self) -> None:
        """deliver_webhook should return True on successful delivery."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("src.domains.critique.services.webhook.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await deliver_webhook(
                "https://example.com/webhook",
                {"job_id": "j1", "status": "completed"},
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_webhook_retry_on_failure(self) -> None:
        """deliver_webhook should retry on HTTP error status."""
        mock_resp_fail = MagicMock()
        mock_resp_fail.status_code = 500
        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200

        with patch("src.domains.critique.services.webhook.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=[mock_resp_fail, mock_resp_ok])
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await deliver_webhook(
                "https://example.com/webhook",
                {"job_id": "j1", "status": "completed"},
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_webhook_max_retries_exceeded(self) -> None:
        """deliver_webhook should return False after max retries."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("src.domains.critique.services.webhook.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await deliver_webhook(
                "https://example.com/webhook",
                {"job_id": "j1", "status": "completed"},
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_webhook_timeout(self) -> None:
        """deliver_webhook should handle timeout exceptions."""
        with patch("src.domains.critique.services.webhook.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await deliver_webhook(
                "https://example.com/webhook",
                {"job_id": "j1", "status": "completed"},
            )
            assert result is False

    @pytest.mark.asyncio
    async def test_webhook_payload_structure(self) -> None:
        """deliver_webhook should POST the payload as JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("src.domains.critique.services.webhook.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            payload = {"job_id": "j1", "status": "completed", "scores": {"seo": 85}}
            await deliver_webhook("https://example.com/webhook", payload)

            call_kwargs = mock_client.post.call_args
            assert (
                call_kwargs.kwargs.get("json") == payload or call_kwargs[1].get("json") == payload
            )


class TestWebhookSSRF:
    """Defense-in-depth: deliver_webhook must refuse SSRF-unsafe callback URLs."""

    @pytest.mark.asyncio
    async def test_ssrf_metadata_ip_blocked(self) -> None:
        """Callback to the cloud metadata IP is rejected before any request."""
        # No httpx mock: validation must short-circuit before the network call.
        result = await deliver_webhook(
            "http://169.254.169.254/latest/meta-data/",
            {"job_id": "j1", "status": "completed"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_ssrf_private_ip_blocked(self) -> None:
        """Callback to a private RFC1918 IP is rejected."""
        result = await deliver_webhook(
            "http://192.168.1.1/webhook",
            {"job_id": "j1", "status": "completed"},
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_ssrf_localhost_blocked(self) -> None:
        """Callback to localhost is rejected."""
        result = await deliver_webhook(
            "http://localhost/webhook",
            {"job_id": "j1", "status": "completed"},
        )
        assert result is False
