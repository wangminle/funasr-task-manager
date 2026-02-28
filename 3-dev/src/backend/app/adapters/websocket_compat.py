"""WebSocket compatibility layer.

Shields callers from differences across websockets library versions,
particularly the `proxy` parameter added in newer releases.

Ported and adapted from funasr-client-python/websocket_compat.py for async-only usage.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any

import websockets


def connect_websocket(uri: str, *, disable_proxy: bool = True, **kwargs: Any) -> Any:
    """Create a version-compatible ``websockets.connect`` call.

    Returns an object that can be used as ``async with connect_websocket(...) as ws:``.
    """
    connect_kwargs = dict(kwargs)

    if disable_proxy:
        connect_kwargs.setdefault("proxy", None)

    try:
        obj = websockets.connect(uri, **connect_kwargs)
    except TypeError as exc:
        if "proxy" in str(exc):
            connect_kwargs.pop("proxy", None)
            obj = websockets.connect(uri, **connect_kwargs)
        else:
            raise

    return _wrap_if_needed(obj)


def _wrap_if_needed(connect_obj: Any) -> Any:
    """Ensure the object supports ``async with``."""
    if hasattr(connect_obj, "__aenter__") and hasattr(connect_obj, "__aexit__"):
        return connect_obj

    if inspect.isawaitable(connect_obj):

        @asynccontextmanager
        async def _ctx() -> Any:
            ws = await connect_obj
            try:
                yield ws
            finally:
                close_fn = getattr(ws, "close", None)
                if callable(close_fn):
                    maybe = close_fn()
                    if inspect.isawaitable(maybe):
                        await maybe

        return _ctx()

    return connect_obj
