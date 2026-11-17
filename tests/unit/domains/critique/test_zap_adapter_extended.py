"""Extended tests for ZAP adapter — covers scan flow, polling, and error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.domains.critique.services.scanners.tool_adapters.zap_adapter import (
    ZapAdapter,
)


class TestZapIsAvailable:
    """Test ZapAdapter.is_available method."""

    async def test_is_available_enabled_and_reachable(self):
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.zap_enabled = True
            ms.return_value.external_tools.zap_api_url = "http://zap:8080"
            ms.return_value.external_tools.zap_api_key = "test-key"  # pragma: allowlist secret

            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.httpx.AsyncClient",
                return_value=mock_client,
            ):
                adapter = ZapAdapter()
                result = await adapter.is_available()
                assert result is True

    async def test_is_available_enabled_but_unreachable(self):
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.zap_enabled = True
            ms.return_value.external_tools.zap_api_url = "http://zap:8080"
            ms.return_value.external_tools.zap_api_key = ""

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.httpx.AsyncClient",
                return_value=mock_client,
            ):
                adapter = ZapAdapter()
                result = await adapter.is_available()
                assert result is False


class TestZapScan:
    """Test ZapAdapter.scan method."""

    async def test_scan_success(self):
        """Full ZAP scan: spider + passive scan + alerts retrieval."""
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.zap_api_url = "http://zap:8080"
            ms.return_value.external_tools.zap_api_key = "key123"  # pragma: allowlist secret
            ms.return_value.external_tools.zap_timeout = 60

            # Spider start response
            spider_resp = MagicMock()
            spider_resp.raise_for_status = MagicMock()
            spider_resp.json.return_value = {"scan": "1"}

            # Spider status: 100% = complete
            spider_status_resp = MagicMock()
            spider_status_resp.raise_for_status = MagicMock()
            spider_status_resp.json.return_value = {"status": "100"}

            # Passive scan: 0 records remaining
            pscan_resp = MagicMock()
            pscan_resp.raise_for_status = MagicMock()
            pscan_resp.json.return_value = {"recordsToScan": "0"}

            # Alerts response
            alerts_resp = MagicMock()
            alerts_resp.raise_for_status = MagicMock()
            alerts_resp.json.return_value = {
                "alerts": [
                    {
                        "pluginId": "10038",
                        "alert": "CSP header not set",
                        "risk": "2",
                        "confidence": "3",
                        "description": "No CSP.",
                        "solution": "Add CSP.",
                        "url": "https://example.com",
                    }
                ]
            }

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=[spider_resp, spider_status_resp, pscan_resp, alerts_resp]
            )
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.httpx.AsyncClient",
                return_value=mock_client,
            ):
                adapter = ZapAdapter()
                findings = await adapter.scan("https://example.com")

            assert len(findings) == 1
            assert findings[0]["check_id"] == "zap-10038"
            assert findings[0]["severity"] == "medium"

    async def test_scan_timeout(self):
        """ZAP scan handles TimeoutError."""
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.zap_api_url = "http://zap:8080"
            ms.return_value.external_tools.zap_api_key = ""
            ms.return_value.external_tools.zap_timeout = 1

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=TimeoutError("ZAP timed out"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.httpx.AsyncClient",
                return_value=mock_client,
            ):
                adapter = ZapAdapter()
                findings = await adapter.scan("https://example.com")

            assert findings == []

    async def test_scan_http_error(self):
        """ZAP scan handles httpx.HTTPError."""
        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter.get_settings"
        ) as ms:
            ms.return_value.external_tools.zap_api_url = "http://zap:8080"
            ms.return_value.external_tools.zap_api_key = ""
            ms.return_value.external_tools.zap_timeout = 60

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.HTTPError("API error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)

            with patch(
                "src.domains.critique.services.scanners.tool_adapters.zap_adapter.httpx.AsyncClient",
                return_value=mock_client,
            ):
                adapter = ZapAdapter()
                findings = await adapter.scan("https://example.com")

            assert findings == []


class TestZapWaitForSpider:
    """Test _wait_for_spider helper."""

    async def test_spider_completes(self):
        adapter = ZapAdapter()
        mock_client = AsyncMock()

        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"status": "100"}
        mock_client.get = AsyncMock(return_value=status_resp)

        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter._POLL_INTERVAL_SECONDS",
            0,
        ):
            await adapter._wait_for_spider(mock_client, "http://zap:8080", {}, "1", timeout=60)

    async def test_spider_timeout(self):
        adapter = ZapAdapter()
        mock_client = AsyncMock()

        status_resp = MagicMock()
        status_resp.raise_for_status = MagicMock()
        status_resp.json.return_value = {"status": "50"}  # never reaches 100
        mock_client.get = AsyncMock(return_value=status_resp)

        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter._POLL_INTERVAL_SECONDS",
            0,
        ):
            # Very short timeout: will exit loop
            await adapter._wait_for_spider(mock_client, "http://zap:8080", {}, "1", timeout=0)


class TestZapWaitForPassiveScan:
    """Test _wait_for_passive_scan helper."""

    async def test_passive_scan_completes(self):
        adapter = ZapAdapter()
        mock_client = AsyncMock()

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"recordsToScan": "0"}
        mock_client.get = AsyncMock(return_value=resp)

        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter._POLL_INTERVAL_SECONDS",
            0,
        ):
            await adapter._wait_for_passive_scan(mock_client, "http://zap:8080", {}, timeout=60)

    async def test_passive_scan_timeout(self):
        adapter = ZapAdapter()
        mock_client = AsyncMock()

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"recordsToScan": "10"}  # never reaches 0
        mock_client.get = AsyncMock(return_value=resp)

        with patch(
            "src.domains.critique.services.scanners.tool_adapters.zap_adapter._POLL_INTERVAL_SECONDS",
            0,
        ):
            await adapter._wait_for_passive_scan(mock_client, "http://zap:8080", {}, timeout=0)
