"""Network validator unit tests."""

import socket

import pytest

from app.utils.network_validator import validate_callback_url


@pytest.mark.unit
class TestNetworkValidator:
    def test_callback_url_allows_when_dns_lookup_temporarily_fails(self, monkeypatch):
        monkeypatch.setattr(
            "app.utils.network_validator.socket.getaddrinfo",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("temporary failure")),
        )

        assert validate_callback_url("https://callback.example.com/hook") is None
