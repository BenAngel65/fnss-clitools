"""Configuration and filesystem layout for fnss-clitools.

All paths use platformdirs so the layout matches platform conventions:
    ~/.config/fnss-clitools/config.json   (Linux/Termux)
    ~/.local/share/fnss-clitools/         (Linux/Termux)

For Termux, $HOME is /data/data/com.termux/files/home, so paths end up under
~/.config/fnss-clitools and ~/.local/share/fnss-clitools as expected.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "fnss-clitools"
APP_AUTHOR = "fnss-clitools"

DEFAULT_CONFIG: Dict[str, Any] = {
    "host": "",
    "token": "",
    "vault": "defaultVault",
    "inbox_path": "INBOX.md",
    "diary_dir": "Logs/Diary",
    "notes_dir": "Inbox",
    "editor": "",
    "watch_dir": "",
    "watch_local_dir": "",          # 旧字段（单值，向后兼容）
    "watch_local_dirs": {},         # 新字段：{hostname: path}，.config 同步时按主机区分
    "watch_poll_interval": 30,
    "watch_debounce": 2.0,
}

DEFAULT_INBOX_HEADER = "# Inbox\n\n"


def config_path() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR)) / "config.json"


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, APP_AUTHOR))


def local_inbox_path() -> Path:
    return data_dir() / "inbox.md"


def pending_path() -> Path:
    return data_dir() / "pending.json"


def diary_data_dir() -> Path:
    """odiary local cache root, mirroring remote `diary_dir`."""
    return data_dir() / "diary"


def _ensure_dirs() -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)


def load_config() -> Dict[str, Any]:
    """Load config from disk; return defaults if missing."""
    _ensure_dirs()
    p = config_path()
    if not p.exists():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(DEFAULT_CONFIG)
    for key, val in DEFAULT_CONFIG.items():
        data.setdefault(key, val)
    return data


def save_config(cfg: Dict[str, Any]) -> Path:
    """Persist config; returns the path written."""
    _ensure_dirs()
    p = config_path()
    p.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(p, 0o600)
    return p


def is_configured(cfg: Dict[str, Any] | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get("host") and cfg.get("token"))


def get_local_watch_dir() -> str:
    """按 hostname 获取本机监控目录。

    因为 .config 是跨设备同步的，watch_local_dirs 用 {hostname: path} 区分。
    首次在设备上运行时不会猜测路径，而是返回空字符串，由调用方报错提示用户手动设置。

    Returns: 监控目录的绝对路径（空字符串表示当前设备未配置）
    """
    cfg = load_config()
    dirs = cfg.get("watch_local_dirs", {})
    hostname = socket.gethostname()

    if hostname in dirs:
        return dirs[hostname]

    # 迁移：旧字段 watch_local_dir 直接搬运到当前 hostname
    old = cfg.get("watch_local_dir", "").strip()
    if old:
        path = str(Path(old).expanduser())
        dirs[hostname] = path
        cfg["watch_local_dirs"] = dirs
        save_config(cfg)
        print(f"→ fnsswatch: 已从旧配置迁移 {hostname!r} → {path}")
        return path

    # 未配置，返回空串让调用方报错
    return ""


def merge_config_args(cfg: Dict[str, Any], args) -> Dict[str, Any]:
    """Apply non-None CLI overrides to a config dict."""
    changed = False
    for key in ("host", "token", "vault", "inbox_path"):
        val = getattr(args, key, None)
        if val:
            cfg[key] = val
            changed = True
    if changed:
        save_config(cfg)
    return cfg