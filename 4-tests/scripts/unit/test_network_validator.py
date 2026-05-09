"""Network validator unit tests."""

import socket

import pytest

from app.utils.network_validator import validate_callback_url


@pytest.mark.unit
class TestNetworkValidator:
    def test_callback_url_blocks_when_dns_lookup_fails(self, monkeypatch):
        """DNS resolution failure should be treated as private (fail-closed)."""
        monkeypatch.setattr(
            "app.utils.network_validator.socket.getaddrinfo",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("temporary failure")),
        )

        result = validate_callback_url("https://callback.example.com/hook")
        assert result is not None
        assert "private" in result.lower() or "internal" in result.lower()
