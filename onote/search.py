"""Search via fnss GET /api/notes (server-side FTS5).

Online-only: requires a configured fnss client. Offline → friendly error.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from clitools.config import is_configured, load_config
from clitools.fnss import FnssClient, FnssError
from clitools.render import console, render_error, render_info, render_warning

from . import note as note_ops
from .sync import make_client, push_pending


def _format_result_line(idx: int, item: dict) -> str:
    path = item.get("path", "")
    size = item.get("size", 0)
    mtime_ms = item.get("mtime", 0)
    if mtime_ms:
        mtime_str = datetime.fromtimestamp(mtime_ms / 1000).strftime("%Y-%m-%d %H:%M")
    else:
        mtime_str = "----"
    return f"[{idx}] {path}    {mtime_str}    {size}B"


def search(query: str, mode: str = "path") -> int:
    """Search notes via fnss.

    mode: 'path' (matches path/title; default) or 'content' (full content).
    Multi-word query: AND semantics (intersection of per-token matches).
    """
    query = (query or "").strip()
    if not query:
        render_error("用法: onote search <query>   或   onote search -c <query>")
        return 1

    client = make_client()
    if client is None:
        render_error("未配置 fnss 凭证")
        render_warning("运行 `onote config --host ... --token ...` 设置")
        return 1

    cfg = load_config()
    vault = cfg["vault"]

    # Opportunistically drain pending + reconcile orphans before searching
    try:
        pushed, errs = push_pending(client)
        if pushed:
            render_info(f"同步时已推送 {pushed} 条待同步项")
        for e in errs:
            render_warning(f"推送失败：{e}")
    except FnssError:
        pass
    from .sync import reconcile_local_cache
    try:
        rec_pushed, rec_errs = reconcile_local_cache(client, vault)
        for e in rec_errs:
            render_warning(f"本地恢复失败：{e}")
        if rec_pushed:
            render_info(f"自动恢复了 {rec_pushed} 个本地孤儿笔记")
    except FnssError:
        pass

    tokens = query.split()

    def _fetch(token: str) -> tuple[list[dict], set[str]]:
        data = client.list_notes(
            vault, keyword=token, search_mode=mode, page=1, page_size=100
        )
        items = data.get("list", [])
        paths = {it.get("path", "") for it in items}
        return items, paths

    try:
        if len(tokens) == 1:
            items, _ = _fetch(tokens[0])
        else:
            # Multi-keyword AND: intersect path sets across tokens
            first_items, first_paths = _fetch(tokens[0])
            common = first_paths
            for tok in tokens[1:]:
                _, paths = _fetch(tok)
                common = common & paths
            items = [it for it in first_items if it.get("path", "") in common]
    except FnssError as e:
        render_error(f"搜索失败：{e}")
        return 1

    if not items:
        render_warning(f"未找到匹配 '{query}' 的笔记")
        return 0

    paths: list[str] = []
    for i, item in enumerate(items, 1):
        path = item.get("path", "")
        paths.append(path)
        console.print(_format_result_line(i, item))

    note_ops.save_last_search(query, mode, paths)
    console.print()
    render_info(
        f"共 {len(paths)} 条结果;后续可用 `onote edit/open/delete <编号>`"
    )
    return 0

    paths: list[str] = []
    for i, item in enumerate(items, 1):
        path = item.get("path", "")
        paths.append(path)
        console.print(_format_result_line(i, item))

    note_ops.save_last_search(query, mode, paths)
    console.print()
    render_info(
        f"共 {len(paths)} 条结果；后续可用 `onote edit/open/delete <编号>`"
    )
    return 0