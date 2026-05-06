"""Unit tests for CLI notify command (Feishu real-time notification)."""

# Local imports inside tests keep patched module paths explicit.
# ruff: noqa: I001

import json
import time
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _patch_paths(tmp_path):
    """Redirect config, token cache, and failure log to temp dir."""
    import cli.config_store as cs
    import cli.commands.notify as notify

    original_config = cs.CONFIG_PATH
    original_token = notify.TOKEN_CACHE_PATH
    original_log = notify.FAILURE_LOG_PATH

    cs.CONFIG_PATH = tmp_path / ".asr-cli.yaml"
    notify.TOKEN_CACHE_PATH = tmp_path / ".asr-cli-feishu-token.json"
    notify.FAILURE_LOG_PATH = tmp_path / ".asr-cli-notify-failures.log"

    yield tmp_path

    cs.CONFIG_PATH = original_config
    notify.TOKEN_CACHE_PATH = original_token
    notify.FAILURE_LOG_PATH = original_log


@pytest.fixture
def set_env(monkeypatch):
    """Helper to set Feishu env vars."""
    def _set(app_id="test_app", app_secret="test_secret", chat_id="oc_test123"):
        monkeypatch.setenv("FEISHU_APP_ID", app_id)
        monkeypatch.setenv("FEISHU_APP_SECRET", app_secret)
        monkeypatch.setenv("FEISHU_CHAT_ID", chat_id)
    return _set


class TestCredentials:
    def test_get_credentials_from_env(self, set_env):
        set_env()
        from cli.commands.notify import _get_credentials
        creds = _get_credentials()
        assert creds == ("test_app", "test_secret")

    def test_get_credentials_from_config(self, _patch_paths):
        from cli.config_store import set_value
        from cli.commands.notify import _get_credentials
        set_value("notify.feishu_app_id", "config_app")
        set_value("notify.feishu_app_secret", "config_secret")
        creds = _get_credentials()
        assert creds == ("config_app", "config_secret")

    def test_get_credentials_missing(self):
        from cli.commands.notify import _get_credentials
        creds = _get_credentials()
        assert creds is None

    def test_env_overrides_config(self, set_env, _patch_paths):
        from cli.config_store import set_value
        from cli.commands.notify import _get_credentials
        set_value("notify.feishu_app_id", "config_app")
        set_value("notify.feishu_app_secret", "config_secret")
        set_env(app_id="env_app", app_secret="env_secret")
        creds = _get_credentials()
        assert creds == ("env_app", "env_secret")


class TestTokenCache:
    def test_save_and_load_token(self, _patch_paths):
        from cli.commands.notify import _save_cached_token, _load_cached_token
        _save_cached_token("app1", "token_abc")
        result = _load_cached_token("app1")
        assert result == "token_abc"

    def test_load_expired_token(self, _patch_paths):
        from cli.commands.notify import _load_cached_token, TOKEN_CACHE_PATH
        data = {
            "tenant_access_token": "old_token",
            "expires_at": time.time() - 100,
            "app_id": "app1",
        }
        TOKEN_CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
        result = _load_cached_token("app1")
        assert result is None

    def test_load_wrong_app_id(self, _patch_paths):
        from cli.commands.notify import _save_cached_token, _load_cached_token
        _save_cached_token("app1", "token_abc")
        result = _load_cached_token("app2")
        assert result is None

    def test_load_corrupted_cache(self, _patch_paths):
        from cli.commands.notify import _load_cached_token, TOKEN_CACHE_PATH
        TOKEN_CACHE_PATH.write_text("not valid json", encoding="utf-8")
        result = _load_cached_token("app1")
        assert result is None

    def test_load_missing_cache(self, _patch_paths):
        from cli.commands.notify import _load_cached_token
        result = _load_cached_token("app1")
        assert result is None


