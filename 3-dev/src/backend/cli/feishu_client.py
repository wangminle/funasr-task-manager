"""Feishu/Lark notification client.

Encapsulates credential resolution, token management, retry logic, and
message/file delivery. CLI commands delegate here after parsing arguments.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from cli import config_store

FEISHU_TOKEN_API = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_SEND_API = "https://open.feishu.cn/open-apis/im/v1/messages"
FEISHU_REPLY_API = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
FEISHU_UPLOAD_API = "https://open.feishu.cn/open-apis/im/v1/files"

TOKEN_CACHE_PATH = Path.home() / ".asr-cli-feishu-token.json"
TOKEN_CACHE_DURATION_SEC = 110 * 60


class NotifyError(Exception):
    """Notification operation failed."""


@dataclass(frozen=True)
class NotifyRoute:
    """Resolved message target."""

    chat_id: str
    reply_to: str | None = None
    receive_id_type: str = "chat_id"

    @classmethod
    def resolve(
        cls,
        chat_id: str | None = None,
        reply_to: str | None = None,
        receive_id_type: str = "chat_id",
    ) -> NotifyRoute:
        """Resolve route from explicit args + environment/config defaults."""
        resolved_reply_to = reply_to or (None if chat_id else _get_default_reply_to())
        resolved_chat_id = chat_id or _get_default_chat_id()
        if not resolved_chat_id and not resolved_reply_to:
            raise NotifyError(
                "缺少 chat_id (设置 --chat-id / FEISHU_CHAT_ID / notify.default_chat_id，或提供 --reply-to)"
            )
        return cls(
            chat_id=resolved_chat_id or "",
            reply_to=resolved_reply_to,
            receive_id_type=receive_id_type,
        )


def _get_default_chat_id() -> str | None:
    return os.environ.get("FEISHU_CHAT_ID") or config_store.get("notify.default_chat_id") or None


def _get_default_reply_to() -> str | None:
    return os.environ.get("FEISHU_REPLY_TO") or config_store.get("notify.default_reply_to") or None


def _normalize_chat_id(chat_id: str) -> str:
    for prefix in ("chat:", "Chat:", "CHAT:"):
        if chat_id.startswith(prefix):
            return chat_id[len(prefix):]
    return chat_id


class FeishuNotifyClient:
    """Feishu notification client with automatic token refresh and retry."""

    def __init__(
        self,
        token_cache_path: Path | None = None,
    ):
        self._token_cache_path = token_cache_path or TOKEN_CACHE_PATH
        self._app_id: str | None = None
        self._app_secret: str | None = None
        self._token: str | None = None

    def ensure_credentials(self) -> tuple[str, str]:
        """Resolve credentials or raise NotifyError."""
        app_id = os.environ.get("FEISHU_APP_ID") or config_store.get("notify.feishu_app_id")
        app_secret = os.environ.get("FEISHU_APP_SECRET") or config_store.get("notify.feishu_app_secret")
        if not (app_id and app_secret):
            raise NotifyError("通知凭据未配置 (设置 FEISHU_APP_ID/FEISHU_APP_SECRET 或 cli config)")
        self._app_id = str(app_id)
        self._app_secret = str(app_secret)
        return self._app_id, self._app_secret

    def get_token(self) -> str:
        """Get a valid token (cached or fresh). Raises NotifyError."""
        if not self._app_id or not self._app_secret:
            self.ensure_credentials()
        token = self._load_cached_token()
        if token:
            self._token = token
            return token
        return self._refresh_token()

    def _refresh_token(self) -> str:
        """Fetch a fresh token. Raises NotifyError on failure."""
        token = self._fetch_token_from_api()
        if not token:
            raise NotifyError("飞书 token 获取失败，检查 app_id/app_secret 是否正确")
        self._token = token
        return token

    def _fetch_token_from_api(self) -> str | None:
        try:
            resp = httpx.post(
                FEISHU_TOKEN_API,
                json={"app_id": self._app_id, "app_secret": self._app_secret},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            if body.get("code", -1) != 0:
                return None
            token = body.get("tenant_access_token")
            if token:
                self._save_cached_token(token)
            return token
        except (httpx.HTTPError, ValueError):
            return None

    def _load_cached_token(self) -> str | None:
        if not self._token_cache_path.exists():
            return None
        try:
            data = json.loads(self._token_cache_path.read_text(encoding="utf-8"))
            if data.get("app_id") != self._app_id:
                return None
            if time.time() >= data.get("expires_at", 0):
                return None
            return data.get("tenant_access_token")
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cached_token(self, token: str) -> None:
        data = {
            "tenant_access_token": token,
            "expires_at": time.time() + TOKEN_CACHE_DURATION_SEC,
            "app_id": self._app_id,
        }
        try:
            content = json.dumps(data)
            fd = os.open(str(self._token_cache_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
        except OSError:
            pass

    def with_retry(self, fn: Callable[[str], dict]) -> dict:
        """Execute fn(token) with one automatic token-refresh retry on 99991668.

        Returns the API response dict. Raises NotifyError if all attempts fail.
        """
        token = self.get_token()
        for attempt in range(2):
            result = fn(token)
            code = result.get("code", -1)
            if code == 0:
                return result
            if code == 99991668 and attempt == 0:
                token = self._refresh_token()
                continue
            if attempt == 0:
                time.sleep(1)
                continue
        raise NotifyError(f"code={result.get('code')}, msg={result.get('msg', 'unknown')}")

    def send_text(self, route: NotifyRoute, text: str) -> str:
        """Send text message. Returns message_id. Raises NotifyError."""
        def _call(token: str) -> dict:
            return _send_text_api(token, route.chat_id, text, route.reply_to, route.receive_id_type)

        result = self.with_retry(_call)
        return result.get("data", {}).get("message_id", "")

    def upload_file(self, file_path: Path, filename: str | None = None) -> str:
        """Upload file to Feishu. Returns file_key. Raises NotifyError."""
        if not file_path.exists():
            raise NotifyError(f"文件不存在: {file_path}")
        if not file_path.is_file():
            raise NotifyError(f"路径不是常规文件: {file_path}")
        try:
            file_size = file_path.stat().st_size
        except OSError as e:
            raise NotifyError(f"Cannot stat file: {e}") from e
        if file_size > 30 * 1024 * 1024:
            raise NotifyError(f"File too large ({file_size} bytes > 30MB limit)")

        display_name = filename or file_path.name

        def _call(token: str) -> dict:
            return _upload_file_api(token, file_path, display_name)

        result = self.with_retry(_call)
        file_key = result.get("file_key") or result.get("data", {}).get("file_key")
        if not file_key:
            raise NotifyError("No file_key in upload response")
        return file_key

    def send_file_message(self, route: NotifyRoute, file_key: str) -> str:
        """Send a file message by file_key. Returns message_id. Raises NotifyError."""
        def _call(token: str) -> dict:
            return _send_file_msg_api(token, route.chat_id, file_key, route.reply_to, route.receive_id_type)

        result = self.with_retry(_call)
        return result.get("data", {}).get("message_id", "")


# --- Low-level API functions (stateless, easily mockable) ---


def _send_text_api(
    token: str,
    chat_id: str,
    text: str,
    reply_to: str | None = None,
    receive_id_type: str = "chat_id",
) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content_json = json.dumps({"text": text}, ensure_ascii=False)

    if reply_to:
        url = FEISHU_REPLY_API.format(message_id=reply_to)
        payload = {"msg_type": "text", "content": content_json}
    else:
        url = f"{FEISHU_SEND_API}?receive_id_type={receive_id_type}"
        payload = {"receive_id": _normalize_chat_id(chat_id), "msg_type": "text", "content": content_json}

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"code": -1, "msg": str(e)}


def _upload_file_api(token: str, file_path: Path, display_name: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with open(file_path, "rb") as f:
            resp = httpx.post(
                FEISHU_UPLOAD_API,
                headers=headers,
                data={"file_type": "stream", "file_name": display_name},
                files={"file": (display_name, f)},
                timeout=120,
            )
        body = resp.json()
        if body.get("code", -1) != 0:
            return body
        file_key = body.get("data", {}).get("file_key")
        if not file_key:
            return {"code": -1, "msg": "No file_key in upload response"}
        return {"code": 0, "file_key": file_key}
    except OSError as e:
        return {"code": -1, "msg": f"Cannot read file: {e}"}
    except (httpx.HTTPError, ValueError) as e:
        return {"code": -1, "msg": f"Upload failed: {e}"}


def _send_file_msg_api(
    token: str,
    chat_id: str,
    file_key: str,
    reply_to: str | None = None,
    receive_id_type: str = "chat_id",
) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    content_json = json.dumps({"file_key": file_key})

    if reply_to:
        url = FEISHU_REPLY_API.format(message_id=reply_to)
        payload = {"msg_type": "file", "content": content_json}
    else:
        url = f"{FEISHU_SEND_API}?receive_id_type={receive_id_type}"
        payload = {"receive_id": _normalize_chat_id(chat_id), "msg_type": "file", "content": content_json}

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"code": -1, "msg": f"Send failed: {e}"}
