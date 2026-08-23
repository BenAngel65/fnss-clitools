#!/usr/bin/env bash
# fnss-clitools 通用安装脚本（Linux / macOS）
# 自动检测系统与包管理器，安装 Python ≥ 3.8 后通过 pipx / pip 安装 fnss-clitools。
set -euo pipefail

OS="$(uname -s)"
case "$OS" in
Linux) OS_FAMILY="linux" ;;
Darwin) OS_FAMILY="macos" ;;
*)
  echo "错误：不支持的操作系统 $OS（仅支持 Linux / macOS）" >&2
  exit 1
  ;;
esac

echo "==> 检测到 $OS_FAMILY ($OS)"

# ---------- 1. 检测包管理器 ----------
detect_pkg_mgr() {
  if [ "$OS_FAMILY" = "macos" ]; then
    command -v brew >/dev/null 2>&1 && {
      echo "brew"
      return
    }
    echo "none"
    return
  fi
  for mgr in apt dnf yum pacman apk zypper; do
    if command -v "$mgr" >/dev/null 2>&1; then
      echo "$mgr"
      return
    fi
  done
  echo "none"
}
PKG_MGR="$(detect_pkg_mgr)"
echo "==> 包管理器: ${PKG_MGR}"

# ---------- 2. 检测 Python ----------
detect_python() {
  for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
      if "$cmd" -c 'import sys' >/dev/null 2>&1; then
        echo "$cmd"
        return
      fi
    fi
  done
  echo ""
}
PY_CMD="$(detect_python)"

need_install_python=0
if [ -z "$PY_CMD" ]; then
  need_install_python=1
else
  if ! "$PY_CMD" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
    cur_ver="$("$PY_CMD" -V 2>&1 | awk '{print $2}')"
    echo "==> 已安装 Python $cur_ver 低于 3.8，需要升级"
    need_install_python=1
  fi
fi

# ---------- 3. 安装/升级 Python ----------
if [ "$need_install_python" = "1" ]; then
  case "$PKG_MGR" in
  apt)
    echo "==> 使用 apt 安装 python3..."
    sudo apt update -y
    sudo apt install -y python3 python3-pip python3-venv
    ;;
  dnf)
    echo "==> 使用 dnf 安装 python3..."
    sudo dnf install -y python3 python3-pip
    ;;
  yum)
    echo "==> 使用 yum 安装 python3..."
    sudo yum install -y python3 python3-pip
    ;;
  pacman)
    echo "==> 使用 pacman 安装 python..."
    sudo pacman -S --noconfirm python python-pip
    ;;
  apk)
    echo "==> 使用 apk 安装 python3..."
    sudo apk add python3 py3-pip
    ;;
  zypper)
    echo "==> 使用 zypper 安装 python3..."
    sudo zypper install -y python3 python3-pip
    ;;
  brew)
    echo "==> 使用 brew 安装 python..."
    brew install python
    ;;
  none)
    echo "错误：未检测到包管理器，请手动安装 Python ≥ 3.8 后重试" >&2
    if [ "$OS_FAMILY" = "macos" ]; then
      echo "  推荐安装 Homebrew 后重跑：https://brew.sh" >&2
      echo "  或从官网安装：https://www.python.org/downloads/macos/" >&2
    else
      echo "  使用系统包管理器安装 python3 + python3-pip 后重跑" >&2
    fi
    exit 1
    ;;
  esac

  PY_CMD="$(detect_python)"
  if [ -z "$PY_CMD" ]; then
    echo "错误：Python 安装后仍未检测到，请检查 PATH 或重新打开终端" >&2
    exit 1
  fi
fi

echo "==> 使用 Python: $PY_CMD ($("$PY_CMD" -V 2>&1 | awk '{print $2}'))"

# ---------- 4. 确保用户 bin 目录存在 ----------
# pipx 默认把命令装到 ~/.local/bin（Linux/macOS 都一样）。
# pip --user 则装到 ~/.local/bin (Linux) 或 ~/Library/Python/X.Y/bin (macOS)。
# 安装完成后根据实际安装方式确定 USER_BIN，确保 update 命令和 CLI 命令在同一目录。
# 这里先创建一个临时变量供 pipx 安装阶段刷新 PATH 用，最终 USER_BIN 在安装完成后确定。
if [ "$OS_FAMILY" = "linux" ]; then
  USER_BIN_FALLBACK="$HOME/.local/bin"
else
  PY_MINOR="$("$PY_CMD" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  USER_BIN_FALLBACK="$HOME/Library/Python/$PY_MINOR/bin"
fi
mkdir -p "$USER_BIN_FALLBACK"