class TestFetchToken:
    @patch("cli.commands.notify.httpx.post")
    def test_fetch_token_success(self, mock_post, _patch_paths):
        from cli.commands.notify import _fetch_token, _load_cached_token
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "code": 0,
            "tenant_access_token": "t-fresh",
        }
        mock_post.return_value = mock_resp

        token = _fetch_token("app1", "secret1")
        assert token == "t-fresh"
        # Should be cached
        assert _load_cached_token("app1") == "t-fresh"

    @patch("cli.commands.notify.httpx.post")
    def test_fetch_token_api_error(self, mock_post):
        from cli.commands.notify import _fetch_token
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"code": 10003, "msg": "invalid app_id"}
        mock_post.return_value = mock_resp

        token = _fetch_token("bad_app", "bad_secret")
        assert token is None

    @patch("cli.commands.notify.httpx.post")
    def test_fetch_token_network_error(self, mock_post):
        import httpx
        from cli.commands.notify import _fetch_token
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        token = _fetch_token("app1", "secret1")
        assert token is None


class TestSendTextMessage:
    @patch("cli.commands.notify.httpx.post")
    def test_send_success(self, mock_post):
        from cli.commands.notify import _send_text_message
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": {"message_id": "om_test123"},
        }
        mock_post.return_value = mock_resp

        result = _send_text_message("token", "oc_chat", "Hello 你好")
        assert result["code"] == 0
        assert result["data"]["message_id"] == "om_test123"

        call_kwargs = mock_post.call_args
        assert "receive_id_type=chat_id" in call_kwargs[0][0]

    @patch("cli.commands.notify.httpx.post")
    def test_send_with_reply_to(self, mock_post):
        from cli.commands.notify import _send_text_message
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "data": {"message_id": "om_reply"}}
        mock_post.return_value = mock_resp

        result = _send_text_message("token", "oc_chat", "reply text", reply_to="om_original")
        assert result["code"] == 0

        call_url = mock_post.call_args[0][0]
        assert "om_original/reply" in call_url

    @patch("cli.commands.notify.httpx.post")
    def test_send_chinese_encoding(self, mock_post):
        from cli.commands.notify import _send_text_message
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"code": 0, "data": {"message_id": "om_cn"}}
        mock_post.return_value = mock_resp

        _send_text_message("token", "oc_chat", "⏳ 正在从飞书下载文件...")
        call_kwargs = mock_post.call_args[1]
        payload = call_kwargs["json"]
        content = json.loads(payload["content"])
        assert "正在从飞书下载文件" in content["text"]

    @patch("cli.commands.notify.httpx.post")
    def test_send_network_error(self, mock_post):
        import httpx
        from cli.commands.notify import _send_text_message
        mock_post.side_effect = httpx.ConnectError("timeout")

        result = _send_text_message("token", "oc_chat", "test")
        assert result["code"] == -1
        assert "timeout" in result["msg"]


class TestDoSend:
    @patch("cli.commands.notify._send_text_message")
    @patch("cli.commands.notify._get_token")
    def test_soft_fail_no_credentials(self, mock_token, mock_send, capsys):
        from cli.commands.notify import _do_send
        _do_send("hello", "oc_chat", None, strict=False)
        mock_send.assert_not_called()
        captured = capsys.readouterr()
        assert "凭据未配置" in captured.err

    @patch("cli.commands.notify._get_credentials")
    @patch("cli.commands.notify._get_token")
    @patch("cli.commands.notify._send_text_message")
    def test_retry_on_failure(self, mock_send, mock_token, mock_creds, capsys):
        mock_creds.return_value = ("app", "secret")
        mock_token.return_value = "token123"
        mock_send.side_effect = [
            {"code": 500, "msg": "internal error"},
            {"code": 0, "data": {"message_id": "om_ok"}},
        ]

        _do_send_fn = __import__("cli.commands.notify", fromlist=["_do_send"])._do_send
        from cli.commands.notify import _do_send
        _do_send("hello", "oc_chat", None, strict=False)

        assert mock_send.call_count == 2
        captured = capsys.readouterr()
        assert "om_ok" in captured.out

    @patch("cli.commands.notify._get_credentials")
    @patch("cli.commands.notify._get_token")
    @patch("cli.commands.notify._send_text_message")
    def test_token_refresh_on_99991668(self, mock_send, mock_token, mock_creds):
        from cli.commands.notify import _do_send
        mock_creds.return_value = ("app", "secret")
        mock_token.return_value = "old_token"

        mock_send.side_effect = [
            {"code": 99991668, "msg": "token expired"},
            {"code": 0, "data": {"message_id": "om_refreshed"}},
        ]

        with patch("cli.commands.notify._fetch_token", return_value="new_token"):
            _do_send("hello", "oc_chat", None, strict=False)

        assert mock_send.call_count == 2


