#!/usr/bin/env bash
# fnss-clitools 通用安装脚本（Linux / macOS）
# 自动检测系统与包管理器，安装 Python ≥ 3.8 后通过 pip 安装 fnss-clitools。
set -e

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

# ---------- 4. 检测 pipx（PEP 668 兼容方式） ----------
# 现代发行版（Debian 12+, Ubuntu 23.04+, Fedora 38+ 等）默认禁止系统级 pip install。
# 改用 pipx，把每个 CLI 工具装在独立的 venv 里，不污染系统。
# Fallback: pip install --user --break-system-packages（不推荐但能跑）。
have_pipx=0
if command -v pipx >/dev/null 2>&1; then
    have_pipx=1
elif "$PY_CMD" -m pipx --version >/dev/null 2>&1; then
    have_pipx=1
fi

if [ "$have_pipx" = "0" ] && [ "$OS_FAMILY" = "linux" ]; then
    echo "==> pipx 未安装，尝试装上..."
    case "$PKG_MGR" in
    apt)    sudo apt install -y pipx 2>/dev/null || sudo apt install -y python3-pipx 2>/dev/null ;;
    dnf)    sudo dnf install -y pipx ;;
    yum)    sudo yum install -y pipx ;;
    pacman) sudo pacman -S --noconfirm python-pipx ;;
    apk)    sudo apk add py3-pipx ;;
    zypper) sudo zypper install -y python3-pipx ;;
    brew)   brew install pipx ;;
    esac
    if command -v pipx >/dev/null 2>&1 || "$PY_CMD" -m pipx --version >/dev/null 2>&1; then
        have_pipx=1
        echo "✓ pipx 已安装"
    else
        echo "→ pipx 装不上，将用 pip --break-system-packages（仅推荐个人机）"
    fi
fi

pip_install() { :; }  # placeholder kept for compat; no longer used

# ---------- 5. 升级 pip / pipx ----------
if [ "$have_pipx" = "1" ]; then
    echo "==> 升级 pipx..."
    "$PY_CMD" -m pipx upgrade pipx || true
else
    echo "==> 升级 pip..."
    "$PY_CMD" -m pip install --upgrade pip --user --break-system-packages --quiet
fi

# ---------- 6. 安装 fnss-clitools ----------
echo "==> 安装 fnss-clitools..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

install_ok=0
# 优先 PyPI（pipx 或 pip 都行）
if [ "$have_pipx" = "1" ]; then
    if "$PY_CMD" -m pipx install fnss-clitools; then
        install_ok=1
    fi
else
    if "$PY_CMD" -m pip install --user --break-system-packages fnss-clitools; then
        install_ok=1
    fi
fi

# PyPI 失败则从本地源码装
if [ "$install_ok" = "0" ]; then
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        echo "→ PyPI 不可达，从本地源码安装：$PROJECT_DIR"
        if [ "$have_pipx" = "1" ]; then
            "$PY_CMD" -m pipx install "$PROJECT_DIR" || true
        else
            "$PY_CMD" -m pip install --user --break-system-packages "$PROJECT_DIR" || true
        fi
    else
        echo "错误：PyPI 不可达且未找到本地源码，请检查网络或手动安装" >&2
        exit 1
    fi
fi

# ---------- 7. 配置 PATH ----------
# Linux:  ~/.local/bin
# macOS:  ~/Library/Python/<major>.<minor>/bin  (PEP 370)
if [ "$OS_FAMILY" = "linux" ]; then
  USER_BIN="$HOME/.local/bin"
else
  PY_MINOR="$("$PY_CMD" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  USER_BIN="$HOME/Library/Python/$PY_MINOR/bin"
fi

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

