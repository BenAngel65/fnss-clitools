"""oinbox command-line interface.

Default behavior: any unknown args become the task text:
    oinbox 买牛奶  -> add a task with content 买牛奶
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from clitools.config import config_path, data_dir, is_configured, load_config, save_config
from clitools.render import console, render_error, render_success, render_warning


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oinbox",
        description="离线优先的 inbox CLI（基于 fast-note-sync-service）",
    )
    parser.add_argument(
        "--version", action="store_true", help="显示版本"
    )

    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser(
        "list", aliases=["ls"], help="拉取并渲染 INBOX.md"
    )

    p_add = sub.add_parser("add", help="添加一条任务")
    p_add.add_argument("text", nargs="+", help="任务内容")

    p_sync = sub.add_parser("sync", help="手动同步（拉取 + 推送待同步项）")

    p_config = sub.add_parser("config", help="配置/查看 fnss 凭证")
    p_config.add_argument("--host", help="fnss 服务地址，如 https://fnss.example.com")
    p_config.add_argument("--token", help="fnss 认证 Token")
    p_config.add_argument("--vault", help="vault 名称")
    p_config.add_argument(
        "--inbox-path", dest="inbox_path", help="远端 INBOX 路径（默认 INBOX.md）"
    )
    p_config.add_argument(
        "--show", action="store_true", help="显示当前配置"
    )
    p_config.add_argument(
        "--path", action="store_true", help="显示配置/数据文件路径"
    )

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
    if args.inbox_path:
        cfg["inbox_path"] = args.inbox_path.lstrip("/")
        changed = True

    if changed:
        save_config(cfg)
        render_success(f"配置已保存到 {config_path()}")

    if args.path:
        console.print(f"配置: {config_path()}")
        console.print(f"数据: {data_dir()}")
        return 0

    if args.show or not changed:
        safe = {k: v for k, v in cfg.items() if k != "token"}
        token = cfg.get("token", "")
        if token:
            safe["token"] = token[:8] + "…" + token[-4:] if len(token) > 16 else "***"
        console.print_json(data=safe)
        if not is_configured(cfg):
            console.print()
            render_warning("尚未配置 host/token，请用 `oinbox config --host ... --token ...` 设置")
        return 0

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    # Allow bare text: parse a default `add` subcommand when only positional
    # args are provided (i.e. `oinbox 买牛奶`).
    raw = list(sys.argv[1:] if argv is None else argv)

    if raw and not raw[0].startswith("-") and raw[0] not in {
        "list",
        "ls",
        "add",
        "sync",
        "config",
        "help",
        "-h",
        "--help",
    }:
        raw = ["add", *raw]

    args = parser.parse_args(raw)

    if args.version:
        console.print(f"oinbox {__version__}")
        return 0

    if args.cmd in ("list", "ls"):
        from .sync import sync_then_render

        return sync_then_render()

    if args.cmd == "add":
        from .sync import add_and_sync

        text = " ".join(args.text)
        return add_and_sync(text)

    if args.cmd == "sync":
        from .sync import manual_sync

        return manual_sync()

    if args.cmd == "config":
        return handle_config(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())