class TestFailureLog:
    def test_log_failure_creates_file(self, _patch_paths):
        from cli.commands.notify import _log_failure, FAILURE_LOG_PATH
        _log_failure("test message", "some error")
        assert FAILURE_LOG_PATH.exists()
        content = FAILURE_LOG_PATH.read_text(encoding="utf-8")
        assert "some error" in content
        assert "test message" in content

    def test_log_failure_truncates(self, _patch_paths):
        from cli.commands.notify import _log_failure, FAILURE_LOG_PATH, MAX_FAILURE_LOG_LINES
        for i in range(MAX_FAILURE_LOG_LINES + 20):
            _log_failure(f"msg_{i}", f"err_{i}")
        lines = FAILURE_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == MAX_FAILURE_LOG_LINES

    def test_log_truncates_long_text(self, _patch_paths):
        from cli.commands.notify import _log_failure, FAILURE_LOG_PATH
        long_text = "A" * 200
        _log_failure(long_text, "error")
        content = FAILURE_LOG_PATH.read_text(encoding="utf-8")
        assert "A" * 80 in content
        assert "A" * 81 not in content

    def test_log_failure_redacts_tokens_and_secret(self, _patch_paths):
        from cli.config_store import set_value
        from cli.commands.notify import _log_failure, FAILURE_LOG_PATH

        set_value("notify.feishu_app_secret", "secret-value-123")
        _log_failure(
            "body with Bearer t-text-token and secret-value-123",
            (
                "Send failed: Authorization: Bearer t-access-token; "
                "tenant_access_token=t-tenant-token app_secret=secret-value-123"
            ),
        )

        content = FAILURE_LOG_PATH.read_text(encoding="utf-8")
        assert "t-access-token" not in content
        assert "t-tenant-token" not in content
        assert "t-text-token" not in content
        assert "secret-value-123" not in content
        assert "Bearer [REDACTED]" in content


class TestConfigStoreNested:
    def test_set_and_get_nested(self, _patch_paths):
        from cli.config_store import set_value, get
        set_value("notify.feishu_app_id", "nested_app")
        assert get("notify.feishu_app_id") == "nested_app"

    def test_nested_does_not_break_top_level(self, _patch_paths):
        from cli.config_store import set_value, get
        set_value("server", "http://custom:8080")
        set_value("notify.feishu_app_id", "app123")
        assert get("server") == "http://custom:8080"
        assert get("notify.feishu_app_id") == "app123"

    def test_multiple_nested_keys(self, _patch_paths):
        from cli.config_store import set_value, get
        set_value("notify.feishu_app_id", "app1")
        set_value("notify.feishu_app_secret", "secret1")
        set_value("notify.default_chat_id", "oc_test")
        assert get("notify.feishu_app_id") == "app1"
        assert get("notify.feishu_app_secret") == "secret1"
        assert get("notify.default_chat_id") == "oc_test"


