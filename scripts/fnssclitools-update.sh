#!/usr/bin/env bash
# fnss-clitools 自更新脚本
# 从 GitHub 拉取最新 release tag 的源码 tarball 并通过 pip 安装。
#
# 推荐安装位置: ~/.local/bin/fnssclitools-update (在 PATH 中)
# 这样调用:   fnssclitools-update
#
# 用法:
#   fnssclitools-update          # 交互式确认
#   fnssclitools-update --yes    # 跳过确认
#   fnssclitools-update --check  # 只检查，不下载安装
#
# 要求:
#   - python3 (>= 3.8) 在 PATH
#   - pip 可用
#
# 平台:
#   - Linux/macOS: pip install --user
#   - Termux:      pip install --target=$HOME/.local/lib/pythonX.Y/site-packages
set -e

REPO="BenAngel65/fnss-clitools"
GITHUB_API="https://api.github.com/repos/${REPO}"
CODELOAD="https://codeload.github.com/${REPO}"

# ---------- 参数解析 ----------
YES=0
CHECK_ONLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes) YES=1; shift ;;
        -c|--check) CHECK_ONLY=1; shift ;;
        -h|--help)
            sed -n '2,15p' "$0"
            exit 0 ;;
        *)
            echo "未知参数: $1" >&2
            exit 1 ;;
    esac
done

# ---------- 检测 Python ----------
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    echo "错误：找不到 python3 或 python" >&2
    echo "  请先安装 Python ≥ 3.8" >&2
    exit 1
fi

PY_VER=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MINOR="$PY_VER"

# ---------- 检测当前版本 ----------
CURRENT=""
for cmd in onote odiary oinbox; do
    if command -v "$cmd" >/dev/null 2>&1; then
        # 输出形如 "onote 0.2.0" — 取第二列
        ver=$("$cmd" --version 2>/dev/null | awk '{print $2; exit}')
        if [ -n "$ver" ]; then
            CURRENT="$ver"
            break
        fi
    fi
done

if [ -z "$CURRENT" ]; then
    echo "错误：未检测到 fnss-clitools 安装" >&2
    echo "  请先运行: bash scripts/install.sh" >&2
    exit 1
fi

echo "==> 当前版本: $CURRENT"

# ---------- 查询 GitHub 最新 release ----------
echo "==> 查询 GitHub 最新版本..."
RELEASE_JSON=$(curl -sSL --max-time 10 "${GITHUB_API}/releases/latest") || {
    echo "错误：无法访问 GitHub API（网络问题或达到 rate limit）" >&2
    exit 1
}

# 检查 HTTP 状态码（curl 不带 -f 时不会自动退出）
HTTP_CODE=$(curl -sSL -o /dev/null -w '%{http_code}' --max-time 10 "${GITHUB_API}/releases/latest")
if [ "$HTTP_CODE" = "404" ]; then
    echo "错误：GitHub 仓库还没有任何 release tag" >&2
    echo "  请先创建首个 release: gh release create v0.2.1 --generate-notes" >&2
    exit 1
fi
if [ "$HTTP_CODE" != "200" ]; then
    echo "错误：GitHub API 返回 HTTP $HTTP_CODE（网络问题或达到 rate limit）" >&2
    exit 1
fi

# 用 python3 解析 JSON（避免依赖 jq）
LATEST_TAG=$(echo "$RELEASE_JSON" | "$PYTHON" -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d['tag_name'].lstrip('v'))
except Exception as e:
    sys.stderr.write(f'解析失败: {e}\n')
    sys.exit(1)
" 2>/dev/null) || {
    echo "错误：无法解析 GitHub API 响应（可能还没有任何 release tag）" >&2
    echo "  请先用 gh release create v0.2.1 创建首个 release" >&2
    exit 1
}

echo "==> 最新版本: $LATEST_TAG"

# 显示 changelog（如果有）
CHANGELOG=$(echo "$RELEASE_JSON" | "$PYTHON" -c "
import json, sys
d = json.load(sys.stdin)
print(d.get('body', '').strip())
" 2>/dev/null || echo "")
if [ -n "$CHANGELOG" ]; then
    echo ""
    echo "==> 更新说明:"
    echo "$CHANGELOG" | head -30
    echo ""
fi

# ---------- 比较 ----------
if [ "$CURRENT" = "$LATEST_TAG" ]; then
    echo "✓ 已是最新版本 ($CURRENT)"
    exit 0
fi

# ---------- 只检查模式 ----------
if [ "$CHECK_ONLY" = "1" ]; then
    echo "i 发现新版本: $CURRENT → $LATEST_TAG"
    echo "  运行 'bash scripts/update.sh' 进行更新"
    exit 0
fi

# ---------- 确认 ----------
if [ "$YES" != "1" ]; then
    read -p "==> 更新到 $LATEST_TAG? [y/N] " -n 1
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "已取消"
        exit 0
    fi
fi

# ---------- 下载 ----------
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

TARBALL="$TMPDIR/fnss-clitools.tar.gz"
echo "==> 下载源码 tarball..."
curl -fsSL --max-time 60 -o "$TARBALL" "${CODELOAD}/tar.gz/refs/tags/v${LATEST_TAG}" || {
    echo "错误：下载 tarball 失败" >&2
    exit 1
}

# ---------- 检测安装方式 ----------
if [[ "$(uname -s)" == "Android" ]] || [[ -n "${TERMUX_VERSION:-}" ]]; then
    TARGET="$HOME/.local/lib/python${PY_MINOR}/site-packages"
    echo "==> 检测到 Termux，安装到: $TARGET"
    INSTALL_CMD=("$PYTHON" -m pip install
                 --upgrade
                 --break-system-packages
                 --target="$TARGET"
                 --no-cache-dir)
else
    echo "==> 安装到 user site-packages"
    INSTALL_CMD=("$PYTHON" -m pip install
                 --upgrade
                 --break-system-packages
                 --user
                 --no-cache-dir)
fi

# ---------- 安装 ----------
echo "==> 安装中..."
"${INSTALL_CMD[@]}" "$TARBALL" || {
    echo "错误：pip install 失败" >&2
    echo "  手动命令：" >&2
    echo "    pip install --user --upgrade \"$TARBALL\"" >&2
    exit 1
}

# ---------- 完成 ----------
echo ""
echo "✓ 更新成功: $CURRENT → $LATEST_TAG"
echo ""
echo "提示：新版本已安装。"
echo "  - 如果用 'pip install --user'，wrapper 脚本可能被覆盖；"
echo "    重启终端或 'hash -r' 让新 PATH 生效"
echo "  - 如果用 Termux 的 --target，命令路径不变；"
echo "    直接执行 oinbox/odiary/onote 即可"