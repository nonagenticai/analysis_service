"""Tests for src.security.url_validator -- SSRF protection."""

import socket
from unittest.mock import patch

import pytest

from src.shared.security.url_validator import validate_callback_url

# A public A record. Resolution is always patched: since the guard now fails
# CLOSED on a DNS error, a real `getaddrinfo` here would turn any resolver
# hiccup on a CI runner into a red build, and under this repo's `-n 4` xdist
# `--reruns 2` would quietly paper over it.
_PUBLIC_RESOLUTION = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _patch_public_dns():
    return patch(
        "src.shared.security.url_validator.socket.getaddrinfo",
        return_value=_PUBLIC_RESOLUTION,
    )


class TestValidateCallbackUrl:
    """Tests for SSRF protection in callback URL validation."""

    def test_valid_https_url_passes(self):
        with _patch_public_dns():
            result = validate_callback_url("https://example.com/webhook")
        assert result == "https://example.com/webhook"

    def test_valid_http_url_passes(self):
        with _patch_public_dns():
            result = validate_callback_url("http://example.com/webhook")
        assert result == "http://example.com/webhook"

    def test_empty_url_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            validate_callback_url("")

    def test_none_like_empty_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            validate_callback_url("   ")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_callback_url("ftp://example.com/file")

    def test_file_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_callback_url("file:///etc/passwd")

    def test_javascript_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_callback_url("javascript:alert(1)")

    def test_localhost_rejected(self):
        with pytest.raises(ValueError, match="localhost"):
            validate_callback_url("http://localhost/webhook")

    def test_localhost_localdomain_rejected(self):
        with pytest.raises(ValueError, match="localhost"):
            validate_callback_url("http://localhost.localdomain/webhook")

    def test_127_0_0_1_rejected(self):
        with pytest.raises(ValueError, match="private IP"):
            validate_callback_url("http://127.0.0.1/webhook")

    def test_10_x_rejected(self):
        with pytest.raises(ValueError, match="private IP"):
            validate_callback_url("http://10.0.0.1/webhook")

    def test_172_16_x_rejected(self):
        with pytest.raises(ValueError, match="private IP"):
            validate_callback_url("http://172.16.0.1/webhook")

    def test_192_168_x_rejected(self):
        with pytest.raises(ValueError, match="private IP"):
            validate_callback_url("http://192.168.1.1/webhook")

    def test_169_254_x_rejected(self):
        with pytest.raises(ValueError, match="private IP"):
            validate_callback_url("http://169.254.169.254/metadata")

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(ValueError, match="private IP"):
            validate_callback_url("http://[::1]/webhook")

    def test_dns_resolving_to_private_ip_rejected(self):
        """If a hostname resolves to a private IP, it should be rejected."""
        mock_result = [
            (2, 1, 6, "", ("10.0.0.1", 80)),
        ]
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo", return_value=mock_result
        ):
            with pytest.raises(ValueError, match="resolves to private IP"):
                validate_callback_url("https://evil.example.com/webhook")

    def test_dns_failure_rejected(self):
        """DNS resolution failure must FAIL CLOSED.

        Inverted deliberately. This test previously asserted the opposite --
        that an unresolvable host was returned unchanged, on the theory that
        "webhook delivery will fail naturally at request time". It does not:
        delivery happens later and from a different network position (inside
        the cluster, where split-DNS answers names that do not resolve from a
        dev box or a CI runner), so admission-time non-resolution is not
        evidence that the callback is harmless. The test was encoding the bug.
        """
        with patch(
            "src.shared.security.url_validator.socket.getaddrinfo",
            side_effect=socket.gaierror("Name resolution failed"),
        ):
            with pytest.raises(ValueError, match="not allowed"):
                validate_callback_url("https://nonexistent.example.com/webhook")

    def test_no_hostname_rejected(self):
        with pytest.raises(ValueError, match="no hostname"):
            validate_callback_url("http:///path")
