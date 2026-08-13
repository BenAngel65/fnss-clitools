"""Diary file operations: parse, insert log entries, extract Logs section."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from clitools.config import diary_data_dir


_LOGS_HEADING_RE = re.compile(r"^#\s+Logs\s*$", re.MULTILINE)


def diary_local_path(remote_path: str) -> Path:
    """Map a remote note path to its local cache path.

    e.g. "Logs/Diary/2026-08-12.md" -> ~/.local/share/fnss-clitools/diary/Logs/Diary/2026-08-12.md
    """
    p = (diary_data_dir() / remote_path).resolve()
    return p


def read_local(remote_path: str) -> Optional[str]:
    p = diary_local_path(remote_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def write_local(remote_path: str, content: str) -> Path:
    p = diary_local_path(remote_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    p.write_text(content, encoding="utf-8")
    return p


def has_logs_section(content: str) -> bool:
    return bool(_LOGS_HEADING_RE.search(content))


def insert_log_entry(content: str, timestamp: str, text: str) -> str:
    """Append a log entry under # Logs section, preserving everything else.

    # Logs is the last section in the daily-note template, so we simply append
    to the end of the file with a blank-line separator.

    Raises ValueError if # Logs heading is missing.
    """
    if not has_logs_section(content):
        raise ValueError("日记中没有 # Logs 标题")
    entry = f"- ⌚{timestamp} {text}"
    return content.rstrip() + f"\n\n{entry}\n"


def extract_logs_section(content: str) -> str:
    """Return the markdown starting from # Logs heading to EOF."""
    match = _LOGS_HEADING_RE.search(content)
    if not match:
        return "# Logs\n\n*(未找到 # Logs 标题)*"
    return content[match.start():].rstrip() + "\n"


def extract_existing_entries(content: str) -> list[str]:
    """Return all existing `- ⌚HH:MM <text>` lines under # Logs."""
    logs = extract_logs_section(content)
    entries: list[str] = []
    for line in logs.splitlines():
        m = re.match(r"^- ⌚(\d{1,2}:\d{2})\s+(.*)$", line)
        if m:
            entries.append(line)
    return entries