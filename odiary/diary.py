"""Diary file operations: parse, insert log entries, extract Logs section."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from clitools.config import diary_data_dir


_LOGS_HEADING_RE = re.compile(r"^#\s+Logs\s*$", re.MULTILINE)
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def diary_local_path(remote_path: str) -> Path:
    """Map a remote note path to its local cache file.

    e.g. "Logs/Diary/2026-08-12.md" -> ~/.local/share/fnss-clitools/diary/Logs/Diary/2026-08-12.md
    """
    p = (diary_data_dir() / remote_path).resolve()
    return p


def read_local(remote_path: str) -> Optional[str]:
    p = diary_local_path(remote_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _normalize(content: str) -> str:
    """Normalize content so cross-tool writes don't accumulate blank lines.

    - Collapse 3+ consecutive newlines down to 2 (max one blank line between
      entries).
    - Strip ALL trailing whitespace (including any final newline).

    Deliberately deviates from POSIX "files end with newline": the file
    ends with the last entry's content, no trailing newline. This matches
    what other plugins (QuickAdd-style) assume, so mixed writes don't
    accumulate stray blank lines.

    Result format:
        entry\\n
        \\n                       <- one blank line separator
        next_entry                 <- file ends here, NO trailing \\n
    """
    content = _MULTI_NEWLINE_RE.sub("\n\n", content)
    content = content.rstrip()
    return content


def write_local(remote_path: str, content: str) -> Path:
    """Write normalized content to local cache.

    Idempotent: collapse multi-blank lines and strip trailing whitespace.
    The file ends with the last entry's content (no trailing newline).
    """
    p = diary_local_path(remote_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_normalize(content), encoding="utf-8")
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