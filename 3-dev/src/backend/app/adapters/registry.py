"""Adapter registry - maps protocol versions to adapter instances."""

from app.adapters.base import BaseAdapter, ServerType
from app.adapters.funasr_ws import FunASRWebSocketAdapter

_registry: dict[str, BaseAdapter] = {}


def get_adapter(protocol_version: str, server_type: str | None = None) -> BaseAdapter:
    key = f"{protocol_version}:{server_type or 'auto'}"
    if key not in _registry:
        st = ServerType.AUTO
        if server_type in ("new", "funasr_main"):
            st = ServerType.FUNASR_MAIN
        elif server_type in ("old", "legacy"):
            st = ServerType.LEGACY
        _registry[key] = FunASRWebSocketAdapter(server_type=st)
    return _registry[key]


def clear_registry() -> None:
    _registry.clear()