# ---------- 4.5 检测并安装 pipx（PEP 668 兼容方式） ----------
# 现代发行版（Debian 12+, Ubuntu 23.04+, Fedora 38+ 等）默认禁止系统级 pip install。
# 改用 pipx，把每个 CLI 工具装在独立的 venv 里，不污染系统。
# Fallback: pip install --user（必要时加 --break-system-packages，仅 Debian 系打补丁）。
#
# 关键：仅当 "$PY_CMD" -m pipx 真正能跑才算 have_pipx=1。
# PATH 里的 pipx 二进制可能属于别的 Python，与当前 $PY_CMD 不匹配。
have_pipx=0
if "$PY_CMD" -m pipx --version >/dev/null 2>&1; then
    have_pipx=1
fi

# pipx 的实际调用方式：优先 $PY_CMD -m pipx，否则 PATH 里的 pipx
if [ "$have_pipx" = "1" ]; then
    PIPX_CMD=("$PY_CMD" -m pipx)
else
    # 保留一个 fallback（仅用于 have_pipx=1 时，但此处 have_pipx=0）
    PIPX_CMD=(pipx)
fi

if [ "$have_pipx" = "0" ]; then
    echo "==> pipx 未安装，尝试装上..."
    case "$PKG_MGR" in
    apt)
        sudo apt update -y
        sudo apt install -y pipx || sudo apt install -y python3-pipx || {
            echo "→ apt 安装 pipx 失败，尝试用 pip 方式..."
            "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
        }
        ;;
    dnf)    sudo dnf install -y pipx || {
                echo "→ dnf 安装 pipx 失败，尝试用 pip 方式..."
                "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
            } ;;
    yum)    sudo yum install -y pipx || {
                echo "→ yum 安装 pipx 失败，尝试用 pip 方式..."
                "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
            } ;;
    pacman) sudo pacman -S --noconfirm python-pipx || {
                echo "→ pacman 安装 pipx 失败，尝试用 pip 方式..."
                "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
            } ;;
    apk)    sudo apk add py3-pipx || {
                echo "→ apk 安装 pipx 失败，尝试用 pip 方式..."
                "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
            } ;;
    zypper) sudo zypper install -y python3-pipx || {
                echo "→ zypper 安装 pipx 失败，尝试用 pip 方式..."
                "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
            } ;;
    brew)   brew install pipx || {
                echo "→ brew 安装 pipx 失败，尝试用 pip 方式..."
                "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
            } ;;
    none)
        echo "→ 无包管理器可用，尝试用 pip 方式装 pipx..."
        "$PY_CMD" -m pip install --user pipx 2>/dev/null || true
        ;;
    esac
    # 刷新 PATH — pip install --user 装的 pipx 可能在 $USER_BIN
    export PATH="$USER_BIN_FALLBACK:$PATH"
    # 重新检测：必须 "$PY_CMD" -m pipx 真正可用
    if "$PY_CMD" -m pipx --version >/dev/null 2>&1; then
        have_pipx=1
        PIPX_CMD=("$PY_CMD" -m pipx)
        echo "✓ pipx 已安装"
    else
        echo "→ pipx 装不上，将用 pip --user 方式安装"
    fi
fi

# ---------- 4.8 检测 --break-system-packages 支持 ----------
# PEP 668 的 --break-system-packages 标志仅在 Debian/Ubuntu 打补丁的 pip 上存在。
# macOS、Arch、Fedora 等 pip 不认这个标志，直接传会报 unknown option。
if "$PY_CMD" -m pip install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
    PIP_BREAK="--break-system-packages"
else
    PIP_BREAK=""
fi

pip_install() { :; }  # placeholder kept for compat; no longer used

# ---------- 5. 升级 pip / pipx ----------
if [ "$have_pipx" = "1" ]; then
    echo "==> 升级 pipx..."
    "${PIPX_CMD[@]}" upgrade pipx 2>/dev/null || true
else
    echo "==> 升级 pip..."
    "$PY_CMD" -m pip install --upgrade pip --user $PIP_BREAK --quiet 2>/dev/null || true
fi

# ---------- 6. 安装 fnss-clitools ----------
echo "==> 安装 fnss-clitools..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

install_ok=0

# --- helper: 用 pip --user 安装（自动加 --break-system-packages 如果支持）---
pip_user_install() {
    if [ -n "$PIP_BREAK" ]; then
        "$PY_CMD" -m pip install --user $PIP_BREAK "$@"
    else
        "$PY_CMD" -m pip install --user "$@"
    fi
}

# 优先 PyPI
if [ "$have_pipx" = "1" ]; then
    if "${PIPX_CMD[@]}" install fnss-clitools; then
        install_ok=1
    else
        echo "→ pipx install 失败，尝试 pip --user 方式..."
        if pip_user_install fnss-clitools; then
            install_ok=1
        fi
    fi
else
    if pip_user_install fnss-clitools; then
        install_ok=1
    fi
fi

