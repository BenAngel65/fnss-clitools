"""Editor selection and invocation (shared by onote / odiary).

Priority:
    1. env vars (in priority order, e.g. ``("ONOTE_EDITOR", "EDITOR")``)
    2. config["editor"] if set in shared config.json
    3. nvim in PATH
    4. vim in PATH
    5. auto-install (apt/dnf/pacman/apk/brew/pkg)
    6. error

Strict config semantics: if ``config_editor`` is set but the binary is not
found, the resolver raises a specific error (no silent fallback to nvim/vim).
This respects the user's explicit choice.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional, Tuple

from .render import render_error, render_info, render_warning


def _find_installed_editor() -> Optional[str]:
    """Look for nvim/vim in PATH. None if neither is present."""
    for cmd in ("nvim", "vim"):
        if shutil.which(cmd):
            return cmd
    return None


def _auto_install_linux() -> bool:
    """Try installing neovim via the available package manager. Returns True on success."""
    for mgr in ("apt", "dnf", "yum", "pacman", "apk", "zypper"):
        if not shutil.which(mgr):
            continue
        try:
            if mgr == "apt":
                cmd = ["sudo", "apt", "install", "-y", "neovim"]
            elif mgr in ("dnf", "yum"):
                cmd = ["sudo", mgr, "install", "-y", "neovim"]
            elif mgr == "pacman":
                cmd = ["sudo", "pacman", "-S", "--noconfirm", "neovim"]
            elif mgr == "apk":
                cmd = ["sudo", "apk", "add", "neovim"]
            elif mgr == "zypper":
                cmd = ["sudo", "zypper", "install", "-y", "neovim"]
            else:
                continue
            render_info(f"未检测到 nvim/vim，尝试通过 {mgr} 安装 neovim...")
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode == 0 and shutil.which("nvim"):
                return True
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
    return False


def _auto_install_macos() -> bool:
    if not shutil.which("brew"):
        return False
    try:
        render_info("未检测到 nvim/vim，尝试通过 brew 安装 neovim...")
        r = subprocess.run(["brew", "install", "neovim"], capture_output=True, text=True)
        if r.returncode == 0 and shutil.which("nvim"):
            return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return False


def _auto_install_termux() -> bool:
    if not shutil.which("pkg"):
        return False
    try:
        render_info("未检测到 nvim/vim，尝试通过 pkg 安装 neovim...")
        r = subprocess.run(["pkg", "install", "-y", "neovim"], capture_output=True, text=True)
        if r.returncode == 0 and shutil.which("nvim"):
            return True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return False


def _auto_install() -> bool:
    system = platform.system()
    if system == "Linux":
        return _auto_install_linux()
    if system == "Darwin":
        return _auto_install_macos()
    if system == "Android" or "TERMUX_VERSION" in os.environ:
        return _auto_install_termux()
    return False


class EditorConfigError(Exception):
    """Raised when config specifies an editor that is not installed."""


def ensure_editor(
    env_vars: Iterable[str] = ("EDITOR",),
    config_editor: str = "",
) -> str:
    """Resolve editor command by priority.

    Args:
        env_vars: env var names to check (in priority order).
        config_editor: value from config["editor"]; if non-empty, must be
            installed or EditorConfigError is raised.

    Returns:
        The editor command (string).

    Raises:
        EditorConfigError: config specifies an editor binary that is missing.
        RuntimeError: no editor could be resolved.
    """
    # 1. Env vars (highest priority)
    for name in env_vars:
        val = os.environ.get(name)
        if val:
            return val

    # 2. Config-specified editor (strict — must be installed)
    if config_editor:
        if shutil.which(config_editor):
            return config_editor
        raise EditorConfigError(
            f"配置中 editor={config_editor!r} 但本机未找到该编辑器。"
            f"请安装 {config_editor} 或修改 ~/.config/fnss-clitools/config.json。"
        )

    # 3. nvim/vim in PATH
    found = _find_installed_editor()
    if found:
        return found

    # 4. Auto-install neovim
    if _auto_install():
        return "nvim"

    # 5. Nothing worked
    raise RuntimeError("未找到任何可用的编辑器")


def run_editor(editor: str, file_path: Path) -> int:
    """Invoke editor and wait for exit. Returns the editor's return code."""
    r = subprocess.run([editor, str(file_path)])
    return r.returncode


def edit_with_fallback(
    file_path: Path,
    env_vars: Iterable[str] = ("EDITOR",),
    config_editor: str = "",
) -> Tuple[int, str]:
    """Open file in editor. Returns (returncode, editor_used).

    editor_used is "" on failure. The caller should check both values.
    Errors are rendered to the terminal; this function does not raise.
    """
    try:
        editor = ensure_editor(env_vars=env_vars, config_editor=config_editor)
    except EditorConfigError as e:
        render_error(str(e))
        return (1, "")
    except RuntimeError as e:
        render_error(str(e))
        render_warning(
            "请手动安装：Termux: pkg install neovim | Linux: apt install neovim | macOS: brew install neovim"
        )
        return (1, "")

    rc = run_editor(editor, file_path)
    return (rc, editor)