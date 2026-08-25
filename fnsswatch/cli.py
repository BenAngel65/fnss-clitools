"""fnsswatch 命令行接口。

Usage:
    fnsswatch on [--poll N]          启动监控守护进程
    fnsswatch off                    停止监控守护进程
    fnsswatch status                 查看守护进程状态
    fnsswatch push [--force]         全量推送：本地 → 远端
    fnsswatch pull [--force]         全量拉取：远端 → 本地
    fnsswatch sync                   手动增量同步
    fnsswatch config [key] [val]     查看/设置配置
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from clitools.config import (
    config_path,
    data_dir,
    is_configured,
    load_config,
    save_config,
)
from clitools.render import console, render_error, render_info, render_success, render_warning

from . import __version__, state
from .merge import MergeResult


# ---------------------------------------------------------------------------
# 交互式冲突解决
# ---------------------------------------------------------------------------

def _interactive_conflict_resolver(remote_path: str, merge_result: MergeResult) -> str:
    """冲突时让用户选择解决方式。

    显示变更详情（增/删/冲突行），然后提供三个选项：
    1. 以本地为准（丢弃远端修改）
    2. 以远端为准（丢弃本地修改）
    3. 合并（union，两边行都保留）← 默认推荐
    """
    console.print()
    console.print(f"[bold yellow]══════ 冲突: {remote_path} ══════[/bold yellow]")
    console.print()

    # 显示变更详情
    if merge_result.local_added:
        console.print("[green]本地新增行:[/green]")
        for line in merge_result.local_added:
            console.print(f"  [green]+ {line.rstrip()}[/green]")
        console.print()

    if merge_result.remote_added:
        console.print("[blue]远端新增行:[/blue]")
        for line in merge_result.remote_added:
            console.print(f"  [blue]+ {line.rstrip()}[/blue]")
        console.print()

    if merge_result.local_deleted:
        console.print("[red]本地删除行:[/red]")
        for line in merge_result.local_deleted:
            console.print(f"  [red]- {line.rstrip()}[/red]")
        console.print()

    if merge_result.remote_deleted:
        console.print("[red]远端删除行:[/red]")
        for line in merge_result.remote_deleted:
            console.print(f"  [red]- {line.rstrip()}[/red]")
        console.print()

    if merge_result.conflicts:
        console.print("[bold red]冲突行（双方改了同一行）:[/bold red]")
        for c in merge_result.conflicts:
            console.print(f"  原始: [dim]{c['base']}[/dim]")
            console.print(f"  本地: [green]{c['local'] or '(删除)'}[/green]")
            console.print(f"  远端: [blue]{c['remote'] or '(删除)'}[/blue]")
        console.print()

    console.print("[bold]选择解决方式:[/bold]")
    console.print("  [cyan]1[/cyan] 合并（union）— 两边行都保留，不丢数据 [dim](推荐)[/dim]")
    console.print("  [cyan]2[/cyan] 以本地为准 — 丢弃远端修改")
    console.print("  [cyan]3[/cyan] 以远端为准 — 丢弃本地修改")
    console.print()

    while True:
        try:
            choice = input("请输入选择 [1-3] (默认 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "1"
        if choice in ("", "1"):
            return "union"
        elif choice == "2":
            return "local"
        elif choice == "3":
            return "remote"
        console.print("[red]无效选择，请输入 1、2 或 3[/red]")


def _setup_interactive_conflict():
    """设置交互式冲突回调（CLI 手动操作时使用）。"""
    from .sync import set_conflict_callback
    set_conflict_callback(_interactive_conflict_resolver)


def _teardown_interactive_conflict():
    """清除交互式冲突回调。"""
    from .sync import clear_conflict_callback
    clear_conflict_callback()


WATCH_CONFIG_KEYS = {
    "watch_local_dirs": "本机监控目录（按 hostname 区分，新设备需手动设置）",
    "watch_dir": "远端路径过滤前缀（留空同步全部 .md）",
    "watch_poll_interval": "远端轮询间隔（秒，默认 30）",
    "watch_debounce": "本地变更去抖延时（秒，默认 2）",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fnsswatch",
        description="fnsswatch — 文件夹监控双向同步守护进程",
    )
    parser.add_argument("--version", action="store_true", help="显示版本")

    sub = parser.add_subparsers(dest="cmd")

    # on
    p_on = sub.add_parser("on", help="启动监控守护进程")
    p_on.add_argument("--poll", type=int, help="远端轮询间隔（秒）")
    p_on.add_argument("--foreground", action="store_true",
                       help="前台运行（调试用，不 fork 为守护进程）")

    # off
    sub.add_parser("off", help="停止监控守护进程")

    # status
    sub.add_parser("status", help="查看守护进程状态")

    # push
    p_push = sub.add_parser("push", help="全量推送（本地 → 远端）")
    p_push.add_argument("--force", action="store_true",
                         help="强制推送所有文件（跳过哈希比对）")

    # pull
    p_pull = sub.add_parser("pull", help="全量拉取（远端 → 本地）")
    p_pull.add_argument("--force", action="store_true",
                         help="强制拉取所有文件（跳过版本比对）")

    # sync
    sub.add_parser("sync", help="手动增量同步（推送 pending + 本地变更）")

    # config
    p_config = sub.add_parser("config", help="查看/设置 fnsswatch 配置")
    p_config.add_argument("key", nargs="?", help="配置项")
    p_config.add_argument("value", nargs="?", help="配置值")
    p_config.add_argument("--show", action="store_true", help="显示当前配置")
    p_config.add_argument("--path", action="store_true", help="显示路径")

    return parser


def _handle_on(args) -> int:
    from .daemon import start_daemon, is_daemon_running
    from .watcher import run_watcher, _shutdown

    if is_daemon_running():
        render_warning("fnsswatch 守护进程已在运行")
        return 1

    cfg = load_config()
    if not is_configured(cfg):
        render_error("未配置 fnss 凭证，运行 `onote config --host ... --token ...` 设置")
        return 1

    poll = args.poll or cfg.get("watch_poll_interval", 30)

    if args.foreground:
        # 前台模式（调试用）
        _shutdown.clear()
        return run_watcher(poll_interval=poll)
    else:
        return start_daemon(poll_interval=poll)


def _handle_off(args) -> int:
    from .daemon import stop_daemon
    return stop_daemon()


def _handle_status(args) -> int:
    from .daemon import status_daemon
    rc = status_daemon()

    # 额外显示同步状态摘要（从磁盘重载，获取最新状态）
    state.reload_from_disk()
    st = state.load_state()
    if st:
        render_info(f"已追踪 {len(st)} 个文件")
    pending = state.load_pending()
    if pending:
        render_warning(f"pending 队列: {len(pending)} 条")
    else:
        render_info("pending 队列: 无")

    # 显示本地文件数（排除 ._ 开头的 macOS AppleDouble 文件）
    from .sync import get_local_watch_root
    root = get_local_watch_root()
    if root.exists():
        md_count = sum(
            1 for _ in root.rglob("*.md")
            if _.is_file() and not any(p.startswith("._") for p in _.parts)
        )
        render_info(f"本地文件: {md_count}")
    return rc


def _handle_push(args) -> int:
    cfg = load_config()
    if not is_configured(cfg):
        render_error("未配置 fnss 凭证")
        return 1

    _setup_interactive_conflict()
    try:
        from .sync import make_client, full_push, reset_cache
        reset_cache()  # 确保不用旧缓存
        client = make_client()
        if client is None:
            render_error("无法创建客户端")
            return 1

        render_info(f"开始全量推送 vault={cfg['vault']!r}"
                    f"{'（强制模式）' if args.force else ''}")
        pushed, errors = full_push(client, cfg["vault"], force=args.force)
        if pushed:
            render_success(f"已推送 {pushed} 个文件")
        else:
            render_info("无需推送（所有文件已是最新）")
        if errors:
            render_warning(f"失败 {errors} 个")
        return 0 if errors == 0 else 3
    finally:
        _teardown_interactive_conflict()


def _handle_pull(args) -> int:
    cfg = load_config()
    if not is_configured(cfg):
        render_error("未配置 fnss 凭证")
        return 1

    _setup_interactive_conflict()
    try:
        from .sync import make_client, full_pull, reset_cache
        reset_cache()  # 确保不用旧缓存
        client = make_client()
        if client is None:
            render_error("无法创建客户端")
            return 1

        render_info(f"开始全量拉取 vault={cfg['vault']!r}"
                    f"{'（强制模式）' if args.force else ''}")
        pulled, errors = full_pull(client, cfg["vault"], force=args.force)
        if pulled:
            render_success(f"已拉取 {pulled} 个文件")
        else:
            render_info("无需拉取（所有文件已是最新）")
        if errors:
            render_warning(f"失败 {errors} 个")
        return 0 if errors == 0 else 3
    finally:
        _teardown_interactive_conflict()


def _handle_sync(args) -> int:
    _setup_interactive_conflict()
    try:
        from .sync import manual_sync, reset_cache
        reset_cache()  # 确保不用旧缓存
        return manual_sync()
    finally:
        _teardown_interactive_conflict()


def _handle_config(args) -> int:
    cfg = load_config()

    if args.path:
        from .sync import get_local_watch_root
        console.print(f"配置: {config_path()}")
        console.print(f"数据: {data_dir()}")
        console.print(f"watch 文件: {get_local_watch_root()}")
        console.print(f"watch 状态: {state.state_path()}")
        console.print(f"watch PID: {state.pid_path()}")
        console.print(f"watch 日志: {state.log_path()}")
        return 0

    if args.key and args.value is not None:
        if args.key in WATCH_CONFIG_KEYS:
            # 类型转换
            if args.key == "watch_poll_interval":
                try:
                    cfg[args.key] = int(args.value)
                except ValueError:
                    render_error(f"{args.key} 需要整数")
                    return 1
            elif args.key == "watch_debounce":
                try:
                    cfg[args.key] = float(args.value)
                except ValueError:
                    render_error(f"{args.key} 需要数字")
                    return 1
            elif args.key == "watch_local_dirs":
                # 按 hostname 写入当前设备路径
                import socket
                from pathlib import Path
                host = socket.gethostname()
                path = str(Path(args.value).expanduser())
                dirs = cfg.get("watch_local_dirs", {})
                dirs[host] = path
                cfg["watch_local_dirs"] = dirs
                save_config(cfg)
                render_success(f"已设置 {host} → {path}")
            else:
                cfg[args.key] = args.value
                save_config(cfg)
                render_success(f"已设置 {args.key} = {cfg[args.key]}")
        else:
            render_error(f"未知配置项: {args.key}")
            render_info(f"可用配置项: {', '.join(WATCH_CONFIG_KEYS)}")
            return 1
        return 0

    # 显示配置
    safe = {k: v for k, v in cfg.items() if k != "token"}
    token = cfg.get("token", "")
    if token:
        safe["token"] = token[:8] + "…" + token[-4:] if len(token) > 16 else "***"
    console.print_json(data=safe)
    console.print()
    render_info("fnsswatch 专用配置项:")
    for key, desc in WATCH_CONFIG_KEYS.items():
        console.print(f"  [cyan]{key}[/cyan]: {desc}")
        if key == "watch_local_dirs":
            dirs = cfg.get(key, {})
            if dirs:
                cur_host = __import__("socket").gethostname()
                for host, path in sorted(dirs.items()):
                    marker = " ← 当前设备" if host == cur_host else ""
                    console.print(f"    {host}: {path}{marker}")
            else:
                console.print("    (空，首次运行 fnsswatch 时自动写入)")
        else:
            val = cfg.get(key, "(未设置)")
            console.print(f"    当前值: {val}")
    if not is_configured(cfg):
        console.print()
        render_warning("尚未配置 host/token")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)

    if args.version:
        console.print(f"fnsswatch {__version__}")
        return 0

    handlers = {
        "on": _handle_on,
        "off": _handle_off,
        "status": _handle_status,
        "push": _handle_push,
        "pull": _handle_pull,
        "sync": _handle_sync,
        "config": _handle_config,
    }
    handler = handlers.get(args.cmd)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return handler(args)
    except RuntimeError as e:
        render_error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