# PyPI 失败则从本地源码装
if [ "$install_ok" = "0" ]; then
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        echo "→ PyPI 不可达，从本地源码安装：$PROJECT_DIR"
        if [ "$have_pipx" = "1" ]; then
            "${PIPX_CMD[@]}" install "$PROJECT_DIR" && install_ok=1 || {
                echo "→ pipx 本地安装也失败，尝试 pip --user 方式..."
                pip_user_install "$PROJECT_DIR" && install_ok=1 || true
            }
        else
            pip_user_install "$PROJECT_DIR" && install_ok=1 || true
        fi
    else
        echo "错误：PyPI 不可达且未找到本地源码，请检查网络或手动安装" >&2
        exit 1
    fi
fi

if [ "$install_ok" = "0" ]; then
    echo "错误：fnss-clitools 安装失败，请检查网络或手动安装" >&2
    exit 1
fi
echo "✓ fnss-clitools 安装成功"

# ---------- 6.5 确定最终 USER_BIN —— 与 CLI 命令在同一目录 ----------
# pipx 默认装到 ~/.local/bin，pip --user 在 macOS 装到 ~/Library/Python/X.Y/bin。
# 直接查 oinbox 实际路径，确定命令安装目录。
INSTALLED_BIN="$(command -v oinbox 2>/dev/null || true)"
if [ -n "$INSTALLED_BIN" ]; then
    USER_BIN="$(dirname "$INSTALLED_BIN")"
else
    # fallback：检测 pipx 和 pip --user 各自的默认路径
    if [ "$have_pipx" = "1" ]; then
        USER_BIN="$HOME/.local/bin"
    else
        USER_BIN="$USER_BIN_FALLBACK"
    fi
fi
mkdir -p "$USER_BIN"

# ---------- 7. 配置 PATH ----------
# 确保用户 bin 目录在 PATH 中
SHELL_RC=""
[ -f "$HOME/.zshrc" ] && SHELL_RC="$HOME/.zshrc"
[ -z "$SHELL_RC" ] && [ -f "$HOME/.bashrc" ] && SHELL_RC="$HOME/.bashrc"
[ -z "$SHELL_RC" ] && [ -f "$HOME/.profile" ] && SHELL_RC="$HOME/.profile"

PATH_MARKER="$(basename "$USER_BIN")"
if [ -n "$SHELL_RC" ] && ! grep -qF "$PATH_MARKER" "$SHELL_RC"; then
  {
    echo ""
    echo "# fnss-clitools: user bin in PATH"
    echo "export PATH=\"$USER_BIN:\$PATH\""
  } >>"$SHELL_RC"
  echo "✓ 已将 $USER_BIN 加入 $SHELL_RC 的 PATH"
  echo "  重启终端或执行 'source $SHELL_RC' 后即可直接使用"
else
  echo "→ PATH 已包含 $PATH_MARKER 或未找到 shell rc，跳过"
fi

# 刷新当前 PATH，确保 update 命令装好后能被找到
export PATH="$USER_BIN:$PATH"

echo "==> 安装 fnssclitools-update 自更新命令..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UPDATE_SRC="$SCRIPT_DIR/fnssclitools-update.sh"
UPDATE_DST="$USER_BIN/fnssclitools-update"
if [ -f "$UPDATE_SRC" ]; then
  mkdir -p "$(dirname "$UPDATE_DST")"
  cp "$UPDATE_SRC" "$UPDATE_DST"
  chmod +x "$UPDATE_DST"
  echo "✓ 已安装 fnssclitools-update 到 $UPDATE_DST"
else
  echo "→ 未找到 $UPDATE_SRC（开发模式？跳过）"
fi

echo ""
echo "============================================"
echo "  fnss-clitools 安装完成！"
echo "  可用命令：oinbox, odiary, onote, fnsssync, fnssclitools-update"
echo "============================================"
echo ""
echo "下一步："
echo "  1. 配置 fnss 凭证（四个工具共享同一份配置）："
echo "     onote config --host https://fnss.example.com --token YOUR_TOKEN"
echo ""
echo "  2. 试运行："
echo "     oinbox list              # 拉取并渲染 INBOX.md"
echo "     oinbox 买牛奶            # 添加任务（无日期）"
echo "     odiary list              # 显示今天 # Logs"
echo "     odiary 2026-08-11 补记   # 添加到指定日期"
echo "     onote search todo        # 搜索笔记"
echo "     fnsssync                # 统一同步（推荐）"
echo "     fnssclitools-update --check  # 检查更新"
echo ""
echo "配置/数据文件位置（实际路径请运行 onote config --path 查看）："
if [ "$OS_FAMILY" = "linux" ]; then
  echo "  配置: \$XDG_CONFIG_HOME/fnss-clitools/config.json（默认 ~/.config/fnss-clitools/）"
  echo "  数据: \$XDG_DATA_HOME/fnss-clitools/         （默认 ~/.local/share/fnss-clitools/）"
else
  echo "  ~/Library/Application Support/fnss-clitools/"
fi
echo ""
echo "卸载："
if [ "$have_pipx" = "1" ]; then
    echo "  pipx uninstall fnss-clitools"
else
    echo "  pip uninstall fnss-clitools"
fi

