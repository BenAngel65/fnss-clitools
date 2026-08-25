"""watchdog 事件处理 — 文件变化监控 + 去抖 + 防循环。

守护进程核心模块：注册 watchdog Observer 监控本地目录，
文件变化时去抖后推送，并通过 ignore_set 防止自己写入触发循环。

核桃派 Zero 优化：
- client / config 缓存，不每次文件变更都重建
- 主循环用 Event.wait() 代替 time.sleep(1)，按需唤醒
- 定期 flush_all() 批量落盘，减少 TF 卡写入
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Dict, Optional

from clitools.config import load_config
from clitools.fnss import FnssClient, FnssError
from clitools.render import render_info, render_warning

from . import state, sync

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False


# 去抖：同一路径的事件延迟 N 秒后处理
DEBOUNCE_SECONDS = 2.0

# 轮询间隔（秒）
POLL_INTERVAL_DEFAULT = 30

# flush 频率（秒）：脏数据定期落盘
FLUSH_INTERVAL = 10

# 全局去抖计时器
_debounce_timers: Dict[str, threading.Timer] = {}
_debounce_lock = threading.Lock()

# 全局关闭信号
_shutdown = threading.Event()

# 忽略的目录名（Obsidian 和系统目录）
_IGNORE_DIRS = {
    ".obsidian", ".obsidian-mobile", ".trash", ".git", ".svn",
    "node_modules", "__pycache__", ".DS_Store", ".smtcomp",
}


def _should_ignore(path: Path) -> bool:
    """判断路径是否应该被忽略。"""
    parts = path.parts
    for part in parts:
        if part.startswith(".") and part not in (".", ".."):
            return True
        if part.startswith("._"):
            return True
        if part in _IGNORE_DIRS:
            return True
    # 只处理 .md 文件
    if path.suffix != ".md":
        return True
    return False


def _on_file_changed(remote_path: str, event_type: str) -> None:
    """文件变化处理（去抖后调用）。"""
    if event_type == "deleted":
        # 删除事件不应被 ignore 机制跳过：
        # ignore 是为防止 write_local 写入文件后触发 watchdog 循环推送，
        # 但删除不存在循环风险。如果 poll 线程拉取文件到本地时 add_ignore，
        # 随后用户删除该文件，ignore 会导致删除被跳过、远端文件不被清理。
        state.remove_ignore(remote_path)
        # 不 return，继续执行删除逻辑
    elif state.is_ignored(remote_path):
        state.remove_ignore(remote_path)
        return

    client = sync.make_client()  # 缓存的 client
    if client is None:
        render_warning("未配置 fnss 凭证，变更已缓存到 pending")
        _queue_change(remote_path, event_type)
        return

    vault = sync._get_config()["vault"]  # 缓存的 config

    if event_type in ("created", "modified"):
        content = sync.read_local(remote_path)
        if content is None:
            return  # 文件可能已被删除
        ok, err = sync.push_single_safe(client, vault, remote_path, content)
        if ok:
            render_info(f"✓ 已推送 {remote_path}")
        else:
            render_warning(f"推送失败 {remote_path}: {err}")
    elif event_type == "deleted":
        ok, err = sync.delete_single_safe(client, vault, remote_path)
        if ok:
            render_info(f"✓ 已删除远端 {remote_path}")
        elif err == "conflict_restored":
            # 冲突恢复：远端文件被其他设备修改过，已拉回本地，不报错
            render_info(f"↩ 删除取消: {remote_path} 远端有更新，已恢复到本地")
        else:
            render_warning(f"远端删除失败 {remote_path}: {err}")

    # 单次推送后异步 flush（轻量：只在有脏数据时写盘）
    state.flush_all()


def _queue_change(remote_path: str, event_type: str) -> None:
    """离线时将变更加入 pending 队列。"""
    if event_type == "deleted":
        state.queue_delete(remote_path)
    else:
        content = sync.read_local(remote_path)
        if content is not None:
            state.queue_write(remote_path, content)
    state.flush_all()


def _debounce_callback(remote_path: str, event_type: str) -> None:
    """去抖计时器回调。"""
    with _debounce_lock:
        _debounce_timers.pop(remote_path, None)
    _on_file_changed(remote_path, event_type)


def schedule_event(remote_path: str, event_type: str) -> None:
    """调度一个文件变化事件（去抖）。"""
    with _debounce_lock:
        # 取消之前的计时器（最后一次事件胜出）
        old_timer = _debounce_timers.pop(remote_path, None)
        if old_timer is not None:
            old_timer.cancel()
        timer = threading.Timer(
            DEBOUNCE_SECONDS,
            _debounce_callback,
            args=(remote_path, event_type),
        )
        timer.daemon = True
        _debounce_timers[remote_path] = timer
        timer.start()


# ---------------------------------------------------------------------------
# watchdog event handler — 合并为单个 handler，消除冗余过滤
# ---------------------------------------------------------------------------

class MarkdownFileHandler(FileSystemEventHandler):
    """watchdog 事件处理器：只处理 .md 文件变化。"""

    def __init__(self, local_root: Path):
        super().__init__()
        self._local_root = local_root.resolve()

    def _process(self, src_path: str, event_type: str) -> None:
        p = Path(src_path)
        if _should_ignore(p):
            return
        remote_path = sync.local_to_remote(p)
        if remote_path is None:
            return
        schedule_event(remote_path, event_type)

    def on_created(self, event):
        if not event.is_directory:
            self._process(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory:
            self._process(event.src_path, "modified")

    def on_deleted(self, event):
        if not event.is_directory:
            self._process(event.src_path, "deleted")

    def on_moved(self, event):
        if not event.is_directory:
            # rename = delete old + create new
            self._process(event.src_path, "deleted")
            self._process(event.dest_path, "created")


# ---------------------------------------------------------------------------
# poll thread
# ---------------------------------------------------------------------------

def _poll_loop(client: FnssClient, vault: str, interval: int) -> None:
    """远端变更轮询线程主循环。"""
    while not _shutdown.is_set():
        try:
            pulled, errors = sync.poll_remote_changes(client, vault)
            if pulled:
                render_info(f"✓ 从远端拉取 {pulled} 个变更")
            if errors:
                render_warning(f"轮询拉取 {errors} 个错误")
        except Exception as e:
            render_warning(f"轮询异常: {e}")

        # 分段等待，以便能快速响应 shutdown
        _shutdown.wait(interval)


# ---------------------------------------------------------------------------
# main watcher loop
# ---------------------------------------------------------------------------

def run_watcher(watch_dir: Optional[str] = None,
                 poll_interval: Optional[int] = None) -> int:
    """启动监控守护进程主循环（前台运行）。

    这是 daemon.py fork 后子进程调用的入口。
    """
    if not _HAS_WATCHDOG:
        render_warning("watchdog 未安装，无法启动文件监控")
        return 1

    cfg = sync._get_config()
    vault = cfg["vault"]

    # 确定本地监控目录
    local_root = sync.get_local_watch_root()
    local_root.mkdir(parents=True, exist_ok=True)

    # 确定轮询间隔
    if poll_interval is None:
        poll_interval = cfg.get("watch_poll_interval", POLL_INTERVAL_DEFAULT)

    # 从配置读取去抖延时
    global DEBOUNCE_SECONDS
    DEBOUNCE_SECONDS = cfg.get("watch_debounce", 2.0)

    render_info(f"fnsswatch 守护进程启动")
    render_info(f"  监控目录: {local_root}")
    render_info(f"  vault: {vault}")
    render_info(f"  轮询间隔: {poll_interval}s")
    render_info(f"  去抖延时: {DEBOUNCE_SECONDS}s")
    render_info(f"  flush 间隔: {FLUSH_INTERVAL}s")

    # 创建 watchdog observer
    observer = Observer()
    handler = MarkdownFileHandler(local_root)
    observer.schedule(handler, str(local_root), recursive=True)
    observer.start()

    # 启动轮询线程
    client = sync.make_client()
    if client is not None:
        poll_thread = threading.Thread(
            target=_poll_loop,
            args=(client, vault, poll_interval),
            daemon=True,
        )
        poll_thread.start()
        render_info("  远端轮询: 已启动")
    else:
        render_warning("  未配置 fnss 凭证，远端轮询未启动（仅本地→远端推送可用）")

    # 启动定期 flush 线程
    flush_thread = threading.Thread(
        target=_flush_loop, daemon=True,
    )
    flush_thread.start()

    render_info("  按 Ctrl+C 停止")

    # 主循环：用 Event.wait 代替 time.sleep，shutdown 时立即响应
    try:
        while not _shutdown.is_set():
            _shutdown.wait(timeout=3600)  # 一小时超时保活
    except KeyboardInterrupt:
        render_info("正在停止...")
    finally:
        _shutdown.set()
        # 取消所有去抖计时器
        with _debounce_lock:
            for timer in _debounce_timers.values():
                timer.cancel()
            _debounce_timers.clear()
        observer.stop()
        observer.join(timeout=5)
        # 落盘所有脏数据
        state.flush_all()
        render_info("fnsswatch 守护进程已停止")

    return 0


def _flush_loop() -> None:
    """定期 flush 脏数据到磁盘的后台线程。"""
    while not _shutdown.is_set():
        _shutdown.wait(FLUSH_INTERVAL)
        if _shutdown.is_set():
            break
        try:
            state.flush_all()
        except Exception:
            pass
