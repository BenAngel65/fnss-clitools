"""odiary command-line interface.

Mirrors oinbox CLI but routes args to date-specific files.

Default behavior:
    odiary <text>          -> add to today's diary
    odiary <date> <text>   -> add to specified date's diary
    odiary list            -> show today's Logs
    odiary list <date>     -> show specified date's Logs
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clitools.config import config_path, data_dir, diary_data_dir, is_configured, load_config, save_config
from clitools.render import console, render_error, render_success, render_warning
from .date import looks_like_date, parse_date, today


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="odiary",
        description="离线优先的 diary CLI（基于 fast-note-sync-service）",
    )
    parser.add_argument("--version", action="store_true", help="显示版本")

    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", aliases=["ls"], help="列出某天的 Logs")
    p_list.add_argument("date", nargs="?", help="日期 YYYY-MM-DD（默认今天）")

    p_add = sub.add_parser("add", help="添加一条日志")
    p_add.add_argument("text", nargs="+", help="日志内容")

    p_edit = sub.add_parser("edit", help="编辑器模式：写入大段文字（自动添加时间戳前缀）")
    p_edit.add_argument("date", nargs="?", help="日期 YYYY-MM-DD（默认今天）")

    p_sync = sub.add_parser("sync", help="手动同步（推送 pending）")

    p_config = sub.add_parser("config", help="配置/查看 fnss 凭证 + diary_dir")
    p_config.add_argument("--host", help="fnss 服务地址")
    p_config.add_argument("--token", help="fnss 认证 Token")
    p_config.add_argument("--vault", help="vault 名称")
    p_config.add_argument(
        "--diary-dir", dest="diary_dir", help="日记目录（默认 Logs/Diary）"
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
    if args.diary_dir:
        cfg["diary_dir"] = args.diary_dir.strip("/")
        changed = True

    if changed:
        save_config(cfg)
        render_success(f"配置已保存到 {config_path()}")

    if args.path:
        console.print(f"配置: {config_path()}")
        console.print(f"数据: {data_dir()}")
        console.print(f"odiary 缓存: {diary_data_dir()}")
        return 0

    if args.show or not changed:
        safe = {k: v for k, v in cfg.items() if k != "token"}
        token = cfg.get("token", "")
        if token:
            safe["token"] = (
                token[:8] + "…" + token[-4:] if len(token) > 16 else "***"
            )
        console.print_json(data=safe)
        if not is_configured(cfg):
            console.print()
            render_warning(
                "尚未配置 host/token，请用 `odiary config --host ... --token ...` 设置"
            )
        return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)

    # Bare-text mode: `odiary <text...>` or `odiary <date> <text...>`
    # If first arg looks like a date and we have >=2 args, treat as date + text.
    # Otherwise treat all args as text to add (today's diary).
    bare_date: str | None = None
    if raw and not raw[0].startswith("-") and raw[0] not in {
        "list",
        "ls",
        "add",
        "edit",
        "sync",
        "config",
        "help",
        "-h",
        "--help",
    }:
        if looks_like_date(raw[0]) and len(raw) >= 2:
            bare_date = raw[0]
            raw = ["add", *raw[1:]]
        else:
            raw = ["add", *raw]

    args = parser.parse_args(raw)

    if args.version:
        from . import __version__

        console.print(f"odiary {__version__}")
        return 0

    if args.cmd in ("list", "ls"):
        from .sync import list_logs

        return list_logs(args.date)

    if args.cmd == "add":
        from .sync import add_log

        text = " ".join(args.text)
        if not text.strip():
            render_error("内容不能为空")
            return 1
        target_date = bare_date or today().isoformat()
        return add_log(text, target_date)

    if args.cmd == "edit":
        from .sync import edit_log

        return edit_log(args.date)

    if args.cmd == "sync":
        from .sync import manual_sync

        return manual_sync()

    if args.cmd == "config":
        return handle_config(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())