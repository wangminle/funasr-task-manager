"""Notify commands: send real-time messages to Feishu/Lark channels.

This module provides the CLI fallback for send_user_notice() when no platform
message tool (e.g. OpenClaw message) is available. It handles Feishu token
management, message delivery, and graceful failure modes.
"""

# Typer commands intentionally use typer.Option in function defaults.
# ruff: noqa: B008

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from contextlib import suppress
from pathlib import Path

import httpx
import typer

from cli import config_store
from cli import output as out

app = typer.Typer()

FEISHU_TOKEN_API = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_SEND_API = "https://open.feishu.cn/open-apis/im/v1/messages"
FEISHU_REPLY_API = "https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
FEISHU_UPLOAD_API = "https://open.feishu.cn/open-apis/im/v1/files"

TOKEN_CACHE_PATH = Path.home() / ".asr-cli-feishu-token.json"
FAILURE_LOG_PATH = Path.home() / ".asr-cli-notify-failures.log"

TOKEN_CACHE_DURATION_SEC = 110 * 60  # 110 minutes (Feishu token valid for 2h)
MAX_FAILURE_LOG_LINES = 100


def _get_credentials() -> tuple[str, str] | None:
    """Resolve Feishu app credentials from env > config > None."""
    app_id = os.environ.get("FEISHU_APP_ID") or config_store.get("notify.feishu_app_id")
    app_secret = os.environ.get("FEISHU_APP_SECRET") or config_store.get("notify.feishu_app_secret")

    if app_id and app_secret:
        return str(app_id), str(app_secret)
    return None


def _get_default_chat_id() -> str | None:
    """Resolve default chat_id from env > config."""
    return os.environ.get("FEISHU_CHAT_ID") or config_store.get("notify.default_chat_id") or None


def _get_default_reply_to() -> str | None:
    """Resolve default reply_to from env > config."""
    return os.environ.get("FEISHU_REPLY_TO") or config_store.get("notify.default_reply_to") or None


def _load_cached_token(app_id: str) -> str | None:
    """Load cached token if valid and matching app_id."""
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("app_id") != app_id:
            return None
        if time.time() >= data.get("expires_at", 0):
            return None
        return data.get("tenant_access_token")
    except (json.JSONDecodeError, OSError):
        return None


def _save_cached_token(app_id: str, token: str) -> None:
    """Save token to cache file."""
    data = {
        "tenant_access_token": token,
        "expires_at": time.time() + TOKEN_CACHE_DURATION_SEC,
        "app_id": app_id,
    }
    with suppress(OSError):
        TOKEN_CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")


def _fetch_token(app_id: str, app_secret: str) -> str | None:
    """Request a new tenant_access_token from Feishu API."""
    try:
        resp = httpx.post(
            FEISHU_TOKEN_API,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if body.get("code", -1) != 0:
            return None
        token = body.get("tenant_access_token")
        if token:
            _save_cached_token(app_id, token)
        return token
    except (httpx.HTTPError, ValueError):
        return None


def _get_token(app_id: str, app_secret: str) -> str | None:
    """Get token from cache or fetch new one."""
    token = _load_cached_token(app_id)
    if token:
        return token
    return _fetch_token(app_id, app_secret)


def _send_text_message(token: str, chat_id: str, text: str, reply_to: str | None = None) -> dict:
    """Send a text message via Feishu IM API. Returns response dict."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    content_json = json.dumps({"text": text}, ensure_ascii=False)

    if reply_to:
        url = FEISHU_REPLY_API.format(message_id=reply_to)
        payload = {
            "msg_type": "text",
            "content": content_json,
        }
    else:
        url = f"{FEISHU_SEND_API}?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": content_json,
        }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"code": -1, "msg": str(e)}


def _upload_file(token: str, file_path: str, filename: str | None = None) -> dict:
    """Upload a file to Feishu and return the response with file_key."""
    headers = {"Authorization": f"Bearer {token}"}
    local_path = Path(file_path)

    if not local_path.exists():
        return {"code": -1, "msg": f"File not found: {file_path}"}

    if not local_path.is_file():
        return {"code": -1, "msg": f"Not a regular file: {file_path}"}

    try:
        file_size = local_path.stat().st_size
    except OSError as e:
        return {"code": -1, "msg": f"Cannot stat file: {e}"}

    if file_size > 30 * 1024 * 1024:
        return {"code": -1, "msg": f"File too large ({file_size} bytes > 30MB limit)"}

    display_name = filename or local_path.name

    try:
        with open(local_path, "rb") as f:
            upload_resp = httpx.post(
                FEISHU_UPLOAD_API,
                headers=headers,
                data={"file_type": "stream", "file_name": display_name},
                files={"file": (display_name, f)},
                timeout=120,
            )
        upload_body = upload_resp.json()
        if upload_body.get("code", -1) != 0:
            return upload_body
        file_key = upload_body.get("data", {}).get("file_key")
        if not file_key:
            return {"code": -1, "msg": "No file_key in upload response"}
        return {"code": 0, "file_key": file_key}
    except OSError as e:
        return {"code": -1, "msg": f"Cannot read file: {e}"}
    except (httpx.HTTPError, ValueError) as e:
        return {"code": -1, "msg": f"Upload failed: {e}"}


def _send_file_message(
    token: str, chat_id: str, file_key: str, reply_to: str | None = None
) -> dict:
    """Send a file message via Feishu IM API using an already-uploaded file_key."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    content_json = json.dumps({"file_key": file_key})

    if reply_to:
        url = FEISHU_REPLY_API.format(message_id=reply_to)
        payload = {
            "msg_type": "file",
            "content": content_json,
        }
    else:
        url = f"{FEISHU_SEND_API}?receive_id_type=chat_id"
        payload = {
            "receive_id": chat_id,
            "msg_type": "file",
            "content": content_json,
        }

    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=15)
        return resp.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"code": -1, "msg": f"Send failed: {e}"}


