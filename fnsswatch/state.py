"""同步状态追踪 — 记录每个文件的上次同步版本。

state.json 位于 data_dir()/fnsswatch/state.json，结构：
{
  "Notes/foo.md": {
    "local_mtime": "2026-08-24T14:30:00.123456",
    "remote_version": 5,
    "local_hash": "a1b2c3d4",
    "synced_at": "2026-08-24T14:30:05"
  },
  ...
}

防循环标记存储在 data_dir()/fnsswatch/ignore.json，是一个
临时路径集合，写入完成后自动清除。

pending 队列存储在 data_dir()/fnsswatch/pending.json。

--- 内存缓存层（针对 TF 卡 / 低 IOPS 设备优化）---

state / ignore / pending 三份数据在内存中各维护一份缓存，
修改时只标记脏位（dirty flag），由调用方在合适时机调用
flush_all() 批量落盘。这样在文件频繁变更时可避免每次都
全量序列化 + 写 TF 卡。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from clitools.config import data_dir

_SUBDIR = "fnsswatch"


# ---------------------------------------------------------------------------
# 内存缓存 + 脏标记
# ---------------------------------------------------------------------------

_state_cache: Optional[Dict[str, Any]] = None
_ignore_cache: Optional[Set[str]] = None
_pending_cache: Optional[List[dict]] = None

_state_dirty = False
_ignore_dirty = False
_pending_dirty = False

_import_lock = __import__("threading").Lock()


# ---------------------------------------------------------------------------
# 路径辅助
# ---------------------------------------------------------------------------

def _watch_dir() -> Path:
    p = data_dir() / _SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path() -> Path:
    return _watch_dir() / "state.json"


def ignore_path() -> Path:
    return _watch_dir() / "ignore.json"


def pending_path() -> Path:
    return _watch_dir() / "pending.json"


def pid_path() -> Path:
    return _watch_dir() / "daemon.pid"


def log_path() -> Path:
    return _watch_dir() / "daemon.log"


def base_dir() -> Path:
    """base 内容缓存目录（存上次同步时的文件内容，用于 3-way merge）。"""
    p = _watch_dir() / "base"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _base_path(remote_path: str) -> Path:
    """remote_path → base 缓存文件路径。

    用 / 分隔的 vault 路径直接映射到 base 目录下的文件。
    """
    # 安全处理：路径中的 / 对应子目录
    return base_dir() / remote_path


# ---------------------------------------------------------------------------
# 原子写入（防止 TF 卡突然断电导致文件损坏）
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, text: str) -> None:
    """先写临时文件再 rename，确保原子性。"""
    dir_ = path.parent
    dir_.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dir_), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))  # 原子 rename
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# state（内存缓存）
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    """返回 state 内存缓存（首次调用时从磁盘加载）。"""
    global _state_cache
    with _import_lock:
        if _state_cache is not None:
            return _state_cache
        p = state_path()
        if not p.exists():
            _state_cache = {}
            return _state_cache
        try:
            _state_cache = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _state_cache = {}
        return _state_cache


def save_state(state: Dict[str, Any]) -> None:
    """更新内存缓存并标记脏位（不一定立即写盘）。"""
    global _state_cache, _state_dirty
    with _import_lock:
        _state_cache = state
        _state_dirty = True


def get_file_state(remote_path: str) -> Optional[dict]:
    return load_state().get(remote_path)


def update_file_state(remote_path: str, *, local_mtime: str,
                      remote_version: Any = None, local_hash: str = "",
                      synced_at: Optional[str] = None) -> None:
    """更新单个文件状态（内存操作，标记脏位）。"""
    global _state_dirty
    st = load_state()
    entry = st.get(remote_path, {})
    entry["local_mtime"] = local_mtime
    if remote_version is not None:
        entry["remote_version"] = remote_version
    if local_hash:
        entry["local_hash"] = local_hash
    entry["synced_at"] = synced_at or datetime.now().isoformat(timespec="seconds")
    st[remote_path] = entry
    with _import_lock:
        _state_dirty = True


def remove_file_state(remote_path: str) -> None:
    global _state_dirty
    st = load_state()
    if remote_path in st:
        st.pop(remote_path, None)
        with _import_lock:
            _state_dirty = True


def force_save_state() -> None:
    """立即将 state 落盘（即使没标记脏位也强制写）。

    注意：json.dumps 遍历 dict 时不持锁（避免阻塞其他线程），
    偶发的 RuntimeError（其他线程同时修改 dict）会被捕获跳过，
    下次 flush 重试。
    """
    global _state_dirty
    st = load_state()
    try:
        text = json.dumps(st, indent=2, ensure_ascii=False) + "\n"
    except (RuntimeError, ValueError):
        return  # 竞态，放弃本次写盘
    p = state_path()
    _atomic_write(p, text)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    with _import_lock:
        _state_dirty = False


# ---------------------------------------------------------------------------
# hash
# ---------------------------------------------------------------------------

def compute_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# base 内容缓存（上次同步时的文件内容，用于 3-way merge）
# ---------------------------------------------------------------------------

def save_base_content(remote_path: str, content: str) -> None:
    """保存上次同步时的文件内容。"""
    p = _base_path(remote_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, content)


def load_base_content(remote_path: str) -> Optional[str]:
    """读取上次同步时的文件内容。"""
    p = _base_path(remote_path)
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def remove_base_content(remote_path: str) -> None:
    """删除 base 缓存（文件被删除时清理）。"""
    p = _base_path(remote_path)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
        # 清理空目录
        try:
            p.parent.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ignore set（内存缓存，防循环）
# ---------------------------------------------------------------------------

def load_ignore() -> Set[str]:
    global _ignore_cache
    with _import_lock:
        if _ignore_cache is not None:
            return _ignore_cache
        p = ignore_path()
        if not p.exists():
            _ignore_cache = set()
            return _ignore_cache
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _ignore_cache = set(data) if isinstance(data, list) else set()
        except (json.JSONDecodeError, OSError):
            _ignore_cache = set()
        return _ignore_cache


def save_ignore(paths: Set[str]) -> None:
    global _ignore_cache, _ignore_dirty
    with _import_lock:
        _ignore_cache = paths
        _ignore_dirty = True


def add_ignore(remote_path: str) -> None:
    global _ignore_dirty
    ig = load_ignore()
    if remote_path not in ig:
        ig.add(remote_path)
        with _import_lock:
            _ignore_dirty = True


def remove_ignore(remote_path: str) -> None:
    global _ignore_dirty
    ig = load_ignore()
    if remote_path in ig:
        ig.discard(remote_path)
        with _import_lock:
            _ignore_dirty = True


def is_ignored(remote_path: str) -> bool:
    return remote_path in load_ignore()


def force_save_ignore() -> None:
    global _ignore_dirty
    with _import_lock:
        ig = load_ignore()
        ig_copy = sorted(ig)  # sorted 返回新 list，安全
        _ignore_dirty = False
    p = ignore_path()
    if not ig_copy:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    else:
        _atomic_write(
            p,
            json.dumps(ig_copy, ensure_ascii=False) + "\n",
        )


# ---------------------------------------------------------------------------
# pending queue（内存缓存）
# ---------------------------------------------------------------------------

def load_pending() -> List[dict]:
    global _pending_cache
    with _import_lock:
        if _pending_cache is not None:
            return _pending_cache
        p = pending_path()
        if not p.exists():
            _pending_cache = []
            return _pending_cache
        try:
            _pending_cache = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _pending_cache = []
        return _pending_cache


def save_pending(items: List[dict]) -> None:
    global _pending_cache, _pending_dirty
    with _import_lock:
        _pending_cache = items
        _pending_dirty = True


def queue_write(remote_path: str, content: str) -> None:
    global _pending_dirty
    items = load_pending()
    items.append({
        "op": "write",
        "path": remote_path,
        "content": content,
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    })
    with _import_lock:
        _pending_dirty = True


def queue_delete(remote_path: str) -> None:
    global _pending_dirty
    items = load_pending()
    items.append({
        "op": "delete",
        "path": remote_path,
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    })
    with _import_lock:
        _pending_dirty = True


def force_save_pending() -> None:
    global _pending_dirty
    with _import_lock:
        items = load_pending()
        items_copy = list(items)  # 浅拷贝 list，防止其他线程 append
        _pending_dirty = False
    p = pending_path()
    if not items_copy:
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    else:
        _atomic_write(
            p,
            json.dumps(items_copy, indent=2, ensure_ascii=False) + "\n",
        )


# ---------------------------------------------------------------------------
# 批量 flush — 一次性把脏数据落盘
# ---------------------------------------------------------------------------

def flush_all() -> None:
    """把所有脏缓存（state / ignore / pending）落盘。

    在守护进程主循环中定期调用（如每 10 秒），以及进程退出前调用。
    """
    if _state_dirty:
        force_save_state()
    if _ignore_dirty:
        force_save_ignore()
    if _pending_dirty:
        force_save_pending()


def reload_from_disk() -> None:
    """丢弃内存缓存，强制从磁盘重新加载（用于多进程场景）。"""
    global _state_cache, _ignore_cache, _pending_cache
    global _state_dirty, _ignore_dirty, _pending_dirty
    with _import_lock:
        _state_cache = None
        _ignore_cache = None
        _pending_cache = None
        _state_dirty = False
        _ignore_dirty = False
        _pending_dirty = False
