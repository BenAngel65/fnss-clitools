"""Sync logic: pull from fnss, push pending entries, write-back merges.

All operations are best-effort. Offline-first: local writes never block on
network. Failed pushes are queued to pending.json and retried on the next
sync.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Optional, Tuple

from clitools.config import (
    DEFAULT_INBOX_HEADER,
    is_configured,
    load_config,
    pending_path,
)
from clitools.fnss import FnssClient, FnssError
from clitools.render import (
    console,
    render_error,
    render_info,
    render_markdown,
    render_success,
    render_warning,
)
from .inbox import append_entry, read_local, write_local


def _ensure_trailing_newline(content: str) -> str:
    return content if content.endswith("\n") else content + "\n"


def _merge_entries(base: str, entries: list[str]) -> tuple[str, list[str]]:
    """Append entries to base; skip exact duplicates.

    Entries are matched as full stripped lines, so re-adding the exact same
    task text won't duplicate it. Different tasks always append.
    """
    out = _ensure_trailing_newline(base) if base else DEFAULT_INBOX_HEADER
    appended: list[str] = []
    for entry in entries:
        line = entry.strip()
        # Avoid exact duplicates; intentional re-adds of identical text
        # are still allowed (they'll get different trailing context on next sync).
        if line and line in out:
            continue
        out += entry if entry.endswith("\n") else entry + "\n"
        appended.append(entry)
    return out, appended


def _load_pending() -> list[dict]:
    p = pending_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_pending(items: list[dict]) -> None:
    p = pending_path()
    if not items:
        if p.exists():
            p.unlink()
        return
    p.write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def queue_pending(entries: list[str]) -> None:
    """Append entries to the pending queue."""
    if not entries:
        return
    items = _load_pending()
    now = datetime.now().isoformat(timespec="seconds")
    for entry in entries:
        items.append({"entry": entry, "queued_at": now})
    _save_pending(items)


def make_client() -> Optional[FnssClient]:
    """Build FnssClient from config; None if unconfigured."""
    cfg = load_config()
    if not is_configured(cfg):
        return None
    return FnssClient(cfg["host"], cfg["token"])


def sync_then_render() -> int:
    """Pull latest from fnss, render local inbox.md.

    Returns process exit code.
    """
    client = make_client()
    pulled = False
    if client is None:
        render_warning("未配置 fnss 凭证，显示本地缓存")
    else:
        cfg = load_config()
        try:
            remote = client.get_note(cfg["vault"], cfg["inbox_path"])
            if remote is None:
                render_warning(
                    f"远端 {cfg['inbox_path']} 不存在，显示本地缓存"
                )
            else:
                write_local(remote.get("content", ""))
                pulled = True
                render_info(
                    f"同步完成 ({datetime.now().strftime('%H:%M:%S')})"
                )
            # opportunistically push pending
            pushed = push_pending(client)
            if pushed:
                render_success(f"推送了 {pushed} 条待同步项")
        except FnssError as e:
            render_warning(f"无法同步：{e}；显示本地缓存")

    content = read_local()
    if not content.strip() or content.strip() == "# Inbox":
        if not pulled:
            console.print(
                "[dim]inbox.md 为空。使用 `oinbox <内容>` 添加第一条记录。[/dim]"
            )
        return 0

    render_markdown(content)
    return 0


def add_and_sync(text: str) -> int:
    """Append a task to local inbox.md, then attempt to push to fnss.

    Returns process exit code.
    """
    text = (text or "").strip()
    if not text:
        render_error("内容不能为空")
        return 1

    _, entry = append_entry(text)
    render_success(f"本地已添加：{entry}")

    client = make_client()
    if client is None:
        render_warning("未配置 fnss 凭证，已仅保存到本地")
        queue_pending([entry])
        return 0

    cfg = load_config()
    try:
        remote = client.get_note(cfg["vault"], cfg["inbox_path"])
        base = "" if remote is None else remote.get("content", "")
        new_content, appended = _merge_entries(base, [entry])
        if not appended:
            render_info("已存在，跳过同步")
            return 0
        client.write_note(cfg["vault"], cfg["inbox_path"], new_content)
        render_success("已同步到 fnss")
        # Drain any pending entries too
        pushed = push_pending(client)
        if pushed:
            render_success(f"额外推送了 {pushed} 条待同步项")
        return 0
    except FnssError as e:
        render_warning(f"同步失败：{e}；已缓存到本地")
        queue_pending([entry])
        return 0


def push_pending(client: FnssClient) -> Tuple[int, List[str]]:
    """Push queued pending entries to fnss.

    Returns (count_pushed, error_list).
    - count_pushed: number of NEW entries actually written to server
    - error_list: empty on success; non-empty if push failed

    Note: idempotent entries (already on server) count as 0 new pushes.
    Failed pushes DO NOT clear pending (so the next sync retries).

    Callers (manual_sync, fnsssync) handle displaying errors.
    """
    items = _load_pending()
    if not items:
        return 0, []
    cfg = load_config()
    entries = [it["entry"] for it in items]
    try:
        remote = client.get_note(cfg["vault"], cfg["inbox_path"])
        base = "" if remote is None else remote.get("content", "")
        new_content, appended = _merge_entries(base, entries)
        if not appended:
            # All entries already on server (idempotent). Clear pending.
            _save_pending([])
            return 0, []
        client.write_note(cfg["vault"], cfg["inbox_path"], new_content)
        _save_pending([])
        return len(appended), []
    except FnssError as e:
        # Push failed; leave pending for next retry.
        return 0, [str(e)]


def manual_sync() -> int:
    """Pull latest content from fnss + push pending entries."""
    if not is_configured():
        render_error("未配置 fnss 凭证，运行 `oinbox config` 设置")
        return 1
    pending_items = _load_pending()
    client = make_client()
    if client is None:
        render_error("无法创建客户端")
        return 1
    cfg = load_config()
    pull_failed = False
    try:
        remote = client.get_note(cfg["vault"], cfg["inbox_path"])
        if remote is None:
            render_warning(
                f"远端 {cfg['inbox_path']} 不存在，本次只推送待同步项"
            )
        else:
            write_local(remote.get("content", ""))
            render_success("已拉取最新内容")
    except FnssError as e:
        pull_failed = True
        render_warning(f"拉取失败：{e}")

    if not pending_items:
        render_info("没有待同步项")
        return 0

    # We have pending items; attempt to push.
    if pull_failed:
        # Skip push if pull failed (likely offline) to avoid wasting time
        render_warning(
            f"跳过推送：{len(pending_items)} 条仍待同步，请检查网络后重试"
        )
        return 0

    pushed, errs = push_pending(client)
    for e in errs:
        render_warning(f"推送失败：{e}")
    if pushed:
        render_success(f"已推送 {pushed} 条")
    elif errs:
        render_warning(
            f"推送失败：{len(pending_items)} 条仍待同步，请检查网络或服务后重试"
        )
    else:
        # pushed=0 and no errs → all entries were idempotent (already on server)
        render_info(f"无新推送（{len(pending_items)} 条已在 server，idempotent 跳过）")
    return 0