class TestCLICommand:
    """Integration tests using Typer test runner."""

    def test_send_no_text_soft_fail(self, _patch_paths):
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send"])
        assert result.exit_code == 0

    def test_send_no_text_strict_fail(self, _patch_paths):
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send", "--strict"])
        assert result.exit_code == 1

    def test_auth_check_no_creds(self, _patch_paths):
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["auth-check"])
        assert result.exit_code == 1
        assert "未配置" in result.stdout or "未配置" in (result.stderr or "")

    @patch("cli.commands.notify._fetch_token")
    def test_auth_check_success(self, mock_fetch, set_env, _patch_paths):
        set_env()
        mock_fetch.return_value = "t-valid"
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["auth-check"])
        assert result.exit_code == 0
        assert "有效" in result.stdout

    @patch("cli.commands.notify._do_send")
    def test_send_text_invokes_do_send(self, mock_do_send, set_env, _patch_paths):
        set_env()
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send", "--text", "hello world"])
        assert result.exit_code == 0
        mock_do_send.assert_called_once()
        args = mock_do_send.call_args[0]
        assert args[0] == "hello world"
        assert args[1] == "oc_test123"

    @patch("cli.commands.notify._do_send")
    def test_send_text_file(self, mock_do_send, set_env, _patch_paths, tmp_path):
        set_env()
        text_file = tmp_path / "notice.txt"
        text_file.write_text("多行\n通知内容", encoding="utf-8")
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send", "--text-file", str(text_file)])
        assert result.exit_code == 0
        mock_do_send.assert_called_once()
        args = mock_do_send.call_args[0]
        assert "多行" in args[0]
        assert "通知内容" in args[0]

    @patch("cli.commands.notify._do_send")
    def test_send_stdin(self, mock_do_send, set_env, _patch_paths):
        set_env()
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send", "--stdin"], input="stdin 内容\n")
        assert result.exit_code == 0
        mock_do_send.assert_called_once()
        args = mock_do_send.call_args[0]
        assert "stdin 内容" in args[0]

    def test_send_empty_text_strict_fail(self, _patch_paths, tmp_path):
        text_file = tmp_path / "empty.txt"
        text_file.write_text("   \n", encoding="utf-8")

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send", "--text-file", str(text_file), "--chat-id", "oc_chat", "--strict"])
        assert result.exit_code == 1

    def test_send_unsupported_channel_strict_fail(self, set_env, _patch_paths):
        set_env()
        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send", "--text", "hello", "--channel", "slack", "--strict"])
        assert result.exit_code == 1


class TestUploadFile:
    """Tests for _upload_file helper function."""

    @patch("cli.commands.notify.httpx.post")
    def test_upload_success(self, mock_post, _patch_paths, tmp_path):
        from cli.commands.notify import _upload_file
        test_file = tmp_path / "result.txt"
        test_file.write_text("转写结果内容", encoding="utf-8")

        mock_post.return_value = MagicMock(
            json=lambda: {"code": 0, "data": {"file_key": "fk_abc123"}}
        )
        result = _upload_file("t-token", str(test_file))
        assert result == {"code": 0, "file_key": "fk_abc123"}
        mock_post.assert_called_once()

    def test_upload_file_not_found(self, _patch_paths):
        from cli.commands.notify import _upload_file
        result = _upload_file("t-token", "/nonexistent/file.txt")
        assert result["code"] == -1
        assert "not found" in result["msg"].lower()

    def test_upload_file_too_large(self, _patch_paths, tmp_path):
        from cli.commands.notify import _upload_file
        large_file = tmp_path / "huge.bin"
        large_file.write_bytes(b"x" * (31 * 1024 * 1024))
        result = _upload_file("t-token", str(large_file))
        assert result["code"] == -1
        assert "30MB" in result["msg"]

    @patch("cli.commands.notify.httpx.post")
    def test_upload_api_error(self, mock_post, _patch_paths, tmp_path):
        from cli.commands.notify import _upload_file
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_post.return_value = MagicMock(
            json=lambda: {"code": 230009, "msg": "permission denied"}
        )
        result = _upload_file("t-token", str(test_file))
        assert result["code"] == 230009

    @patch("cli.commands.notify.httpx.post")
    def test_upload_network_error(self, mock_post, _patch_paths, tmp_path):
        import httpx
        from cli.commands.notify import _upload_file
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_post.side_effect = httpx.ConnectError("connection refused")
        result = _upload_file("t-token", str(test_file))
        assert result["code"] == -1
        assert "Upload failed" in result["msg"]


