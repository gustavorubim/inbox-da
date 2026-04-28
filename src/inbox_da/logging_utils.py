"""Rich terminal and logging helpers."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

console = Console()


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True)],
        force=True,
    )


def show_header(title: str, subtitle: str) -> None:
    console.print(Panel.fit(subtitle, title=title, border_style="cyan"))


@contextmanager
def progress_step(label: str) -> Iterator[None]:
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task(label, total=None)
        try:
            yield
            progress.update(task_id, description=f"{label} complete")
        finally:
            progress.stop()


def print_key_values(title: str, values: dict[str, object]) -> None:
    table = Table(title=title, show_header=True)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    for key, value in values.items():
        table.add_row(key, str(value))
    console.print(table)
