"""Local INBOX.md file operations."""
from __future__ import annotations

import os
from pathlib import Path

from clitools.config import DEFAULT_INBOX_HEADER, data_dir, local_inbox_path


def ensure_local() -> Path:
    """Make sure local inbox.md exists; create with header if missing."""
    p = local_inbox_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(DEFAULT_INBOX_HEADER, encoding="utf-8")
    return p


def read_local() -> str:
    p = local_inbox_path()
    if not p.exists():
        return DEFAULT_INBOX_HEADER
    return p.read_text(encoding="utf-8")


def write_local(content: str) -> Path:
    p = local_inbox_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    p.write_text(content, encoding="utf-8")
    return p


def append_entry(text: str) -> tuple[Path, str]:
    """Append a task line to local inbox.md.

    Returns (path, formatted_entry_line).
    """
    entry = f"- [ ] {text}"
    p = ensure_local()
    with open(p, "a", encoding="utf-8") as f:
        f.write(entry + "\n")
    return p, entry