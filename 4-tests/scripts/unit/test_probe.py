"""Server probe unit tests — tests for app.services.server_probe."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.server_probe import (
    ProbeLevel,
    ServerCapabilities,
    probe_server,
    _coerce_bool,
)


@pytest.mark.unit
class TestServerCapabilities:
    def test_to_dict_and_from_dict(self):
        caps = ServerCapabilities(
            reachable=True,
            responsive=True,
            supports_offline=True,
            has_timestamp=True,
            is_final_semantics="always_false",
            inferred_server_type="funasr_main",
            probe_level=ProbeLevel.OFFLINE_LIGHT,
            probe_duration_ms=150.0,
            probe_notes=["WebSocket connected", "offline probe OK"],
        )
        d = caps.to_dict()
        assert d["reachable"] is True
        assert d["inferred_server_type"] == "funasr_main"
        assert d["probe_level"] == "OFFLINE_LIGHT"

        restored = ServerCapabilities.from_dict(d)
        assert restored.reachable is True
        assert restored.inferred_server_type == "funasr_main"
        assert restored.probe_level == ProbeLevel.OFFLINE_LIGHT
        assert "offline probe OK" in restored.probe_notes

    def test_from_dict_unknown_probe_level_fallback(self):
        d = {"probe_level": "NONEXISTENT"}
        caps = ServerCapabilities.from_dict(d)
        assert caps.probe_level == ProbeLevel.CONNECT_ONLY


@pytest.mark.unit
class TestCoerceBool:
    @pytest.mark.parametrize("val,expected", [
        (True, True),
        (False, False),
        (None, None),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("yes", True),
        ("no", False),
        ("", False),
    ])
    def test_coerce_bool_values(self, val, expected):
        assert _coerce_bool(val) is expected


@pytest.mark.unit
class TestProbeServer:
    async def test_connect_only_success(self):
        """T-M2-01: connect-only probe should succeed when connection works."""
        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with patch("app.services.server_probe.connect_websocket", return_value=mock_ws):
            caps = await probe_server(
                "10.0.0.1", 10095,
                use_ssl=False,
                level=ProbeLevel.CONNECT_ONLY,
                timeout=5.0,
            )

        assert caps.reachable is True
        assert caps.probe_duration_ms > 0

    async def test_connect_only_refused(self):
        with patch(
            "app.services.server_probe.connect_websocket",
            side_effect=ConnectionRefusedError("refused"),
        ):
            caps = await probe_server(
                "10.0.0.1", 10095,
                use_ssl=False,
                level=ProbeLevel.CONNECT_ONLY,
                timeout=3.0,
            )

        assert caps.reachable is False
        assert caps.error is not None
        assert "refused" in caps.error

    async def test_offline_light_infer_funasr_main(self):
        """T-M2-02: offline-light probe should correctly infer funasr_main from is_final=False."""
        response_json = json.dumps({"text": "", "mode": "offline", "is_final": False})

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=response_json)

        with patch("app.services.server_probe.connect_websocket", return_value=mock_ws):
            caps = await probe_server(
                "10.0.0.1", 10095,
                use_ssl=False,
                level=ProbeLevel.OFFLINE_LIGHT,
                timeout=8.0,
            )

        assert caps.reachable is True
        assert caps.responsive is True
        assert caps.inferred_server_type == "funasr_main"
        assert caps.is_final_semantics == "always_false"

    async def test_offline_light_infer_legacy(self):
        response_json = json.dumps({"text": "测试", "mode": "offline", "is_final": True})

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=response_json)

        with patch("app.services.server_probe.connect_websocket", return_value=mock_ws):
            caps = await probe_server(
                "10.0.0.1", 10095,
                use_ssl=False,
                level=ProbeLevel.OFFLINE_LIGHT,
                timeout=8.0,
            )

        assert caps.reachable is True
        assert caps.inferred_server_type == "legacy"
        assert caps.is_final_semantics == "legacy_true"

    async def test_timeout_returns_unreachable(self):
        with patch(
            "app.services.server_probe.connect_websocket",
            side_effect=asyncio.TimeoutError(),
        ):
            caps = await probe_server(
                "10.0.0.1", 10095,
                use_ssl=False,
                level=ProbeLevel.CONNECT_ONLY,
                timeout=1.0,
            )

        assert caps.reachable is False
        assert caps.error is not None
