"""odiary sync logic: pull per-day files, push pending entries."""
from __future__ import annotations

import json
import re
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from clitools.config import data_dir, diary_data_dir, is_configured, load_config
from clitools.editor import edit_with_fallback as _editor_edit
from clitools.fnss import FnssClient, FnssError
from clitools.render import (
    console,
    render_error,
    render_info,
    render_success,
    render_warning,
)
from . import diary as diary_ops
from .date import diary_filename


def diary_pending_path() -> Path:
    return diary_data_dir() / "pending.json"


def load_pending() -> list[dict]:
    p = diary_pending_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_pending(items: list[dict]) -> None:
    p = diary_pending_path()
    if not items:
        if p.exists():
            p.unlink()
        return
    p.write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def queue_pending(date_str: str, entries: list[str]) -> None:
    if not entries:
        return
    items = load_pending()
    now = datetime.now().isoformat(timespec="seconds")
    for entry in entries:
        items.append({"date": date_str, "entry": entry, "queued_at": now})
    save_pending(items)


def make_client() -> Optional[FnssClient]:
    cfg = load_config()
    if not is_configured(cfg):
        return None
    return FnssClient(cfg["host"], cfg["token"])


def diary_path_for(date_str: str) -> str:
    """Build the remote path for a given date (YYYY-MM-DD)."""
    cfg = load_config()
    base = cfg["diary_dir"].strip("/")
    return f"{base}/{diary_filename_for(date_str)}"


def diary_filename_for(date_str: str) -> str:
    """Build YYYY-MM-DD.md from a date string."""
    return f"{date_str}.md"


def load_or_fetch(client: Optional[FnssClient], remote_path: str) -> tuple[Optional[str], bool]:
    """Try local cache first, then fnss. Returns (content, existed_on_remote).

    - (None, False): nothing anywhere
    - (content, False): only local cache hit
    - (content, True): remote hit (local may or may not be updated)
    """
    local = diary_ops.read_local(remote_path)
    if client is None:
        return local, False

    cfg = load_config()
    try:
        remote = client.get_note(cfg["vault"], remote_path)
    except FnssError as e:
        render_warning(f"无法读取远端：{e}")
        return local, False

    if remote is None:
        # 404 — file doesn't exist remotely
        return None, False

    content = remote.get("content", "")
    diary_ops.write_local(remote_path, content)
    return content, True


def add_log(text: str, date_str: str) -> int:
    """Append a log entry to the diary for `date_str`. Offline-first."""
    from .date import parse_date  # local import to keep deps minimal

    try:
        target = parse_date(date_str)
    except ValueError as e:
        render_error(str(e))
        return 1

    iso_date = target.isoformat()
    remote_path = diary_path_for(iso_date)

    client = make_client()
    content, existed_remote = load_or_fetch(client, remote_path)

    if content is None:
        render_error(
            f"日记文件 {remote_path} 不存在"
        )
        console.print(
            "[dim]请先在 Obsidian 中创建当天的日记（Daily Note 模板）[/dim]"
        )
        return 1

    if not diary_ops.has_logs_section(content):
        render_error(f"{remote_path} 中没有 # Logs 标题")
        return 1

    timestamp = datetime.now().strftime("%H:%M")
    entry = f"- ⌚{timestamp} {text}"
    new_content = diary_ops.insert_log_entry(content, timestamp, text)
    diary_ops.write_local(remote_path, new_content)
    render_success(f"已添加：{iso_date} ⌚{timestamp} {text}")

    if client is None:
        render_warning("未配置 fnss 凭证，已仅保存到本地")
        queue_pending(iso_date, [entry])
        return 0

    cfg = load_config()
    try:
        # Re-fetch to merge (in case remote changed between get and now)
        remote = client.get_note(cfg["vault"], remote_path)
        if remote is None:
            render_warning(f"远端 {remote_path} 不存在；仅本地保存")
            queue_pending(iso_date, [entry])
            return 0
        latest = remote.get("content", "")
        # Idempotency: if entry already in latest, skip push
        if entry.strip() in latest:
            render_info("已存在，跳过同步")
            # Still update local cache to latest (normalized)
            diary_ops.write_local(remote_path, diary_ops._normalize(latest))
            return 0
        merged = diary_ops._normalize(diary_ops.insert_log_entry(latest, timestamp, text))
        client.write_note(cfg["vault"], remote_path, merged)
        diary_ops.write_local(remote_path, merged)
        render_success("已同步到 fnss")
        pushed = push_pending(client)
        if pushed:
            render_success(f"额外推送了 {pushed} 条待同步项")
        return 0
    except FnssError as e:
        render_warning(f"同步失败：{e}；已缓存到本地")
        queue_pending(iso_date, [entry])
        return 0