def _log_failure(text_preview: str, error: str) -> None:
    """Append failure to log file (keep last MAX_FAILURE_LOG_LINES)."""
    entry = f"[{datetime.datetime.now().isoformat()}] {error} | text={text_preview[:80]}\n"
    try:
        lines: list[str] = []
        if FAILURE_LOG_PATH.exists():
            lines = FAILURE_LOG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
        lines.append(entry)
        if len(lines) > MAX_FAILURE_LOG_LINES:
            lines = lines[-MAX_FAILURE_LOG_LINES:]
        FAILURE_LOG_PATH.write_text("".join(lines), encoding="utf-8")
    except OSError:
        pass


def _do_send(text: str, chat_id: str, reply_to: str | None, strict: bool) -> None:
    """Core send logic with retry and exit-code handling."""
    creds = _get_credentials()
    if not creds:
        msg = "通知凭据未配置，跳过发送 (设置 FEISHU_APP_ID/FEISHU_APP_SECRET 或 cli config)"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    app_id, app_secret = creds
    token = _get_token(app_id, app_secret)
    if not token:
        msg = "飞书 token 获取失败，检查 app_id/app_secret 是否正确"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        _log_failure(text, "token_fetch_failed")
        return

    # Attempt send with 1 retry
    for attempt in range(2):
        result = _send_text_message(token, chat_id, text, reply_to)
        code = result.get("code", -1)

        if code == 0:
            message_id = result.get("data", {}).get("message_id", "")
            print(f"message_id={message_id}")
            return

        # Token expired - refresh and retry
        if code == 99991668 and attempt == 0:
            token = _fetch_token(app_id, app_secret)
            if not token:
                break
            continue

        # Other error on first attempt - retry once
        if attempt == 0:
            time.sleep(1)
            continue

    # All attempts failed
    error_msg = result.get("msg", "unknown error")
    msg = f"通知发送失败: code={code}, msg={error_msg}"
    _log_failure(text, msg)
    if strict:
        out.error(msg)
        raise typer.Exit(1)
    sys.stderr.write(f"[WARN] {msg}\n")


@app.command(name="send")
def send(
    text: str | None = typer.Option(None, "--text", "-t", help="消息文本内容"),
    text_file: Path | None = typer.Option(None, "--text-file", help="从文件读取消息内容"),
    stdin: bool = typer.Option(False, "--stdin", help="从标准输入读取消息内容"),
    chat_id: str | None = typer.Option(None, "--chat-id", "-c", help="目标会话 ID"),
    reply_to: str | None = typer.Option(None, "--reply-to", "-r", help="回复的消息 ID（线程回复）"),
    channel: str = typer.Option("feishu", "--channel", help="渠道类型"),
    strict: bool = typer.Option(False, "--strict", help="严格模式：失败时 exit 1"),
):
    """发送文本通知到飞书会话。"""
    # Resolve text content
    if stdin:
        content = sys.stdin.read().strip()
    elif text_file:
        if not text_file.exists():
            msg = f"文件不存在: {text_file}"
            if strict:
                out.error(msg)
                raise typer.Exit(1)
            sys.stderr.write(f"[WARN] {msg}\n")
            return
        content = text_file.read_text(encoding="utf-8").strip()
    elif text:
        content = text
    else:
        msg = "必须提供 --text、--text-file 或 --stdin 之一"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    if not content:
        msg = "通知内容为空，跳过发送"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    # Resolve reply_to first — reply API doesn't require chat_id
    resolved_reply_to = reply_to or _get_default_reply_to()

    # Resolve chat_id (not required when reply_to is present)
    resolved_chat_id = chat_id or _get_default_chat_id()
    if not resolved_chat_id and not resolved_reply_to:
        msg = "缺少 chat_id (设置 --chat-id / FEISHU_CHAT_ID / notify.default_chat_id，或提供 --reply-to)"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    if channel != "feishu":
        msg = f"渠道 '{channel}' 暂不支持，仅支持 feishu"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    _do_send(content, resolved_chat_id or "", resolved_reply_to, strict)


