# fnss-clitools

> 离线优先的 CLI 工具集，适配 [fast-note-sync-service](https://github.com/haierkeys/fast-note-sync-service)（fnss）。
> 主战场：**Termux**（Android），同时支持 Linux/macOS。

设计哲学：**先输入，再同步**。本机写文件永远不等网络，断网也能用，恢复连接后自动补齐。

## 工具

| 命令 | 用途 |
| --- | --- |
| `oinbox` | 任务收件箱 — 追加 / 查询 `INBOX.md` |
| `odiary` | 日记 Logs — 按日期追加 / 查询每日 `# Logs` 段（支持编辑器模式） |
| `onote` | 笔记 CRUD — 搜索 / 编辑 / 删除（vim/nvim 驱动） |

三个工具共享同一份配置（host / token / vault）。

## 安装

### Termux

```bash
bash scripts/install-termux.sh
# 或手动
pkg install python
pip install --user /path/to/fnss-clitools   # 或 pip install fnss-clitools
```

### Linux / macOS

```bash
bash scripts/install.sh
# 或手动：见下方依赖
```

依赖：Python ≥ 3.8，纯 Python wheel，**无 C 扩展**。

## 更新

```bash
bash scripts/update.sh          # 交互式确认升级
bash scripts/update.sh --check  # 只检查，不下载安装
bash scripts/update.sh --yes    # 跳过确认（脚本化调用）
```

脚本从 GitHub Releases 拉最新源码 tarball，自动检测平台（Termux 用 `--target`，Linux/macOS 用 `--user`），通过 `pip install` 完成升级。

升级成功后：
- `--user` 安装：重启终端或 `hash -r` 让新 PATH 生效
- Termux `--target`：直接执行 `oinbox/odiary/onote` 即可

## 快速开始

```bash
# 共享一次配置（host / token / vault）
oinbox config --host https://fnss.example.com --token eyJhbGc...

# oinbox
oinbox list                   # 拉取并渲染 INBOX.md
oinbox 买牛奶                 # 追加任务（纯内容，无日期）

# odiary
odiary list                   # 显示今天 # Logs
odiary 写完了项目文档         # 添加到今天
odiary 2026-8-11 补记昨天     # 添加到指定日期
odiary list 2026-08-10        # 显示指定日期 # Logs

# 手动同步（离线缓存会自动在 list/add 时推送）
oinbox sync
odiary sync
```

## 命令参考

### `oinbox`

| 命令 | 行为 |
| --- | --- |
| `oinbox <文本...>` | 追加任务，自动同步 |
| `oinbox list` / `oinbox ls` | 拉取远端 + 渲染本地 |
| `oinbox add <文本...>` | 同上但显式 |
| `oinbox sync` | 拉取 + 推送 pending |
| `oinbox config` | 配置 / 查看 |

`oinbox` 写入格式：`- [ ] <内容>`（无时间戳，保持简洁）。

### `odiary`

| 命令 | 行为 |
| --- | --- |
| `odiary <文本...>` | 追加日志到今天 |
| `odiary <日期> <文本...>` | 追加日志到指定日期（`YYYY-MM-DD` 或 `YYYY-M-D`） |
| `odiary list` / `odiary ls` | 显示今天 # Logs |
| `odiary list <日期>` | 显示指定日期 # Logs |
| `odiary add <文本...>` | 同上但显式 |
| `odiary edit` | 编辑器模式：写大段文字，自动加时间戳前缀 |
| `odiary edit <日期>` | 编辑指定日期（编辑器模式） |
| `odiary sync` | 推送 pending |
| `odiary config` | 配置 / 查看 |

`odiary add` 写入格式：`- ⌚HH:MM <内容>`，自动追加在日记文件 `# Logs` 段末尾。

> Obsidian 会把 `- ⌚HH:MM` 渲染为 bulleted list item（这是 Markdown 标准行为）。如果觉得「list」结构不好，避免在 entry body 里用 `1.` `2.` 这种 ordered list 起始符，否则会变成嵌套结构。

**每次写入自动归一化**：与 QuickAdd、Obsidian 等插件混用同一文件时容易累积多余空行。`odiary` 写入本地缓存与服务端前会执行：

1. 折叠 3+ 个连续换行为 2 个（相邻 entry 之间最多 1 个 blank line）
2. 去掉尾部空白，确保文件以**单个** `\n` 结尾（POSIX），最后一行永远是日记内容

这样 `odiary add/edit/sync` 之后服务端永远是干净格式。Obsidian 下次 sync 拉取就会得到清理后的内容（`odiary` 不直接修改 Obsidian 本地 vault）。

`odiary edit` 适用场景：写多行、列表、段落。打开 vim/nvim，初始内容是 `- ⌚HH:MM `（当前时间），用户编辑后整段内容作为一条 entry 追加。

格式保证：
- 上方空行隔开（`\n\n`）
- 下方紧跟内容，不空行
- 最后一行是文字（仅 POSIX 末尾 `\n`）

支持多行 entry：用户写多少行都视为一条。

时间戳处理：
- 用户保留 `- ⌚HH:MM ` → 用其原样
- 用户改成别的 `- ⌚HH:MM ` → 用新时间戳
- 用户删掉时间戳行 → 用启动时的时间重新包装

**取消条件**：timestamp 后 body 完全空白 → 取消，不推送。

### `onote`

| 命令 | 行为 |
| --- | --- |
| `onote <标题...>` | strict create：写入 `Inbox/<标题>.md` → 调 vim/nvim 编辑 → `:q` 后 POST |
| `onote new <标题...>` | 显式新建（已存在则报错） |
| `onote edit <编号\|路径>` | 编辑（编号来自 `search`，或直接给路径） |
| `onote open <编号\|路径>` | 终端 rich 渲染内容 |
| `onote delete <编号\|路径>` | 删除（默认二次确认；`--yes` 跳过；硬删） |
| `onote search <query>` | 路径/title 搜索（默认） |
| `onote search -c <query>` | 内容搜索 |
| `onote sync` | 推送 pending 队列 |
| `onote config` | 配置（含 `--notes-dir`） |

**编辑器优先级**：`$ONOTE_EDITOR` > `$EDITOR` > `nvim` > `vim` > 自动安装（apt/dnf/pacman/apk/brew/pkg）> 报错。

**路径规范化**：
- 裸标题 `topic` → `Inbox/topic.md`
- 子目录裸标题 `sub/topic` → `sub/topic.md`
- `.md` 结尾视为已限定路径

**ref 引用**：`edit/open/delete <ref>` 的 `ref` 可以是数字编号（来自 `search` 输出，TTL 24h）或 vault-相对路径。`last_search.json` 缓存于 `~/.local/share/fnss-clitools/notes/`，过期后编号失效。

**搜索在线要求**：`onote search` 直接调 fnss 服务端 SQLite FTS5（`GET /api/notes`），离线时报错并提示联网。无本地索引。

### 共享 config

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `--host` | (空) | fnss 服务地址 |
| `--token` | (空) | fnss 认证 Token |
| `--vault` | `defaultVault` | vault 名称 |
| `--inbox-path` | `INBOX.md` | oinbox 远端路径 |
| `--diary-dir` | `Logs/Diary` | odiary 远端目录 |
| `--notes-dir` | `Inbox` | onote 默认笔记目录 |
| `--show` | — | 显示当前配置 |
| `--path` | — | 显示文件路径 |

`config.json` 中可直接编辑 `editor` 字段（详见下方「编辑器选择」），无需 CLI flag。

### 编辑器选择（onote / odiary edit 共用）

优先级：
1. `$ONOTE_EDITOR` / `$ODIARY_EDITOR` env（工具专属）
2. `$EDITOR` env（通用）
3. `config["editor"]` 字段
4. `nvim` 在 PATH
5. `vim` 在 PATH
6. 自动安装 neovim（apt/dnf/pacman/apk/brew/pkg）

**严格模式**：`config["editor"]` 设了具体值但二进制未找到，**报错**并提示修改 config，**不**自动 fallback 到 nvim/vim。

修改 `editor` 字段：直接编辑 `~/.config/fnss-clitools/config.json`。

## 文件布局

| 用途 | 路径（Linux/Termux） |
| --- | --- |
| 配置 | `~/.config/fnss-clitools/config.json` |
| oinbox 缓存 | `~/.local/share/fnss-clitools/inbox.md` |
| oinbox pending | `~/.local/share/fnss-clitools/pending.json` |
| odiary 缓存 | `~/.local/share/fnss-clitools/diary/` |
| odiary pending | `~/.local/share/fnss-clitools/diary/pending.json` |
| onote 缓存 | `~/.local/share/fnss-clitools/notes/` |
| onote pending | `~/.local/share/fnss-clitools/notes/pending.json` |
| onote last_search | `~/.local/share/fnss-clitools/notes/last_search.json` |

macOS 对应路径：`~/Library/Application Support/fnss-clitools/`。

## 离线策略

- **写**：本地立即落盘，再尝试同步；失败则进入 pending 队列
- **读**：先尝试拉取；失败则用本地缓存并显示警告
- **同步**：每次 `list` / `add` / `sync` 都顺便把 pending 推上去（幂等：去重检查）
- **odiary 文件不存在**：友好提示「请先在 Obsidian 中创建」，**不**自动创建

## 限制

- fnss `POST /api/note` 是**全量覆盖**，不是 diff/merge。
  - 当前实现 = 「先 GET 远端 → 合并本地新增 → POST 回去」。
  - 多设备同时写会产生冲突。v1 接受此限制。
- oinbox 不支持删除/编辑任务（直接编辑 INBOX.md 即可，Obsidian 是更好的客户端）。
- odiary 仅支持追加日志；不动 `# Logs` 之外的其他 section。
- onote 不支持 baseHash 乐观并发控制；last write wins。
- onote `search` 强制在线（直接调 fnss 服务端 FTS5）。
- onote `last_search` 编号 TTL 24h；过期请重新 `search`。

## 项目结构

```
fnss-clitools/
├── pyproject.toml
├── README.md
├── scripts/
│   ├── install-termux.sh           # Termux 专用安装
│   └── install.sh                  # Linux/macOS 通用安装
├── tests/
│   ├── test_integration.py              # oinbox 集成测试
│   ├── test_odiary_integration.py       # odiary 集成测试
│   └── test_onote_integration.py        # onote 集成测试
├── clitools/                       # 共享模块
│   ├── __init__.py
│   ├── config.py                   # 共享配置
│   ├── editor.py                   # 共享编辑器选择 + 自动安装（onote/odiary 共用）
│   ├── fnss.py                     # 共享 REST 客户端（oinbox/odiary/onote 共用）
│   └── render.py                   # 共享 rich 渲染
├── oinbox/                         # oinbox 子包
│   ├── cli.py
│   ├── inbox.py
│   └── sync.py
├── odiary/                         # odiary 子包
│   ├── cli.py
│   ├── date.py
│   ├── diary.py
│   └── sync.py
└── onote/                          # onote 子包
    ├── cli.py
    ├── note.py
    ├── editor.py                   # 薄壳：调用 clitools.editor
    ├── search.py
    └── sync.py
```

## 开发

```bash
git clone ...
cd fnss-clitools
pip install -e .

# 本地跑
oinbox --help
oinbox 测试任务
odiary 测试日志
```

## 发布 checklist

发布新版本前必须**同步 bump** 所有版本号位置（否则 `update.sh` 会陷入"已是最新"死循环）：

```bash
# 1. bump 版本号（4 个文件）
#    pyproject.toml: version = "0.2.1"
#    clitools/__init__.py: __version__ = "0.2.1"
#    oinbox/__init__.py
#    odiary/__init__.py
#    onote/__init__.py
$EDITOR pyproject.toml clitools/__init__.py oinbox/__init__.py odiary/__init__.py onote/__init__.py

# 2. commit + tag
git add -A
git commit -m "Bump version to 0.2.1"
git tag v0.2.1
git push origin main v0.2.1

# 3. 创建 GitHub Release（changelog 用 --generate-notes 自动汇总）
gh release create v0.2.1 --generate-notes --title "v0.2.1"
```

`update.sh` 从 `https://api.github.com/repos/BenAngel65/fnss-clitools/releases/latest` 读取最新版本，无需上传额外 artifact。

## 许可

MIT