def list_logs(date_str: Optional[str]) -> int:
    from .date import parse_date, today

    try:
        target = parse_date(date_str) if date_str else today()
    except ValueError as e:
        render_error(str(e))
        return 1

    iso_date = target.isoformat()
    remote_path = diary_path_for(iso_date)

    client = make_client()
    content, existed_remote = load_or_fetch(client, remote_path)

    if content is None:
        render_error(f"日记文件 {remote_path} 不存在")
        console.print("[dim]请先在 Obsidian 中创建当天的日记[/dim]")
        return 1

    if not diary_ops.has_logs_section(content):
        render_error(f"{remote_path} 中没有 # Logs 标题")
        return 1

    if client is not None and existed_remote:
        render_info(f"同步完成 ({datetime.now().strftime('%H:%M:%S')})")

    logs = diary_ops.extract_logs_section(content)
    if not logs.strip() or logs.strip() in ("# Logs", "# Logs\n*(未找到 # Logs 标题)*"):
        console.print(f"[dim]{iso_date} 暂无 Logs[/dim]")
        return 0

    from rich.markdown import Markdown

    console.print(Markdown(logs))
    return 0


def edit_log(date_str: Optional[str]) -> int:
    """Open an editor to write a chunk of text; append as one new diary entry.

    The temp file is seeded with "- ⌚HH:MM " (current time) so the user can
    type immediately. Whatever they edit becomes the entry body — multiple
    lines, lists, paragraphs are all part of ONE entry.

    Format guarantee (per user requirement):
        [original content rstripped]
        [blank line]                  <- "\n\n"
        - ⌚HH:MM <user text line 1>
        <user text line 2>
        ...
        <user text line N>\\n          <- trailing \\n (POSIX)
    """
    from .date import parse_date, today

    if date_str:
        try:
            target = parse_date(date_str)
        except ValueError as e:
            render_error(str(e))
            return 1
    else:
        target = today()

    iso_date = target.isoformat()
    remote_path = diary_path_for(iso_date)

    client = make_client()
    content, existed_remote = load_or_fetch(client, remote_path)

    if content is None:
        render_error(f"日记文件 {remote_path} 不存在")
        console.print("[dim]请先在 Obsidian 中创建当天的日记（Daily Note 模板）[/dim]")
        return 1

    if not diary_ops.has_logs_section(content):
        render_error(f"{remote_path} 中没有 # Logs 标题")
        return 1

    timestamp = datetime.now().strftime("%H:%M")
    initial = f"- ⌚{timestamp} "

    # Create a temp file in /tmp so the user has a clean edit surface
    fd, tmp_name = tempfile.mkstemp(prefix="fnss-edit-odiary-", suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        import os as _os
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(initial)
        rc, _ = _editor_edit(tmp_path)
        if rc != 0:
            render_warning(f"编辑器异常退出 (code={rc})，仍尝试推送（如有改动）")
        try:
            user_text = tmp_path.read_text(encoding="utf-8")
        except OSError as e:
            render_error(f"读取临时文件失败：{e}")
            return 1
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    # Check body emptiness — strip timestamp prefix first to handle the
    # "user :q without typing" case (initial file = "- ⌚HH:MM " only).
    expected_prefix = f"- ⌚{timestamp} "
    body_after_prefix = ""
    if user_text.startswith(expected_prefix):
        body_after_prefix = user_text[len(expected_prefix):]
    elif user_text.startswith("- ⌚"):
        # User changed timestamp; strip any HH:MM timestamp they typed
        m = re.match(r"^- ⌚\d{1,2}:\d{2} ", user_text)
        body_after_prefix = user_text[m.end():] if m else user_text
    else:
        # User deleted the timestamp line entirely
        body_after_prefix = user_text

    if not body_after_prefix.strip():
        render_info("内容为空，已取消")
        return 0

    if user_text.startswith(expected_prefix):
        entry = user_text.rstrip()
    elif user_text.startswith("- ⌚"):
        # User changed the timestamp — preserve their version
        entry = user_text.rstrip()
    else:
        # User deleted the timestamp line — re-wrap with current time
        entry = (f"- ⌚{timestamp} " + user_text).rstrip()
    entry = entry + "\n"

    new_content = content.rstrip() + "\n\n" + entry
    diary_ops.write_local(remote_path, new_content)
    first_line = entry.splitlines()[0]
    render_success(f"已添加：{iso_date} {first_line}")

    if client is None:
        render_warning("未配置 fnss 凭证，已仅保存到本地")
        queue_pending(iso_date, [entry])
        return 0

    cfg = load_config()
    try:
        remote = client.get_note(cfg["vault"], remote_path)
        if remote is None:
            render_warning(f"远端 {remote_path} 不存在；仅本地保存")
            queue_pending(iso_date, [entry])
            return 0
        latest = remote.get("content", "")
        if entry in latest:
            render_info("已存在，跳过同步")
            diary_ops.write_local(remote_path, latest)
            return 0
        merged = diary_ops._normalize(latest.rstrip() + "\n\n" + entry + "\n")
        client.write_note(cfg["vault"], remote_path, merged)
        diary_ops.write_local(remote_path, merged)
        render_success("已同步到 fnss")
        pushed = push_pending(client)
        if pushed:
            render_info(f"额外推送了 {pushed} 条待同步项")
        return 0
    except FnssError as e:
        render_warning(f"同步失败：{e}；已缓存到本地")
        queue_pending(iso_date, [entry])
        return 0


def push_pending(client: FnssClient) -> int:
    """Push queued pending entries, grouped by date. Returns count pushed."""
    items = load_pending()
    if not items:
        return 0

    cfg = load_config()
    by_date: dict[str, list[str]] = defaultdict(list)
    for it in items:
        by_date[it["date"]].append(it["entry"])

    total_pushed = 0
    remaining: list[dict] = []
    for date_str, entries in by_date.items():
        remote_path = diary_path_for(date_str)
        try:
            remote = client.get_note(cfg["vault"], remote_path)
            if remote is None:
                render_warning(f"{remote_path} 不存在，保留 {len(entries)} 条")
                remaining.extend(
                    {"date": date_str, "entry": e, "queued_at": it["queued_at"]}
                    for e, it in zip(entries, (i for i in items if i["date"] == date_str))
                )
                continue
            content = remote.get("content", "")
            for entry in entries:
                if entry.rstrip() in content:
                    continue
                # Entry already includes its own "- ⌚HH:MM " prefix (queue_pending
                # stores the full line). Append as-is with blank-line separator
                # instead of re-running insert_log_entry (which would duplicate
                # the timestamp prefix).
                content = content.rstrip() + "\n\n" + entry.rstrip() + "\n"
            content = diary_ops._normalize(content)
            client.write_note(cfg["vault"], remote_path, content)
            diary_ops.write_local(remote_path, content)
            total_pushed += len(entries)
        except FnssError as e:
            render_warning(f"{remote_path} 推送失败：{e}；保留待重试")
            remaining.extend(
                {"date": date_str, "entry": e, "queued_at": it["queued_at"]}
                for e, it in zip(entries, (i for i in items if i["date"] == date_str))
            )

    save_pending(remaining)
    return total_pushed


def manual_sync() -> int:
    """Push pending entries to fnss. Reports clearly when offline."""
    if not is_configured():
        render_error("未配置 fnss 凭证，运行 `onote config` 设置")
        return 1
    pending_items = load_pending()
    if not pending_items:
        render_info("没有待同步项")
        return 0

    client = make_client()
    if client is None:
        render_error("无法创建客户端")
        return 1

    pushed = push_pending(client)
    if pushed:
        render_success(f"已推送 {pushed} 条")
    else:
        render_warning(
            f"推送失败：{len(pending_items)} 条仍待同步，请检查网络或服务后重试"
        )
    return 0