@app.command(name="send-file")
def send_file(
    file: Path = typer.Option(..., "--file", "-f", help="本地文件路径"),
    filename: str | None = typer.Option(None, "--filename", help="飞书显示的文件名"),
    text: str | None = typer.Option(None, "--text", "-t", help="随附文本消息（先发文本再发文件）"),
    chat_id: str | None = typer.Option(None, "--chat-id", "-c", help="目标会话 ID"),
    reply_to: str | None = typer.Option(None, "--reply-to", "-r", help="回复的消息 ID（线程回复）"),
    channel: str = typer.Option("feishu", "--channel", help="渠道类型"),
    strict: bool = typer.Option(False, "--strict", help="严格模式：失败时 exit 1"),
):
    """上传并发送文件附件到飞书会话。

    支持最大 30MB 文件。如果提供 --text，会先发送文本消息再发送文件。
    """
    if channel != "feishu":
        msg = f"渠道 '{channel}' 暂不支持，仅支持 feishu"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    resolved_reply_to = reply_to or _get_default_reply_to()

    resolved_chat_id = chat_id or _get_default_chat_id()
    if not resolved_chat_id and not resolved_reply_to:
        msg = "缺少 chat_id (设置 --chat-id / FEISHU_CHAT_ID / notify.default_chat_id，或提供 --reply-to)"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    creds = _get_credentials()
    if not creds:
        msg = "通知凭据未配置 (设置 FEISHU_APP_ID/FEISHU_APP_SECRET 或 cli config)"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    app_id, app_secret = creds
    token = _get_token(app_id, app_secret)
    if not token:
        msg = "飞书 token 获取失败，检查 app_id/app_secret 是否正确"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        _log_failure(str(file), "token_fetch_failed")
        return

    if not file.exists():
        msg = f"文件不存在: {file}"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    if not file.is_file():
        msg = f"路径不是常规文件: {file}"
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    # Send accompanying text first if provided
    if text:
        text_result = _send_text_message(token, resolved_chat_id, text, resolved_reply_to)
        if text_result.get("code", -1) != 0:
            sys.stderr.write(f"[WARN] 伴随文本发送失败: {text_result.get('msg', '')}\n")

    # Upload file with retry on token expiry
    for attempt in range(2):
        upload_result = _upload_file(token, str(file), filename)
        upload_code = upload_result.get("code", -1)

        if upload_code == 0:
            break

        if upload_code == 99991668 and attempt == 0:
            token = _fetch_token(app_id, app_secret)
            if not token:
                break
            continue

        if attempt == 0:
            time.sleep(1)
            continue
    else:
        error_msg = upload_result.get("msg", "unknown error")
        msg = f"文件上传失败: {error_msg}"
        _log_failure(str(file), msg)
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    if upload_result.get("code", -1) != 0:
        error_msg = upload_result.get("msg", "unknown error")
        msg = f"文件上传失败: {error_msg}"
        _log_failure(str(file), msg)
        if strict:
            out.error(msg)
            raise typer.Exit(1)
        sys.stderr.write(f"[WARN] {msg}\n")
        return

    file_key = upload_result["file_key"]

    # Send file message with retry on token expiry
    for attempt in range(2):
        send_result = _send_file_message(token, resolved_chat_id, file_key, resolved_reply_to)
        send_code = send_result.get("code", -1)

        if send_code == 0:
            message_id = send_result.get("data", {}).get("message_id", "")
            print(f"message_id={message_id}")
            return

        if send_code == 99991668 and attempt == 0:
            token = _fetch_token(app_id, app_secret)
            if not token:
                break
            continue

        if attempt == 0:
            time.sleep(1)
            continue

    error_msg = send_result.get("msg", "unknown error")
    msg = f"文件消息发送失败: code={send_result.get('code')}, msg={error_msg}"
    _log_failure(str(file), msg)
    if strict:
        out.error(msg)
        raise typer.Exit(1)
    sys.stderr.write(f"[WARN] {msg}\n")


@app.command(name="auth-check")
def auth_check(
    channel: str = typer.Option("feishu", "--channel", help="渠道类型"),
):
    """验证飞书通知凭据是否可用。失败时 exit 1。"""
    if channel != "feishu":
        out.error(f"渠道 '{channel}' 暂不支持")
        raise typer.Exit(1)

    creds = _get_credentials()
    if not creds:
        out.error(
            "飞书凭据未配置 "
            "(需要 FEISHU_APP_ID + FEISHU_APP_SECRET 或 notify.feishu_app_id + notify.feishu_app_secret)"
        )
        raise typer.Exit(1)

    app_id, app_secret = creds

    # Always fetch fresh token for auth-check
    token = _fetch_token(app_id, app_secret)
    if not token:
        out.error(f"飞书凭据无效: 无法获取 tenant_access_token (app_id: {app_id})")
        raise typer.Exit(1)

    chat_id = _get_default_chat_id()
    chat_info = f", default_chat_id: {chat_id}" if chat_id else ", default_chat_id: 未设置"
    out.success(f"飞书凭据有效 (app_id: {app_id}, token 已缓存{chat_info})")
