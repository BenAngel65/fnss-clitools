#!/data/data/com.termux/files/usr/bin/bash
# Termux 一键安装脚本（fnss-clitools = oinbox + odiary）
set -e

echo "==> 检测 Termux 环境..."
if [ -z "$PREFIX" ] || [ ! -d "/data/data/com.termux" ]; then
    echo "错误：此脚本仅在 Termux 中运行" >&2
    exit 1
fi

echo "==> 更新源..."
pkg update -y

echo "==> 安装 Python..."
pkg install -y python

echo "==> 升级 pip..."
python -m pip install --upgrade pip --quiet

echo "==> 安装 fnss-clitools..."
# 优先从 PyPI 安装；如果失败则尝试从本地目录安装（开发者模式）
if python -m pip install --user fnss-clitools 2>/dev/null; then
    echo "✓ 从 PyPI 安装成功"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
    if [ -f "$PROJECT_DIR/pyproject.toml" ]; then
        echo "→ 从本地源码安装：$PROJECT_DIR"
        python -m pip install --user "$PROJECT_DIR"
    else
        echo "错误：本地安装失败，请检查网络或手动安装" >&2
        exit 1
    fi
fi

echo "==> 安装依赖（requests / rich / platformdirs）..."
python -m pip install --user requests rich platformdirs --quiet

echo "==> 配置 PATH..."
SHELL_RC=""
[ -f "$HOME/.zshrc" ] && SHELL_RC="$HOME/.zshrc"
[ -z "$SHELL_RC" ] && [ -f "$HOME/.bashrc" ] && SHELL_RC="$HOME/.bashrc"
if [ -n "$SHELL_RC" ] && ! grep -q '\.local/bin' "$SHELL_RC"; then
    sed -i 's|^export PATH=|export PATH=$HOME/.local/bin:|' "$SHELL_RC"
    echo "✓ 已将 \$HOME/.local/bin 加入 $SHELL_RC 的 PATH"
    echo "  重启 Termux 或执行 'source $SHELL_RC' 后即可直接使用"
else
    echo "→ PATH 已包含 .local/bin，跳过"
fi

echo "==> 修复 Termux 专用 shebang..."
# Termux 设备上 /usr/bin/env 不存在（或指向 toybox），导致依赖 env 解析的
# shebang 失败。把已安装脚本的 shebang 改成 Termux 专属绝对路径。
if [ -f "$HOME/.local/bin/fnssclitools-update" ]; then
    TERMUX_BASH="/data/data/com.termux/files/usr/bin/bash"
    if [ -x "$TERMUX_BASH" ]; then
        sed -i "1s|.*|#!$TERMUX_BASH|" "$HOME/.local/bin/fnssclitools-update" 2>/dev/null || true
        echo "✓ fnssclitools-update shebang 已改为 Termux 路径"
    fi
fi

echo ""
echo "============================================"
echo "  fnss-clitools 安装完成！"
echo "  安装命令：oinbox, odiary, onote, fnsssync"
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
echo ""
echo "配置/数据文件位置："
echo "  ~/.config/fnss-clitools/config.json   # 共享配置"
echo "  ~/.local/share/fnss-clitools/         # 数据"