class TestSendFileMessage:
    """Tests for _send_file_message helper function."""

    @patch("cli.commands.notify.httpx.post")
    def test_send_file_message_success(self, mock_post, _patch_paths):
        from cli.commands.notify import _send_file_message
        mock_post.return_value = MagicMock(
            json=lambda: {"code": 0, "data": {"message_id": "om_file_msg_1"}}
        )
        result = _send_file_message("t-token", "oc_chat1", "fk_abc123")
        assert result["code"] == 0
        assert result["data"]["message_id"] == "om_file_msg_1"
        call_kwargs = mock_post.call_args
        assert "receive_id_type=chat_id" in call_kwargs[0][0]

    @patch("cli.commands.notify.httpx.post")
    def test_send_file_message_reply_to(self, mock_post, _patch_paths):
        from cli.commands.notify import _send_file_message
        mock_post.return_value = MagicMock(
            json=lambda: {"code": 0, "data": {"message_id": "om_reply_1"}}
        )
        result = _send_file_message("t-token", "oc_chat1", "fk_abc123", reply_to="om_parent")
        assert result["code"] == 0
        call_url = mock_post.call_args[0][0]
        assert "om_parent" in call_url
        assert "reply" in call_url

    @patch("cli.commands.notify.httpx.post")
    def test_send_file_message_network_error(self, mock_post, _patch_paths):
        import httpx
        from cli.commands.notify import _send_file_message
        mock_post.side_effect = httpx.ConnectError("timeout")
        result = _send_file_message("t-token", "oc_chat1", "fk_abc123")
        assert result["code"] == -1
        assert "Send failed" in result["msg"]


