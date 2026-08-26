"""守护进程管理 — 启动/停止/PID 管理。

守护进程通过 fork 实现：子进程运行 watcher.run_watcher()，
父进程写 PID 文件后退出。日志输出到 daemon.log。
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from clitools.config import is_configured, load_config
from clitools.render import render_error, render_info, render_success, render_warning

from . import state


def _is_running(pid: int) -> bool:
    """检查进程是否存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def read_pid() -> Optional[int]:
    """读取 daemon PID。"""
    p = state.pid_path()
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def is_daemon_running() -> bool:
    pid = read_pid()
    return pid is not None and _is_running(pid)


def start_daemon(poll_interval: Optional[int] = None) -> int:
    """启动守护进程。

    使用 double-fork 模式脱离终端，日志重定向到 daemon.log。
    Returns: 0 成功, 1 已在运行, 2 配置/启动失败
    """
    if is_daemon_running():
        render_warning("fnsswatch 守护进程已在运行")
        return 1

    cfg = load_config()
    if not is_configured(cfg):
        render_error("未配置 fnss 凭证，运行 `onote config` 设置")
        return 2

    # 确保目录存在
    local_root = state._watch_dir()
    local_root.mkdir(parents=True, exist_ok=True)

    log_file = state.log_path()
    pid_file = state.pid_path()

    # First fork
    try:
        pid = os.fork()
    except OSError as e:
        render_error(f"fork 失败: {e}")
        return 2

    if pid > 0:
        # 父进程：waitpid 等待第一次 fork 的子进程（中间进程）。
        # 中间进程会等孙进程写完 PID 文件后才退出（退出码 0），
        # 或超时退出（退出码 1）——消除"轮询 5 秒超时误报失败"的竞态。
        child_ok = False
        try:
            _, status = os.waitpid(pid, 0)
            child_ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        except OSError:
            pass
        # 中间进程退出码 0 = 孙进程 PID 已写入；非 0 = 启动失败
        if not child_ok:
            render_error("守护进程启动失败")
            return 2
        pid_val = read_pid()
        if pid_val and _is_running(pid_val):
            render_success(f"fnsswatch 守护进程已启动 (PID {pid_val})")
            return 0
        else:
            render_error("守护进程启动失败")
            return 2

    # 子进程：setsid 脱离终端
    os.setsid()

    # 子进程内存缓存清空 — 从磁盘重新加载（防止 fork 前 parent 残留的缓存）
    state.reload_from_disk()

    # Second fork
    try:
        pid2 = os.fork()
    except OSError as e:
        sys.exit(1)

    if pid2 > 0:
        # 第一次 fork 的子进程（中间进程）：
        # 等待孙进程（守护进程）写完 PID 文件后再退出。
        # 这样父进程 waitpid(pid) 返回时 PID 文件必然已写入。
        for _ in range(100):  # 最长等 10 秒（TF 卡慢也够）
            if pid_file.exists():
                os._exit(0)
            time.sleep(0.1)
        # 超时：孙进程启动失败，向父进程传递失败（通过退出码 + 不写 PID）
        os._exit(1)

    # 第二次 fork 的子进程 = 守护进程
    # 重定向 stdin/stdout/stderr 到日志文件
    sys.stdout.flush()
    sys.stderr.flush()
    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)
    # stdin → /dev/null
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    # 写 PID 文件
    my_pid = os.getpid()
    pid_file.write_text(str(my_pid), encoding="utf-8")

    # 设置 umask
    os.umask(0o077)

    # 运行 watcher 主循环
    from .watcher import run_watcher, _shutdown
    _shutdown.clear()
    try:
        rc = run_watcher(poll_interval=poll_interval)
    except Exception as e:
        print(f"守护进程异常退出: {e}", file=sys.stderr, flush=True)
        rc = 1
    finally:
        # 确保脏数据落盘
        state.flush_all()
        try:
            pid_file.unlink()
        except OSError:
            pass

    sys.exit(rc)


def stop_daemon() -> int:
    """停止守护进程。发送 SIGTERM。"""
    pid = read_pid()
    if pid is None:
        render_info("fnsswatch 守护进程未运行")
        return 0

    if not _is_running(pid):
        # 进程已不在，清理残留 PID 文件
        try:
            state.pid_path().unlink()
        except OSError:
            pass
        render_info("守护进程已不在运行（清理残留 PID 文件）")
        return 0

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        render_info("守护进程已不在运行")
        try:
            state.pid_path().unlink()
        except OSError:
            pass
        return 0

    # 等待进程退出
    for _ in range(30):
        if not _is_running(pid):
            break
        time.sleep(0.2)

    if _is_running(pid):
        # SIGTERM 没杀掉，强制 SIGKILL
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        time.sleep(0.3)

    # 清理 PID 文件
    try:
        state.pid_path().unlink()
    except OSError:
        pass

    render_success("fnsswatch 守护进程已停止")
    return 0


def status_daemon() -> int:
    """查询守护进程状态。"""
    pid = read_pid()
    if pid is None:
        render_info("fnsswatch 守护进程未运行")
        return 0

    if _is_running(pid):
        render_success(f"fnsswatch 守护进程运行中 (PID {pid})")
        # 显示日志最后几行
        log_file = state.log_path()
        if log_file.exists():
            try:
                lines = log_file.read_text(encoding="utf-8").splitlines()
                if lines:
                    render_info("最近日志:")
                    for line in lines[-5:]:
                        print(f"  {line}")
            except OSError:
                pass
        return 0
    else:
        render_warning(f"守护进程未运行（残留 PID {pid}，清理中）")
        try:
            state.pid_path().unlink()
        except OSError:
            pass
        return 0
