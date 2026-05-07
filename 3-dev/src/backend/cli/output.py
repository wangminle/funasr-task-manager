"""Output formatting: table / json / text modes using Rich."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()
err_console = Console(stderr=True)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def print_text(rows: list[list[str]]) -> None:
    for row in rows:
        print("\t".join(str(c) for c in row))


def print_table(title: str, columns: list[str], rows: list[list[str]], footer: str | None = None) -> None:
    table = Table(title=title, show_lines=False)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(c) for c in row])
    console.print(table)
    if footer:
        console.print(f"[dim]{footer}[/dim]")


def render(fmt: str, *, data: Any = None, title: str = "", columns: list[str] | None = None,
           rows: list[list[str]] | None = None, footer: str | None = None) -> None:
    """Unified output dispatcher."""
    if fmt == "json":
        print_json(data)
    elif fmt == "text":
        if rows:
            print_text(rows)
        else:
            print(json.dumps(data, ensure_ascii=False, default=str) if data else "")
    else:
        if columns and rows is not None:
            print_table(title, columns, rows, footer)
        elif data:
            print_json(data)


def error(msg: str) -> None:
    err_console.print(f"[red bold]Error:[/red bold] {msg}")


def success(msg: str) -> None:
    err_console.print(f"[green]{msg}[/green]")


def info(msg: str) -> None:
    err_console.print(f"[dim]{msg}[/dim]")