class TestSendFileCommand:
    """Integration tests for the send-file CLI command."""

    @patch("cli.commands.notify._send_file_message")
    @patch("cli.commands.notify._upload_file")
    @patch("cli.commands.notify._get_token")
    def test_send_file_success(self, mock_token, mock_upload, mock_send_msg, set_env, _patch_paths, tmp_path):
        set_env()
        test_file = tmp_path / "转写结果.txt"
        test_file.write_text("转写内容", encoding="utf-8")

        mock_token.return_value = "t-valid"
        mock_upload.return_value = {"code": 0, "file_key": "fk_test"}
        mock_send_msg.return_value = {"code": 0, "data": {"message_id": "om_sent1"}}

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file)])
        assert result.exit_code == 0
        assert "om_sent1" in result.stdout

    @patch("cli.commands.notify._send_text_message")
    @patch("cli.commands.notify._send_file_message")
    @patch("cli.commands.notify._upload_file")
    @patch("cli.commands.notify._get_token")
    def test_send_file_with_text(
        self, mock_token, mock_upload, mock_send_msg, mock_text, set_env, _patch_paths, tmp_path
    ):
        set_env()
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_token.return_value = "t-valid"
        mock_upload.return_value = {"code": 0, "file_key": "fk_test"}
        mock_send_msg.return_value = {"code": 0, "data": {"message_id": "om_sent2"}}
        mock_text.return_value = {"code": 0, "data": {"message_id": "om_text"}}

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file), "--text", "✅ 转写完成"])
        assert result.exit_code == 0
        mock_text.assert_called_once()
        text_args = mock_text.call_args
        assert "转写完成" in text_args[0][2]

    @patch("cli.commands.notify._get_token")
    def test_send_file_not_found_soft_fail(self, mock_token, set_env, _patch_paths):
        set_env()
        mock_token.return_value = "t-valid"

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", "/no/such/file.txt"])
        assert result.exit_code == 0

    @patch("cli.commands.notify._get_token")
    def test_send_file_not_found_strict(self, mock_token, set_env, _patch_paths):
        set_env()
        mock_token.return_value = "t-valid"

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", "/no/such/file.txt", "--strict"])
        assert result.exit_code == 1

    def test_send_file_no_creds_soft_fail(self, _patch_paths, tmp_path):
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file)])
        assert result.exit_code == 0

    def test_send_file_no_creds_strict(self, _patch_paths, tmp_path):
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file), "--strict"])
        assert result.exit_code == 1

    def test_send_file_no_chat_id(self, monkeypatch, _patch_paths, tmp_path):
        monkeypatch.setenv("FEISHU_APP_ID", "test_app")
        monkeypatch.setenv("FEISHU_APP_SECRET", "test_secret")
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file)])
        assert result.exit_code == 0

    def test_send_file_unsupported_channel_strict_fail(self, _patch_paths, tmp_path):
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file), "--channel", "slack", "--strict"])
        assert result.exit_code == 1

    @patch("cli.commands.notify._send_file_message")
    @patch("cli.commands.notify._upload_file")
    @patch("cli.commands.notify._fetch_token")
    @patch("cli.commands.notify._get_token")
    def test_send_file_token_refresh_on_upload(
        self, mock_get_token, mock_fetch, mock_upload, mock_send_msg, set_env, _patch_paths, tmp_path
    ):
        """Token expiry during upload triggers refresh and retry."""
        set_env()
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_get_token.return_value = "t-expired"
        mock_fetch.return_value = "t-refreshed"
        mock_upload.side_effect = [
            {"code": 99991668, "msg": "token expired"},
            {"code": 0, "file_key": "fk_refreshed"},
        ]
        mock_send_msg.return_value = {"code": 0, "data": {"message_id": "om_after_refresh"}}

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file)])
        assert result.exit_code == 0
        assert "om_after_refresh" in result.stdout
        mock_fetch.assert_called_once()

    @patch("cli.commands.notify._send_file_message")
    @patch("cli.commands.notify._upload_file")
    @patch("cli.commands.notify._get_token")
    def test_send_file_with_reply_to(self, mock_token, mock_upload, mock_send_msg, set_env, _patch_paths, tmp_path):
        set_env()
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_token.return_value = "t-valid"
        mock_upload.return_value = {"code": 0, "file_key": "fk_test"}
        mock_send_msg.return_value = {"code": 0, "data": {"message_id": "om_reply"}}

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file), "--reply-to", "om_parent_msg"])
        assert result.exit_code == 0
        send_call_args = mock_send_msg.call_args[0]
        assert send_call_args[3] == "om_parent_msg"

    @patch("cli.commands.notify._send_file_message")
    @patch("cli.commands.notify._upload_file")
    @patch("cli.commands.notify._get_token")
    def test_send_file_custom_filename(self, mock_token, mock_upload, mock_send_msg, set_env, _patch_paths, tmp_path):
        set_env()
        test_file = tmp_path / "raw_output.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_token.return_value = "t-valid"
        mock_upload.return_value = {"code": 0, "file_key": "fk_test"}
        mock_send_msg.return_value = {"code": 0, "data": {"message_id": "om_named"}}

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file), "--filename", "会议记录.txt"])
        assert result.exit_code == 0
        upload_call_args = mock_upload.call_args[0]
        assert upload_call_args[2] == "会议记录.txt"

    @patch("cli.commands.notify._send_file_message")
    @patch("cli.commands.notify._upload_file")
    @patch("cli.commands.notify._get_token")
    def test_send_file_upload_failure_strict(
        self, mock_token, mock_upload, mock_send_msg, set_env, _patch_paths, tmp_path
    ):
        set_env()
        test_file = tmp_path / "result.txt"
        test_file.write_text("内容", encoding="utf-8")

        mock_token.return_value = "t-valid"
        mock_upload.return_value = {"code": 230009, "msg": "permission denied"}

        from typer.testing import CliRunner
        from cli.commands.notify import app
        runner = CliRunner()
        result = runner.invoke(app, ["send-file", "--file", str(test_file), "--strict"])
        assert result.exit_code == 1
