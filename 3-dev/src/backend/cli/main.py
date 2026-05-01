"""ASR CLI main entry point - Typer app with global options."""

from __future__ import annotations

import os
from typing import Optional

import typer

from cli import __version__
from cli import config_store
from cli.api_client import ASRClient

app = typer.Typer(
    name="asr-cli",
    help="ASR Task Manager CLI - 语音识别任务管理命令行工具",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    invoke_without_command=True,
)


class _Ctx:
    """Shared state passed through Typer context."""
    client: ASRClient
    output_format: str
    quiet: bool


def _resolve(cli_val: str | None, env_key: str, config_key: str, default: str) -> str:
    """Resolve option: CLI flag > env var > config file > default."""
    if cli_val:
        return cli_val
    env = os.environ.get(env_key)
    if env:
        return env
    stored = config_store.get(config_key)
    if stored:
        return str(stored)
    return default


@app.callback()
def main(
    ctx: typer.Context,
    server: Optional[str] = typer.Option(None, "--server", "-s", help="API 服务地址"),
    api_key: Optional[str] = typer.Option(None, "--api-key", "-k", help="API 认证 Token"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="输出格式: table/json/text"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="静默模式"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="调试模式"),
    timeout: float = typer.Option(30.0, "--timeout", help="HTTP 请求超时(秒)"),
    version: bool = typer.Option(False, "--version", is_eager=True, help="显示版本号"),
):
    if version:
        typer.echo(f"asr-cli {__version__}")
        raise typer.Exit()

    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()

    resolved_server = _resolve(server, "ASR_API_SERVER", "server", "http://localhost:15797")
    resolved_key = _resolve(api_key, "ASR_API_KEY", "api_key", "")
    resolved_output = _resolve(output, "ASR_OUTPUT_FORMAT", "output", "table")

    if verbose:
        typer.echo(f"Server: {resolved_server}", err=True)
        typer.echo(f"API Key: {'***' if resolved_key else '(none)'}", err=True)

    obj = _Ctx()
    obj.client = ASRClient(
        base_url=resolved_server,
        api_key=resolved_key or None,
        timeout=timeout,
    )
    obj.output_format = resolved_output
    obj.quiet = quiet
    ctx.obj = obj


def get_ctx(ctx: typer.Context) -> _Ctx:
    return ctx.obj


# --- Register sub-commands ---
from cli.commands import upload as _upload_mod  # noqa: E402
from cli.commands import file as _file_mod  # noqa: E402
from cli.commands import task as _task_mod  # noqa: E402
from cli.commands import transcribe as _transcribe_mod  # noqa: E402
from cli.commands import server as _server_mod  # noqa: E402
from cli.commands import system as _system_mod  # noqa: E402
from cli.commands import config_cmd as _config_mod  # noqa: E402

app.add_typer(_task_mod.app, name="task", help="任务管理")
app.add_typer(_file_mod.app, name="file", help="文件查询")
app.add_typer(_server_mod.app, name="server", help="ASR 节点管理")
app.add_typer(_config_mod.app, name="config", help="CLI 配置管理")

app.command(name="upload")(_upload_mod.upload)
app.command(name="transcribe")(_transcribe_mod.transcribe)
app.command(name="health")(_system_mod.health)
app.command(name="stats")(_system_mod.stats)
app.command(name="metrics")(_system_mod.metrics)
app.command(name="doctor")(_system_mod.doctor)
