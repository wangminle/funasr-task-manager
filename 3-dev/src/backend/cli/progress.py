"""Progress bar and SSE stream utilities using Rich."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn

if TYPE_CHECKING:
    from cli.api_client import ASRClient

console = Console()


def wait_for_task(client: "ASRClient", task_id: str, poll_interval: float = 5.0,
                  timeout: float = 3600.0, quiet: bool = False) -> dict:
    """Poll task status until terminal state, with optional Rich progress bar."""
    terminal = {"SUCCEEDED", "FAILED", "CANCELED"}
    start = time.time()

    if quiet:
        while True:
            task = client.get_task(task_id)
            if task["status"] in terminal:
                return task
            if time.time() - start > timeout:
                raise TimeoutError(f"Task {task_id} did not finish within {timeout}s")
            time.sleep(poll_interval)

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[status]}"),
        BarColumn(bar_width=40),
        TextColumn("{task.percentage:>5.1f}%"),
        TimeRemainingColumn(),
        TextColumn("[dim]{task.fields[msg]}"),
        console=console,
    ) as progress:
        bar = progress.add_task(task_id, total=100, status="...", msg="")
        while True:
            task = client.get_task(task_id)
            pct = task.get("progress", 0) * 100
            eta = task.get("eta_seconds")
            msg = f"ETA ~{eta}s" if eta else ""
            progress.update(bar, completed=pct, status=task["status"], msg=msg)
            if task["status"] in terminal:
                progress.update(bar, completed=100 if task["status"] == "SUCCEEDED" else pct)
                return task
            if time.time() - start > timeout:
                raise TimeoutError(f"Task {task_id} did not finish within {timeout}s")
            time.sleep(poll_interval)


def stream_progress(client: "ASRClient", task_id: str) -> None:
    """Print SSE progress events to console."""
    try:
        for event in client.task_progress_stream(task_id):
            etype = event.get("event_type", "")
            pct = event.get("progress", 0) * 100
            msg = event.get("message", "")
            console.print(f"[cyan]{etype:15s}[/cyan]  {pct:5.1f}%  {msg}")
            if etype in ("SUCCEEDED", "FAILED", "CANCELED"):
                break
    except KeyboardInterrupt:
        console.print("[yellow]Stream interrupted[/yellow]")
