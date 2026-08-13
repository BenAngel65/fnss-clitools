"""Sync + CRUD logic for onote.

All operations are best-effort and offline-first: local writes never block
on network. Failed operations are queued to pending.json and retried on
the next sync.

pending.json schema:
    [
      {"op":"write", "path":"...", "content":"...", "queued_at":"..."},
      {"op":"delete","path":"...",                 "queued_at":"..."},
    ]
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from clitools.config import is_configured, load_config
from clitools.fnss import FnssClient, FnssError
from clitools.render import (
    console,
    render_error,
    render_info,
    render_success,
    render_warning,
)

from . import note as note_ops
from . import editor as editor_mod


# Empty initial content for newly-created notes; user fills it in via nvim.
INITIAL_CONTENT = ""


# ---------- pending queue ----------

def pending_path() -> Path:
    return note_ops.notes_data_dir() / "pending.json"


def load_pending() -> list[dict]:
    p = pending_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_pending(items: list[dict]) -> None:
    p = pending_path()
    if not items:
        if p.exists():
            p.unlink()
        return
    p.write_text(
        json.dumps(items, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def queue_write(path: str, content: str) -> None:
    items = load_pending()
    items.append({
        "op": "write",
        "path": path,
        "content": content,
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    })
    save_pending(items)


def queue_delete(path: str) -> None:
    items = load_pending()
    items.append({
        "op": "delete",
        "path": path,
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    })
    save_pending(items)


def push_pending(client: FnssClient) -> tuple[int, list[str]]:
    """Push queued ops to fnss. FIFO; remaining items stay queued on error.

    Returns (count_pushed, error_messages).
    """
    items = load_pending()
    if not items:
        return (0, [])

    cfg = load_config()
    vault = cfg["vault"]

    pushed = 0
    errors: list[str] = []
    remaining: list[dict] = []

    for it in items:
        op = it.get("op")
        path = it.get("path", "")
        try:
            if op == "write":
                content = it.get("content", "")
                # Idempotency: skip if server already has identical content
                try:
                    remote = client.get_note(vault, path)
                except FnssError as e:
                    errors.append(f"{path}: GET 失败 {e}")
                    remaining.append(it)
                    continue
                if remote is not None and remote.get("content", "") == content:
                    pushed += 1
                    continue
                client.write_note(vault, path, content)
                pushed += 1
            elif op == "delete":
                # Soft delete; recycle-clear is best-effort (may already be cleared)
                client.delete_note(vault, path)
                try:
                    client.recycle_clear(vault, path)
                except FnssError:
                    pass
                pushed += 1
            else:
                errors.append(f"{path}: unknown op {op!r}")
                continue
        except FnssError as e:
            errors.append(f"{path}: {e}")
            remaining.append(it)

    save_pending(remaining)
    return (pushed, errors)


# ---------- client helper ----------

def make_client() -> Optional[FnssClient]:
    cfg = load_config()
    if not is_configured(cfg):
        return None
    return FnssClient(cfg["host"], cfg["token"])


def reconcile_local_cache(client: FnssClient, vault: str) -> tuple[int, list[str]]:
    """Walk local notes cache; push any file that diverges from (or is missing on) the server.

    Use case: recover orphaned local notes that were created/edited offline and
    whose pending.json entries were lost (manual cleanup, crash, etc.).

    Returns (count_pushed, error_messages).
    """
    paths = note_ops.scan_notes_dir()
    if not paths:
        return (0, [])

    pushed = 0
    errors: list[str] = []

    for path in paths:
        try:
            local_content = note_ops.read_local(path) or ""
            remote = client.get_note(vault, path)
            if remote is None:
                # Server missing → push local
                client.write_note(vault, path, local_content)
                pushed += 1
            elif remote.get("content", "") != local_content:
                # Drift → push local (last-write-wins)
                client.write_note(vault, path, local_content)
                pushed += 1
            # else: identical → skip
        except FnssError as e:
            errors.append(f"{path}: {e}")

    return (pushed, errors)


# ---------- CRUD operations ----------

def create_new(title: str) -> int:
    """Strict create + nvim edit + push. Errors if file exists locally or remotely."""
    title = (title or "").strip()
    if not title:
        render_error("标题不能为空")
        return 1

    try:
        path = note_ops.normalize_path(title)
    except ValueError as e:
        render_error(str(e))
        return 1

    local = note_ops.local_note_path(path)

    if local.exists():
        render_error(f"本地已存在：{path}")
        render_warning("如需编辑请用 `onote edit <path>`")
        return 1

    client = make_client()
    cfg = load_config()
    if client is not None:
        # Recover any orphaned local notes first so they don't linger.
        _reconcile_silently(client, cfg["vault"])
        try:
            remote = client.get_note(cfg["vault"], path)
            if remote is not None:
                render_error(f"远端已存在：{path}")
                render_warning("如需编辑请用 `onote edit <path>`")
                return 1
        except FnssError as e:
            render_warning(f"无法检查远端：{e}；继续本地创建")

    note_ops.write_local(path, INITIAL_CONTENT)
    render_success(f"已创建本地：{path}")

    rc, editor = editor_mod.edit_with_fallback(local)
    if not editor:
        # Editor unavailable; treat as local-only draft
        render_warning("未启动编辑器，本地草稿已保留")
        queue_write(path, INITIAL_CONTENT)
        return 0
    if rc != 0:
        render_warning(f"编辑器异常退出 (code={rc})，仍尝试推送")

    try:
        new_content = local.read_text(encoding="utf-8")
    except OSError as e:
        render_error(f"读取本地文件失败：{e}")
        return 1

    if not new_content.strip():
        # Empty content = user changed their mind; clean up locally, do not push.
        note_ops.delete_local(path)
        render_info("内容为空，已取消创建")
        return 0

    if client is None:
        render_warning("未配置 fnss 凭证，已仅保存到本地")
        queue_write(path, new_content)
        return 0

    try:
        client.write_note(cfg["vault"], path, new_content)
        render_success(f"已同步到 fnss: {path}")
        _drain_pending_silently(client)
        return 0
    except FnssError as e:
        render_warning(f"同步失败：{e}；已缓存到本地")
        queue_write(path, new_content)
        return 0


def edit_existing(ref: str) -> int:
    """Resolve ref → fetch (or fall back to local cache) → nvim → push."""
    try:
        path = note_ops.resolve_ref(ref)
    except ValueError as e:
        render_error(str(e))
        return 1

    local = note_ops.local_note_path(path)
    client = make_client()
    cfg = load_config()

    original_content: Optional[str] = None
    if client is not None:
        # Recover any orphaned local notes first.
        _reconcile_silently(client, cfg["vault"])
        try:
            remote = client.get_note(cfg["vault"], path)
            if remote is not None:
                original_content = remote.get("content", "")
                note_ops.write_local(path, original_content)
            else:
                # Not found remotely → seed locally and treat as new
                if not local.exists():
                    original_content = INITIAL_CONTENT
                    note_ops.write_local(path, original_content)
                    render_info(f"远端无此笔记，本地新建并打开：{path}")
                else:
                    original_content = local.read_text(encoding="utf-8")
        except FnssError as e:
            render_warning(f"无法读取远端：{e}")
            if local.exists():
                original_content = local.read_text(encoding="utf-8")
            else:
                render_error("离线且本地无缓存，无法编辑")
                return 1
    else:
        if local.exists():
            original_content = local.read_text(encoding="utf-8")
        else:
            render_error("未配置 fnss 凭证且本地无缓存，无法编辑")
            return 1

    rc, editor = editor_mod.edit_with_fallback(local)
    if not editor:
        return 1
    if rc != 0:
        render_warning(f"编辑器异常退出 (code={rc})，仍尝试推送（如有改动）")

    try:
        new_content = local.read_text(encoding="utf-8")
    except OSError as e:
        render_error(f"读取本地文件失败：{e}")
        return 1

    if not new_content.strip():
        # Empty content — remove local cache and do not push.
        note_ops.delete_local(path)
        render_info("内容为空，已清空本地缓存")
        return 0

    if new_content == original_content:
        render_info("内容未变更，跳过推送")
        return 0

    if client is None:
        render_warning("离线状态，已缓存到本地")
        queue_write(path, new_content)
        return 0

    try:
        client.write_note(cfg["vault"], path, new_content)
        note_ops.write_local(path, new_content)
        render_success(f"已同步：{path}")
        _drain_pending_silently(client)
        return 0
    except FnssError as e:
        render_warning(f"同步失败：{e}；已缓存到本地")
        queue_write(path, new_content)
        return 0


def open_note(ref: str) -> int:
    """Fetch + render to terminal via rich Markdown."""
    try:
        path = note_ops.resolve_ref(ref)
    except ValueError as e:
        render_error(str(e))
        return 1

    from rich.markdown import Markdown

    client = make_client()
    cfg = load_config()

    content: Optional[str] = None
    if client is not None:
        try:
            remote = client.get_note(cfg["vault"], path)
            if remote is not None:
                content = remote.get("content", "")
                note_ops.write_local(path, content)
        except FnssError as e:
            render_warning(f"无法读取远端：{e}")

    if content is None:
        local_content = note_ops.read_local(path)
        if local_content is None:
            render_error(f"笔记不存在：{path}")
            return 1
        content = local_content
        render_warning("显示本地缓存")

    if not content.strip():
        console.print(f"[dim]{path} 为空[/dim]")
        return 0

    console.print(Markdown(content))
    return 0


def delete_note(ref: str, confirm: bool = True) -> int:
    """Delete note (soft + hard) with optional confirmation."""
    try:
        path = note_ops.resolve_ref(ref)
    except ValueError as e:
        render_error(str(e))
        return 1

    if confirm:
        try:
            resp = input(f"确定删除 {path}？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            render_info("已取消")
            return 0
        if resp not in ("y", "yes"):
            render_info("已取消")
            return 0

    client = make_client()
    if client is None:
        render_warning("离线状态，已缓存删除操作")
        queue_delete(path)
        return 0

    cfg = load_config()
    try:
        client.delete_note(cfg["vault"], path)
        try:
            client.recycle_clear(cfg["vault"], path)
        except FnssError:
            pass
        note_ops.delete_local(path)
        render_success(f"已删除：{path}")
        return 0
    except FnssError as e:
        render_warning(f"删除失败：{e}；已缓存删除操作")
        queue_delete(path)
        return 0


def manual_sync() -> int:
    """Push pending + reconcile orphaned local notes."""
    client = make_client()
    if client is None:
        render_error("未配置 fnss 凭证，运行 `onote config` 设置")
        return 1
    cfg = load_config()
    vault = cfg["vault"]

    pushed, errs = push_pending(client)
    for e in errs:
        render_warning(f"推送失败：{e}")

    rec_pushed, rec_errs = reconcile_local_cache(client, vault)
    for e in rec_errs:
        render_warning(f"本地恢复失败：{e}")

    total = pushed + rec_pushed
    if total:
        render_success(f"已推送 {pushed} 条待同步项 + 恢复 {rec_pushed} 个本地孤儿")
    else:
        render_info("没有待同步项")
    return 0


# ---------- internal ----------

def _drain_pending_silently(client: FnssClient) -> None:
    """Push pending after a successful op; surface only errors and counts."""
    try:
        pushed, errs = push_pending(client)
        for e in errs:
            render_warning(f"推送失败：{e}")
        if pushed:
            render_info(f"额外推送了 {pushed} 条待同步项")
    except FnssError:
        pass


def _reconcile_silently(client: FnssClient, vault: str) -> None:
    """Recover orphaned local notes; surface only errors and counts.

    Called at the start of create/edit/sync to catch local files that exist
    but never made it into pending.json (or whose pending was lost).
    """
    try:
        pushed, errs = reconcile_local_cache(client, vault)
        for e in errs:
            render_warning(f"本地恢复失败：{e}")
        if pushed:
            render_info(f"自动恢复了 {pushed} 个本地孤儿笔记")
    except FnssError:
        pass