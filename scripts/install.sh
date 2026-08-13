#!/usr/bin/env bash
# fnss-clitools 通用安装脚本（Linux / macOS）
# 自动检测系统与包管理器，安装 Python ≥ 3.8 后通过 pip 安装 fnss-clitools。
set -e

OS="$(uname -s)"
case "$OS" in
    Linux)  OS_FAMILY="linux" ;;
    Darwin) OS_FAMILY="macos" ;;
    *) echo "错误：不支持的操作系统 $OS（仅支持 Linux / macOS）" >&2; exit 1 ;;
esac

echo "==> 检测到 $OS_FAMILY ($OS)"

# ---------- 1. 检测包管理器 ----------
detect_pkg_mgr() {
    if [ "$OS_FAMILY" = "macos" ]; then
        command -v brew >/dev/null 2>&1 && { echo "brew"; return; }
        echo "none"; return
    fi
    for mgr in apt dnf yum pacman apk zypper; do
        if command -v "$mgr" >/dev/null 2>&1; then
            echo "$mgr"; return
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
                echo "$cmd"; return
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

# ---------- 4. 确保 pip 可用 ----------
if ! "$PY_CMD" -m pip --version >/dev/null 2>&1; then
    echo "==> pip 不可用，尝试 ensurepip..."
    "$PY_CMD" -m ensurepip --upgrade --user || {
        echo "错误：无法初始化 pip，请手动安装后重试" >&2; exit 1;
    }
fi

# ---------- 5. 升级 pip ----------
echo "==> 升级 pip..."
"$PY_CMD" -m pip install --upgrade pip --user --quiet

# ---------- 6. 安装 fnss-clitools ----------
echo "==> 安装 fnss-clitools..."
if "$PY_CMD" -m pip install --user fnss-clitools 2>/dev/null; then
    echo "✓ 从 PyPI 安装成功"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        echo "→ 从本地源码安装：$PROJECT_DIR"
        "$PY_CMD" -m pip install --user "$PROJECT_DIR"
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
    } >> "$SHELL_RC"
    echo "✓ 已将 $USER_BIN 加入 $SHELL_RC 的 PATH"
    echo "  重启终端或执行 'source $SHELL_RC' 后即可直接使用"
else
    echo "→ PATH 已包含 $PATH_MARKER 或未找到 shell rc，跳过"
fi

echo ""
echo "============================================"
echo "  fnss-clitools 安装完成！"
echo "  可用命令：oinbox, odiary"
echo "============================================"
echo ""
echo "下一步："
echo "  1. 配置 fnss 凭证（两个工具共享同一份配置）："
echo "     oinbox config --host https://fnss.example.com --token YOUR_TOKEN"
echo ""
echo "  2. 试运行："
echo "     oinbox list              # 拉取并渲染 INBOX.md"
echo "     oinbox 买牛奶            # 添加任务（无日期）"
echo "     odiary list              # 显示今天 # Logs"
echo "     odiary 2026-08-11 补记   # 添加到指定日期"
echo ""
echo "配置/数据文件位置（实际路径请运行 oinbox config --path 查看）："
if [ "$OS_FAMILY" = "linux" ]; then
    echo "  配置: \$XDG_CONFIG_HOME/fnss-clitools/config.json（默认 ~/.config/fnss-clitools/）"
    echo "  数据: \$XDG_DATA_HOME/fnss-clitools/         （默认 ~/.local/share/fnss-clitools/）"
else
    echo "  ~/Library/Application Support/fnss-clitools/"
fi
echo ""
echo "卸载："
echo "  $PY_CMD -m pip uninstall fnss-clitools"