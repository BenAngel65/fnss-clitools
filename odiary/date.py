"""Date parsing utilities for odiary."""
from __future__ import annotations

import re
from datetime import date as _date
from typing import Optional


_DATE_PATTERNS = [
    "%Y-%m-%d",
    "%Y-%m-%d",
]


def looks_like_date(text: str) -> bool:
    """Heuristic check: is this arg a date like 2026-08-12 or 2026-8-12?"""
    if not text:
        return False
    return bool(re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text))


def parse_date(text: str) -> _date:
    """Parse YYYY-MM-DD or YYYY-M-D. Raises ValueError on bad input."""
    if not looks_like_date(text):
        raise ValueError(f"日期格式错误：{text!r}（应为 YYYY-MM-DD）")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if not m:
        raise ValueError(f"日期格式错误：{text!r}（应为 YYYY-MM-DD）")
    try:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError(f"日期格式错误：{text!r}（应为 YYYY-MM-DD）")


def today() -> _date:
    return _date.today()


def diary_filename(d: _date) -> str:
    """YYYY-MM-DD.md"""
    return f"{d.isoformat()}.md"