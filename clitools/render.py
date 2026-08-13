"""Markdown rendering with rich."""
from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown

console = Console()


def render_markdown(content: str) -> None:
    """Print markdown content with rich's terminal renderer."""
    if not content or not content.strip():
        console.print(
            "[dim]inbox.md 为空。使用 `oinbox <内容>` 添加第一条记录。[/dim]"
        )
        return
    md = Markdown(content)
    console.print(md)


def render_error(msg: str) -> None:
    console.print(f"[bold red]✗ {msg}[/bold red]")


def render_warning(msg: str) -> None:
    console.print(f"[bold yellow]⚠ {msg}[/bold yellow]")


def render_success(msg: str) -> None:
    console.print(f"[bold green]✓ {msg}[/bold green]")


def render_info(msg: str) -> None:
    console.print(f"[cyan]⏫ {msg}[/cyan]")