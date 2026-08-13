"""onote command-line interface.

Default behavior: any unknown args become the new-note title:
    onote 买牛奶  -> strict create at Inbox/买牛奶.md + nvim edit
"""
from __future__ import annotations

import argparse
import sys

from clitools.config import config_path, data_dir, is_configured, load_config, save_config
from clitools.render import console, render_error, render_success, render_warning

from . import __version__


RESERVED_SUBCMDS = {
    "new", "edit", "open", "delete", "search", "sync", "config",
    "help", "-h", "--help",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onote",
        description="onote — 笔记 CRUD（vim/nvim 编辑 + fnss 同步）",
    )
    parser.add_argument("--version", action="store_true", help="显示版本")

    sub = parser.add_subparsers(dest="cmd")

    p_new = sub.add_parser("new", help="新建笔记（已存在则报错）")
    p_new.add_argument("title", nargs="+", help="笔记标题")

    p_edit = sub.add_parser("edit", help="编辑笔记（编号或路径）")
    p_edit.add_argument("ref", help="编号（来自 search）或路径")

    p_open = sub.add_parser("open", help="终端查看笔记")
    p_open.add_argument("ref", help="编号或路径")

    p_delete = sub.add_parser("delete", help="删除笔记（默认二次确认）")
    p_delete.add_argument("ref", help="编号或路径")
    p_delete.add_argument("--yes", "-y", action="store_true", help="跳过确认")

    p_search = sub.add_parser("search", help="搜索笔记（默认按路径/title）")
    p_search.add_argument(
        "-c", "--content", action="store_true", help="按内容搜索"
    )
    p_search.add_argument("query", nargs="+", help="搜索关键词（多个用空格分隔，整体作为一个 query）")

    sub.add_parser("sync", help="推送 pending 队列")

    p_config = sub.add_parser("config", help="配置/查看 fnss 凭证")
    p_config.add_argument("--host", help="fnss 服务地址")
    p_config.add_argument("--token", help="fnss 认证 Token")
    p_config.add_argument("--vault", help="vault 名称")
    p_config.add_argument(
        "--notes-dir", dest="notes_dir", help="默认笔记目录（默认 Inbox）"
    )
    p_config.add_argument("--show", action="store_true", help="显示当前配置")
    p_config.add_argument("--path", action="store_true", help="显示路径")

    return parser


def handle_config(args) -> int:
    cfg = load_config()
    changed = False
    if args.host:
        cfg["host"] = args.host.rstrip("/")
        changed = True
    if args.token:
        cfg["token"] = args.token
        changed = True
    if args.vault:
        cfg["vault"] = args.vault
        changed = True
    if args.notes_dir:
        cfg["notes_dir"] = args.notes_dir.strip("/")
        changed = True

    if changed:
        save_config(cfg)
        render_success(f"配置已保存到 {config_path()}")

    if args.path:
        from .note import notes_data_dir
        console.print(f"配置: {config_path()}")
        console.print(f"数据: {data_dir()}")
        console.print(f"onote 缓存: {notes_data_dir()}")
        return 0

    if args.show or not changed:
        safe = {k: v for k, v in cfg.items() if k != "token"}
        token = cfg.get("token", "")
        if token:
            safe["token"] = token[:8] + "…" + token[-4:] if len(token) > 16 else "***"
        console.print_json(data=safe)
        if not is_configured(cfg):
            console.print()
            render_warning(
                "尚未配置 host/token，请用 `onote config --host ... --token ...` 设置"
            )
        return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)

    # Bare-text mode: `onote <title...>` → new (strict)
    if raw and not raw[0].startswith("-") and raw[0] not in RESERVED_SUBCMDS:
        raw = ["new", *raw]

    args = parser.parse_args(raw)

    if args.version:
        console.print(f"onote {__version__}")
        return 0

    if args.cmd == "new":
        from .sync import create_new
        return create_new(" ".join(args.title))

    if args.cmd == "edit":
        from .sync import edit_existing
        return edit_existing(args.ref)

    if args.cmd == "open":
        from .sync import open_note
        return open_note(args.ref)

    if args.cmd == "delete":
        from .sync import delete_note
        return delete_note(args.ref, confirm=not args.yes)

    if args.cmd == "search":
        from .search import search as do_search
        mode = "content" if args.content else "path"
        query = " ".join(args.query)
        return do_search(query, mode=mode)

    if args.cmd == "sync":
        from .sync import manual_sync
        return manual_sync()

    if args.cmd == "config":
        return handle_config(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())