"""fnsswatch 同步逻辑 — 与 fnsssync 编排框架对接。

遵循项目现有模式：
- make_client() 返回 FnssClient 或 None（带缓存，避免每次重建 Session）
- push_pending(client) 返回 (pushed_count, error_list)
- manual_sync() 供 CLI 调用
pending.json 格式与 onote 一致：[{op, path, content, queued_at}]
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from clitools.config import is_configured, load_config
from clitools.fnss import FnssClient, FnssError
from clitools.render import (
    render_error,
    render_info,
    render_success,
    render_warning,
)

from . import state
from .merge import MergeResult, merge_or_conflict


# ---------------------------------------------------------------------------
# 冲突回调 — CLI 手动操作时让用户选择，daemon 模式自动 union
# ---------------------------------------------------------------------------

_conflict_callback: Optional[Callable[[str, MergeResult], str]] = None


def set_conflict_callback(cb: Callable[[str, MergeResult], str]) -> None:
    """设置冲突回调函数。

    cb(remote_path, merge_result) -> "local" | "remote" | "union"
    """
    global _conflict_callback
    _conflict_callback = cb


def clear_conflict_callback() -> None:
    """清除冲突回调（恢复自动 union 模式）。"""
    global _conflict_callback
    _conflict_callback = None


def _resolve_conflict(remote_path: str, merge_result: MergeResult,
                      base: Optional[str], local: str, remote: str) -> str:
    """处理冲突：有回调就问用户，没回调就自动 union。

    返回最终要用的内容。
    """
    if _conflict_callback is not None:
        try:
            choice = _conflict_callback(remote_path, merge_result)
        except Exception:
            choice = "union"
        if choice == "local":
            return local
        elif choice == "remote":
            return remote
        # union 或其他 → 用 merge_result.content
        return merge_result.content
    # 无回调（daemon 模式）→ 自动 union
    return merge_result.content


# ---------------------------------------------------------------------------
# client / config 缓存（避免每次文件变更都读磁盘 + 重建 Session）
# ---------------------------------------------------------------------------

_cached_client: Optional[FnssClient] = None
_cached_config: Optional[dict] = None


def make_client() -> Optional[FnssClient]:
    """返回缓存的 FnssClient（首次调用时读配置并创建）。"""
    global _cached_client
    if _cached_client is not None:
        return _cached_client
    cfg = _get_config()
    if not is_configured(cfg):
        return None
    _cached_client = FnssClient(cfg["host"], cfg["token"])
    return _cached_client


def _get_config() -> dict:
    """返回缓存的配置字典。"""
    global _cached_config
    if _cached_config is not None:
        return _cached_config
    _cached_config = load_config()
    return _cached_config


def reset_cache() -> None:
    """丢弃缓存的 client 和 config（用于配置变更后）。"""
    global _cached_client, _cached_config
    _cached_client = None
    _cached_config = None


def get_watch_dir() -> str:
    """获取配置的远端路径过滤前缀（留空表示同步 vault 内全部 .md 文件）。"""
    return _get_config().get("watch_dir", "").strip("/")


def get_local_watch_root() -> Path:
    """获取本机监控目录的根路径。

    通过 config.get_local_watch_dir() 按 hostname 获取。
    如果当前设备未配置，抛出 RuntimeError 提示用户手动设置。
    """
    from clitools.config import get_local_watch_dir
    local_dir = get_local_watch_dir().strip()
    if not local_dir:
        import socket
        raise RuntimeError(
            f"当前设备 {socket.gethostname()!r} 未配置监控目录。\n"
            f"请运行: fnsswatch config watch_local_dirs <本地路径>\n"
            f"例如: fnsswatch config watch_local_dirs ~/Note"
        )
    return Path(local_dir).expanduser().resolve()


def remote_to_local(remote_path: str) -> Path:
    """vault 相对路径 → 本地文件路径。"""
    root = get_local_watch_root()
    return (root / remote_path).resolve()


def local_to_remote(local_path: Path) -> Optional[str]:
    """本地文件路径 → vault 相对路径。"""
    root = get_local_watch_root()
    try:
        rel = local_path.resolve().relative_to(root)
        return rel.as_posix()
    except ValueError:
        return None


def read_local(remote_path: str) -> Optional[str]:
    p = remote_to_local(remote_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def write_local(remote_path: str, content: str) -> tuple[Path, str]:
    """写本地文件，自动加入防循环 ignore 集。

    Returns: (file_path, actual_content_written)
    注意：如果 content 不以 \n 结尾会补一个，返回值是实际写入磁盘的内容。
    调用方应使用返回值来计算 hash 和保存 base，确保三者一致。
    """
    p = remote_to_local(remote_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    state.add_ignore(remote_path)
    if not content.endswith("\n"):
        content += "\n"
    p.write_text(content, encoding="utf-8")
    return p, content


def delete_local(remote_path: str) -> bool:
    p = remote_to_local(remote_path)
    if p.exists():
        state.add_ignore(remote_path)
        p.unlink()
        # 清理空目录
        try:
            p.parent.rmdir()
        except OSError:
            pass
        return True
    return False


# --------------------------------------------------------------------------
# push (local → remote)
# --------------------------------------------------------------------------

def _push_with_conflict_check(
    client: FnssClient, vault: str, remote_path: str,
    local_content: str,
) -> Tuple[bool, Optional[str], bool]:
    """推送单个文件，带冲突检测。

    Returns: (success, error_msg, was_merged)
    - success=True, was_merged=False: 正常推送成功
    - success=True, was_merged=True: 冲突自动合并后推送成功
    - success=False: 推送失败（已入 pending 队列）
    """
    file_state = state.get_file_state(remote_path) or {}
    known_remote_version = file_state.get("remote_version")
    base_content = state.load_base_content(remote_path)

    # 1. 查远端当前 version
    try:
        remote_note = client.get_note(vault, remote_path)
    except FnssError as e:
        state.queue_write(remote_path, local_content)
        return False, str(e), False

    if remote_note is not None:
        remote_version = remote_note.get("version")
        remote_content = remote_note.get("content", "")
    else:
        remote_version = None
        remote_content = None

    # 2. 冲突检测
    # 场景 A：远端 version 变了 = 别的设备改过远端 → 冲突
    # 场景 B：远端有文件但本地没有 known_remote_version（首次推送但远端已有内容）
    #         → 如果本地有 base（曾经同步过但 state 丢了），仍可 merge
    #         → 如果没有 base，退化为 union merge
    # 场景 C：远端有文件、本地无 state 无 base，且本地内容与远端不同
    #         → 首次推送旧数据，需 union merge 保护远端内容
    conflict = False
    if remote_version is not None and remote_content is not None:
        if known_remote_version is not None and remote_version != known_remote_version:
            # 场景 A：远端 version 变了
            conflict = True
        elif known_remote_version is None and base_content is not None:
            # 场景 B：首次推送但远端已有内容，本地有 base（state 丢但 base 在）
            # 如果本地内容和 base 一样 = 本地没改 = 不需要 merge，直接推送就是
            # 如果本地内容和 base 不一样 = 双方都改了 = 需要 merge
            if state.compute_hash(local_content) != state.compute_hash(base_content):
                conflict = True
        elif known_remote_version is None and base_content is None:
            # 场景 C：首次推送，远端已有内容，本地无 state 无 base
            # 如果本地内容和远端不同 → union merge 保护双方内容
            if state.compute_hash(local_content) != state.compute_hash(remote_content):
                conflict = True

    content_to_push = local_content
    was_merged = False

    if conflict and remote_content is not None:
        # 双方都改了，做 3-way merge
        merge_result, has_conflict = merge_or_conflict(base_content, local_content, remote_content)
        was_merged = True
        if has_conflict:
            content_to_push = _resolve_conflict(
                remote_path, merge_result, base_content, local_content, remote_content
            )
            if _conflict_callback is not None:
                render_info(f"✓ 冲突已按用户选择解决: {remote_path}")
            else:
                render_warning(
                    f"⚠ 冲突已自动合并（union）: {remote_path}\n"
                    f"  本地新增: {merge_result.local_added}\n"
                    f"  远端新增: {merge_result.remote_added}\n"
                    f"  本地删除: {merge_result.local_deleted}\n"
                    f"  远端删除: {merge_result.remote_deleted}\n"
                    f"  冲突详情: {merge_result.conflicts}"
                )
        else:
            content_to_push = merge_result.content
            render_info(f"✓ 自动合并成功: {remote_path}")

    # 3. 推送
    try:
        resp = client.write_note(vault, remote_path, content_to_push)
    except FnssError as e:
        state.queue_write(remote_path, content_to_push)
        return False, str(e), was_merged

    # 4. 更新 state + base 缓存
    local_hash = state.compute_hash(content_to_push)
    pushed_remote_version = resp.get("version") if isinstance(resp, dict) else None
    # fnss 服务端 write_note 返回的 version 可能不可靠（异步更新），
    # push 后重新 get_note 获取真实 version，避免后续删除时误判冲突
    try:
        fresh_note = client.get_note(vault, remote_path)
        if fresh_note is not None:
            pushed_remote_version = fresh_note.get("version", pushed_remote_version)
    except FnssError:
        pass  # get_note 失败时退回 write_note 返回的 version
    mtime = datetime.now().isoformat(timespec="seconds")
    state.update_file_state(
        remote_path,
        local_mtime=mtime,
        remote_version=pushed_remote_version,
        local_hash=local_hash,
        synced_at=mtime,
    )
    state.save_base_content(remote_path, content_to_push)

    # 如果合并了，同步更新本地文件
    if was_merged:
        _path, actual_written = write_local(remote_path, content_to_push)
        # 用实际写入磁盘的内容重新计算 hash 和 base，确保三者一致
        actual_hash = state.compute_hash(actual_written)
        state.update_file_state(
            remote_path,
            local_mtime=mtime,
            local_hash=actual_hash,
            synced_at=mtime,
        )
        state.save_base_content(remote_path, actual_written)

    return True, None, was_merged


def push_single(client: FnssClient, vault: str, remote_path: str,
                content: str) -> bool:
    """推送单个文件到远端（带冲突检测）。成功返回 True。"""
    ok, err, _ = _push_with_conflict_check(client, vault, remote_path, content)
    if not ok:
        raise FnssError(err or "推送失败")
    return True


def push_single_safe(client: FnssClient, vault: str, remote_path: str,
                     content: str) -> Tuple[bool, Optional[str]]:
    """推送单个文件，失败时入 pending 队列而不抛异常。"""
    try:
        push_single(client, vault, remote_path, content)
        return True, None
    except FnssError as e:
        return False, str(e)


def _content_equal_ignoring_trailing_newline(a: str, b: str) -> bool:
    """判断两个内容是否"语义相同"：完全相同，或仅差末尾一个换行。

    背景：write_local 写本地文件与缓存 base 时，会自动补齐末尾 \\n；
    而远端内容可能不带 \\n（例如由其他设备或 echo 直接创建）。
    若用 hash 严格比较，远端无换行的内容会被误判为"远端被修改"，
    导致删除被取消、本地文件回弹（删除回弹 bug）。

    只容忍"恰好一个换行"的差异，与 full_pull 的 size 预过滤
    （local_size == remote_size or local_size == remote_size + 1）保持一致。
    不会把 "a\\n\\n" vs "a" 误判为相同。
    """
    if a == b:
        return True
    if a.endswith("\n") and a[:-1] == b:
        return True
    if b.endswith("\n") and b[:-1] == a:
        return True
    return False


def delete_single_safe(client: FnssClient, vault: str,
                       remote_path: str) -> Tuple[bool, Optional[str]]:
    """从远端删除单个文件，带冲突检测。

    冲突检测策略：比较远端内容与本地 base（上次同步时的内容）。
    如果远端内容与 base 不同，说明被其他设备修改过，不删除。
    （fnss 服务端的 version 字段是异步更新的，不可靠，
    不能用作冲突检测依据。）
    """
    file_state = state.get_file_state(remote_path) or {}

    # 查远端当前状态
    try:
        remote_note = client.get_note(vault, remote_path)
    except FnssError as e:
        state.queue_delete(remote_path)
        return False, str(e)

    if remote_note is None:
        # 远端已经没有了，直接清理 state
        state.remove_file_state(remote_path)
        state.remove_base_content(remote_path)
        return True, None

    remote_content = remote_note.get("content", "")
    remote_version = remote_note.get("version")

    # 冲突检测：比较远端内容 hash 与本地 base 内容 hash
    # 注意容忍末尾换行差异（write_local 会补 \n，远端可能不带），
    # 否则仅差一个 \n 也会被误判为"远端被修改"→ 删除回弹。
    base_content = state.load_base_content(remote_path)
    if base_content is not None:
        remote_hash = state.compute_hash(remote_content)
        if not _content_equal_ignoring_trailing_newline(remote_content,
                                                        base_content):
            # 远端被其他设备修改了，取消删除，把远端内容拉回本地
            render_warning(
                f"⚠ 删除冲突: {remote_path} 远端已被修改，保留远端版本并拉回本地"
            )
            _, actual_written = write_local(remote_path, remote_content)
            actual_hash = state.compute_hash(actual_written)
            now = datetime.now().isoformat(timespec="seconds")
            state.update_file_state(
                remote_path,
                local_mtime=now,
                remote_version=remote_version,
                local_hash=actual_hash,
                synced_at=now,
            )
            state.save_base_content(remote_path, actual_written)
            return False, "conflict_restored"

    # 远端没被修改（或无 base 记录），执行删除
    try:
        client.delete_note(vault, remote_path)
        try:
            client.recycle_clear(vault, remote_path)
        except FnssError:
            pass
        state.remove_file_state(remote_path)
        state.remove_base_content(remote_path)
        return True, None
    except FnssError as e:
        state.queue_delete(remote_path)
        return False, str(e)


def push_pending(client: FnssClient) -> Tuple[int, List[str]]:
    """推送 pending 队列中的操作。

    Returns: (pushed_count, error_list)
    """
    items = state.load_pending()
    if not items:
        return 0, []

    vault = _get_config()["vault"]

    pushed = 0
    errors: List[str] = []
    remaining: list[dict] = []

    for it in items:
        op = it.get("op")
        path = it.get("path", "")
        try:
            if op == "write":
                content = it.get("content", "")
                ok, err, _ = _push_with_conflict_check(client, vault, path, content)
                if ok:
                    pushed += 1
                else:
                    errors.append(f"{path}: {err}")
                    remaining.append(it)
            elif op == "delete":
                ok, err = delete_single_safe(client, vault, path)
                if ok:
                    pushed += 1
                else:
                    errors.append(f"{path}: {err}")
                    remaining.append(it)
            else:
                errors.append(f"{path}: unknown op {op!r}")
                continue
        except FnssError as e:
            errors.append(f"{path}: {e}")
            remaining.append(it)

    state.save_pending(remaining)
    state.flush_all()
    return pushed, errors


# --------------------------------------------------------------------------
# full push
# --------------------------------------------------------------------------

def full_push(client: FnssClient, vault: str, force: bool = False) -> Tuple[int, int]:
    """全量推送：遍历本地目录，推送有变更的文件。

    首次运行（state 为空）：
    - 先 list_notes 拿远端 path 集合
    - 远端已有的文件：跳过不推送（避免旧数据覆盖远端最新）
    - 远端没有的文件：正常推送（新文件）
    后续运行（state 有记录）：
    - hash 变了才推送
    - 首次推送远端已有文件时走 _push_with_conflict_check 冲突检测

    Returns: (pushed_count, error_count)
    """
    root = get_local_watch_root()
    if not root.exists():
        return 0, 0

    pushed = 0
    errors = 0
    saved_state = state.load_state()

    # 首次运行检测：state 为空说明从未同步过
    is_first_run = not saved_state

    # 首次运行时拉取远端 path 集合，避免重复推送已有文件
    remote_path_set: set[str] = set()
    if is_first_run or force:
        try:
            page = 1
            while True:
                result = client.list_notes(vault, page=page, page_size=100)
                for item in result["list"]:
                    remote_path_set.add(item["path"])
                pager = result.get("pager", {})
                total = pager.get("totalRows", 0)
                if page * 100 >= total:
                    break
                page += 1
        except FnssError as e:
            render_warning(f"获取远端列表失败，跳过预过滤: {e}")

    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        # 跳过 macOS AppleDouble 文件（._ 开头）和其他隐藏文件
        if any(part.startswith("._") for part in p.parts):
            continue
        remote_path = local_to_remote(p)
        if remote_path is None:
            continue
        try:
            content = p.read_text(encoding="utf-8")
            local_hash = state.compute_hash(content)

            file_state = saved_state.get(remote_path, {})

            if not force:
                # hash 没变 → 跳过
                if file_state.get("local_hash") == local_hash:
                    continue

                # 首次运行 + 远端已有 → 跳过，不推送也不记录 state
                # 后续推送时会走 _push_with_conflict_check 的首次推送路径
                # 自动做冲突检测和 merge
                if is_first_run and remote_path in remote_path_set:
                    continue
            elif remote_path in remote_path_set:
                # force 模式但也只推远端没有的？不，force 就是强制全推
                pass

            ok, err, _ = _push_with_conflict_check(client, vault, remote_path, content)
            if ok:
                pushed += 1
            else:
                errors += 1
                render_warning(f"推送失败 {remote_path}: {err}")
        except OSError as e:
            errors += 1
            render_warning(f"读取失败 {p}: {e}")

    state.flush_all()
    return pushed, errors


# --------------------------------------------------------------------------
# full pull / remote change detection
# --------------------------------------------------------------------------

def full_pull(client: FnssClient, vault: str, force: bool = False) -> Tuple[int, int]:
    """全量拉取：从远端拉取所有文件到本地。

    首次运行（state 为空）：
    - 只记录每个文件的 remote_version 到 state（local_hash 留空）
    - 不拉取文件内容，不写本地文件
    - 后续运行靠 version 增量检测 + local_hash 空值检测
    - 第二次 pull 时 local_hash 为空的文件会被拉取内容：
      - 本地无文件 → 直接写入
      - 本地有旧文件 → union merge（保留双方所有行，不丢数据）

    force 模式：忽略 version 缓存，强制拉取所有文件内容（仍跳过首次索引逻辑）。

    Returns: (pulled_count, error_count)
    """
    pulled_count = 0
    errors = 0

    # 1. 分页拉取远端列表（只含 path + version，不含 content）
    remote_items: dict[str, Any] = {}  # {path: {version, ...meta}}
    page = 1
    while True:
        try:
            result = client.list_notes(vault, page=page, page_size=100)
        except FnssError as e:
            render_error(f"获取远端列表失败: {e}")
            return pulled_count, errors + 1

        for item in result["list"]:
            remote_items[item["path"]] = item

        pager = result.get("pager", {})
        total = pager.get("totalRows", 0)
        if page * 100 >= total:
            break
        page += 1

    saved_state = state.load_state()
    watch_prefix = get_watch_dir()
    remote_path_set = set(remote_items.keys())

    # 首次运行检测：state 为空说明从未同步过
    is_first_run = not saved_state and not force

    if is_first_run:
        # 首次运行：只索引，不拉内容
        # 对每个远端文件，记录 remote_version 到 state。
        #
        # 重要：不记录 local_hash —— 因为本地文件可能是旧数据，
        # 内容不一定和远端一致。留空 local_hash 意味着：
        # - pull 第二轮：version 没变则跳过（正确，远端确实没变）
        #   但本地旧文件不会被纠正 —— 这是可接受的，因为用户可以在
        #   首次 pull 后用 `fnsswatch pull --force` 强制全量拉取覆盖
        # - push：local_hash 为空 → 任何本地文件都会被视为"有变更"→ 推送
        #   这在首次索引后是危险的（会把旧内容推上去覆盖远端最新）
        #
        # 解决方案：首次索引只记录 remote_version 和 remote_content 的 hash
        #   作为 "remote_hash"，后续 push 时对比 local_hash vs remote_hash
        #   来判断本地是否有真正变更，避免把旧文件误推上去
        indexed = 0
        for remote_path, meta in remote_items.items():
            if not remote_path.endswith(".md"):
                continue
            if watch_prefix and not remote_path.startswith(watch_prefix):
                continue

            version = meta.get("version")

            saved_state[remote_path] = {
                "remote_version": version,
                "local_hash": "",
                "synced_at": datetime.now().isoformat(timespec="seconds"),
            }
            indexed += 1

        state.save_state(saved_state)
        state.flush_all()
        render_info(
            f"首次同步索引完成：已记录 {indexed} 个远端文件版本信息（未下载内容）。\n"
            f"再次运行 `fnsswatch pull` 将按版本增量拉取变更内容。"
        )
        return 0, 0

    # 2. 后续运行：version 预过滤，size 预过滤，分批并发拉取，串行处理 merge/写入
    # 2a. 收集需要拉取的文件列表
    to_fetch: list[tuple[str, Any, int]] = []  # [(remote_path, version, remote_size)]
    skipped_by_size = 0
    for remote_path, meta in remote_items.items():
        if not remote_path.endswith(".md"):
            continue
        if watch_prefix and not remote_path.startswith(watch_prefix):
            continue

        version = meta.get("version")
        remote_size = meta.get("size", 0)

        # version 预过滤：没变就跳过，不发 get_note 请求
        # 但如果 local_hash 为空（首次索引后未拉取内容），需要拉一次
        if not force:
            file_state = saved_state.get(remote_path, {})
            if file_state.get("remote_version") == version:
                # version 没变，但如果 local_hash 为空说明首次索引没拉内容
                # 需要拉一次来同步实际文件内容到本地
                if file_state.get("local_hash"):
                    continue  # 已同步过内容，版本也没变，跳过

        # size 预过滤：如果本地有同名文件且大小一致，大概率内容相同
        # 直接用本地内容标记 hash，不发 get_note
        if not force and remote_size > 0:
            local_file = remote_to_local(remote_path)
            if local_file.exists():
                try:
                    local_size = local_file.stat().st_size
                    # 远端 size 不含末尾 \n 补齐，本地写入时会补 \n
                    # 所以 size 差 1 字节也算匹配
                    if local_size == remote_size or local_size == remote_size + 1:
                        local_content = local_file.read_text(encoding="utf-8")
                        local_hash = state.compute_hash(local_content)
                        saved_state[remote_path] = {
                            "remote_version": version,
                            "local_hash": local_hash,
                            "local_mtime": datetime.now().isoformat(timespec="seconds"),
                            "synced_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        state.save_base_content(remote_path, local_content)
                        skipped_by_size += 1
                        continue
                except OSError:
                    pass

        to_fetch.append((remote_path, version, remote_size))

    if skipped_by_size:
        render_info(f"size 预过滤跳过 {skipped_by_size} 个文件（本地大小一致，假定内容相同）")

    # 2b. 分批并发拉取远端内容（避免内存中攒太多数据）
    fetched: dict[str, tuple[str, Any]] = {}  # remote_path -> (content, version)
    if to_fetch:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import os

        max_workers = min(5, (os.cpu_count() or 2) + 1)
        batch_size = 200

        def _fetch_one(item: tuple[str, Any, int]) -> tuple[str, Any, Optional[str]]:
            """线程函数：拉取一个文件。返回 (remote_path, version, content_or_None)"""
            rp, ver, _ = item
            try:
                note = client.get_note(vault, rp)
                if note is None:
                    return rp, ver, None
                return rp, ver, note.get("content", "")
            except FnssError as e:
                render_warning(f"拉取失败 {rp}: {e}")
                return rp, ver, None

        total_batches = (len(to_fetch) + batch_size - 1) // batch_size
        for batch_idx in range(0, len(to_fetch), batch_size):
            batch = to_fetch[batch_idx:batch_idx + batch_size]
            batch_num = batch_idx // batch_size + 1
            render_info(f"并发拉取批次 {batch_num}/{total_batches}（{len(batch)} 个文件）...")

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [executor.submit(_fetch_one, item) for item in batch]
                for fut in as_completed(futures):
                    rp, ver, content = fut.result()
                    if content is not None:
                        fetched[rp] = (content, ver)

            # 每批处理完后立即处理 merge + 写入
            for remote_path, (content, version) in fetched.items():
                try:
                    # 冲突检测：本地是否有未推送的修改
                    base_content = state.load_base_content(remote_path)
                    local_content = read_local(remote_path)

                    local_modified = (
                        local_content is not None
                        and base_content is not None
                        and state.compute_hash(local_content) != state.compute_hash(base_content)
                    )

                    # 首次索引后第一次拉取：base 不存在但本地有旧文件
                    # 此时无法 3-way merge（缺 base），退化为 union merge 保留双方所有行
                    file_state = saved_state.get(remote_path, {})
                    first_index_pull = (
                        local_content is not None
                        and base_content is None
                        and not file_state.get("local_hash")
                    )

                    if local_modified or first_index_pull:
                        # 双方都改了 → 3-way merge（有 base）或 union merge（无 base）
                        merge_result, has_conflict = merge_or_conflict(
                            base_content, local_content, content
                        )
                        if has_conflict:
                            final_content = _resolve_conflict(
                                remote_path, merge_result, base_content, local_content, content
                            )
                            if _conflict_callback is not None:
                                render_info(f"✓ 拉取冲突已按用户选择解决: {remote_path}")
                            else:
                                render_warning(
                                    f"⚠ 拉取冲突已自动合并（union）: {remote_path}\n"
                                    f"  本地新增: {merge_result.local_added}\n"
                                    f"  远端新增: {merge_result.remote_added}\n"
                                    f"  本地删除: {merge_result.local_deleted}\n"
                                    f"  远端删除: {merge_result.remote_deleted}\n"
                                    f"  冲突详情: {merge_result.conflicts}"
                                )
                        else:
                            final_content = merge_result.content
                            render_info(f"✓ 拉取自动合并: {remote_path}")
                    else:
                        final_content = content

                    _, actual_written = write_local(remote_path, final_content)
                    actual_hash = state.compute_hash(actual_written)
                    state.save_base_content(remote_path, actual_written)
                    state.update_file_state(
                        remote_path,
                        local_mtime=datetime.now().isoformat(timespec="seconds"),
                        remote_version=version,
                        local_hash=actual_hash,
                    )
                    pulled_count += 1
                except FnssError as e:
                    errors += 1
                    render_warning(f"处理失败 {remote_path}: {e}")

            # 每批处理后 flush 一次 state（避免内存中攒太多数据）
            state.save_state(saved_state)
            state.flush_all()
            fetched.clear()
            render_info(f"批次 {batch_num}/{total_batches} 完成，累计拉取 {pulled_count} 个文件")

    # 检测远端没有但本地有的文件（远端已删除同步）
    if force:
        local_root = get_local_watch_root()
        if local_root.exists():
            for p in sorted(local_root.rglob("*.md")):
                if not p.is_file():
                    continue
                if any(part.startswith("._") for part in p.parts):
                    continue
                rp = local_to_remote(p)
                if rp and rp not in remote_path_set:
                    # 只删除曾经同步过的文件（有 base 缓存说明曾经同步过）
                    # 纯本地新建、从未同步的文件不删
                    if state.load_base_content(rp) is not None:
                        state.add_ignore(rp)
                        p.unlink()
                        state.remove_base_content(rp)
                        state.remove_file_state(rp)
                    else:
                        render_info(f"跳过本地独有文件（未同步过）: {rp}")

    state.flush_all()
    return pulled_count, errors


def poll_remote_changes(client: FnssClient, vault: str) -> Tuple[int, int]:
    """轮询远端变更：只拉第一页（最新 100 条），按 version 增量检测。

    fnss 服务端按 updatedAt 降序返回，最近变更的文件一定在第一页。
    - state 中有记录且 version 相同 → 跳过
    - state 中有记录但 version 不同 → 拉取内容
    - state 中无记录 → 只记录 version（不拉内容，等 full_pull 手动初始化）

    用于守护进程的轮询线程。
    Returns: (pulled_count, error_count)
    """
    try:
        result = client.list_notes(vault, page=1, page_size=100)
    except FnssError:
        return 0, 1

    saved_state = state.load_state()
    watch_prefix = get_watch_dir()
    pulled = 0
    errors = 0

    for item in result["list"]:
        remote_path = item.get("path", "")
        if not remote_path.endswith(".md"):
            continue
        if watch_prefix and not remote_path.startswith(watch_prefix):
            continue

        remote_version = item.get("version")
        file_state = saved_state.get(remote_path)

        if file_state is None:
            # 首次见到这个文件：只记录 version，不拉内容
            # （全量初始化应由 fnsswatch pull 完成）
            state.update_file_state(
                remote_path,
                local_mtime=datetime.now().isoformat(timespec="seconds"),
                remote_version=remote_version,
                local_hash="",
            )
            continue

        if file_state.get("remote_version") == remote_version:
            continue  # 版本没变

        # version 变了，拉取内容
        try:
            note = client.get_note(vault, remote_path)
            if note is None:
                continue
            remote_content = note.get("content", "")

            # 冲突检测：本地是否有未推送的修改
            base_content = state.load_base_content(remote_path)
            local_content = read_local(remote_path)
            local_hash_on_disk = state.compute_hash(local_content) if local_content is not None else ""

            # 本地文件存在且与上次同步时不同 = 本地有未推送修改
            local_modified = (
                local_content is not None
                and base_content is not None
                and state.compute_hash(local_content) != state.compute_hash(base_content)
            )
            # 也可能是本地改了但没有 base 缓存（首次同步后改的）
            local_modified_no_base = (
                local_content is not None
                and base_content is None
                and file_state.get("local_hash") != local_hash_on_disk
            )

            if local_modified or local_modified_no_base:
                # 双方都改了 → 3-way merge
                merge_result, has_conflict = merge_or_conflict(
                    base_content, local_content, remote_content
                )
                if has_conflict:
                    final_content = _resolve_conflict(
                        remote_path, merge_result, base_content, local_content, remote_content
                    )
                    if _conflict_callback is not None:
                        render_info(f"✓ 轮询冲突已按用户选择解决: {remote_path}")
                    else:
                        render_warning(
                            f"⚠ 轮询冲突已自动合并（union）: {remote_path}\n"
                            f"  本地新增: {merge_result.local_added}\n"
                            f"  远端新增: {merge_result.remote_added}\n"
                            f"  本地删除: {merge_result.local_deleted}\n"
                            f"  远端删除: {merge_result.remote_deleted}\n"
                            f"  冲突详情: {merge_result.conflicts}"
                        )
                else:
                    final_content = merge_result.content
                    render_info(f"✓ 轮询自动合并: {remote_path}")

                _, actual_written = write_local(remote_path, final_content)
                actual_hash = state.compute_hash(actual_written)
                now = datetime.now().isoformat(timespec="seconds")

                # 合并后需要推送到远端
                push_ok = True
                try:
                    resp = client.write_note(vault, remote_path, final_content)
                    remote_version = resp.get("version") if isinstance(resp, dict) else remote_version
                except FnssError:
                    push_ok = False  # 推送失败，下次再试

                # 只有推送成功才更新 base 为合并后内容
                # 推送失败时 base 保留旧值，下次 poll 会重新检测冲突并重试
                if push_ok:
                    state.save_base_content(remote_path, actual_written)
            else:
                # 本地没改，直接用远端覆盖
                _, actual_written = write_local(remote_path, remote_content)
                actual_hash = state.compute_hash(actual_written)
                state.save_base_content(remote_path, actual_written)
                now = datetime.now().isoformat(timespec="seconds")

            state.update_file_state(
                remote_path,
                local_mtime=now,
                remote_version=remote_version,
                local_hash=actual_hash,
            )
            pulled += 1
        except FnssError:
            errors += 1

    # 不调用 save_state 全量覆盖，改用 update_file_state 逐条增量更新
    # 避免 poll 线程的旧快照覆盖 push 线程在此期间写入的 state
    state.flush_all()
    return pulled, errors


# --------------------------------------------------------------------------
# manual sync
# --------------------------------------------------------------------------

def manual_sync() -> int:
    """手动同步：推送 pending + 全量推送本地变更。"""
    if not is_configured():
        render_error("未配置 fnss 凭证，运行 `onote config` 设置")
        return 1

    client = make_client()
    if client is None:
        render_error("无法创建客户端")
        return 1

    vault = _get_config()["vault"]

    # 1. 排空 pending 队列
    pushed, errs = push_pending(client)
    for e in errs:
        render_warning(f"推送失败: {e}")
    if pushed:
        render_success(f"已推送 {pushed} 条待同步项")

    # 2. 全量推送本地变更
    local_pushed, local_errors = full_push(client, vault, force=False)
    if local_pushed:
        render_success(f"已推送 {local_pushed} 个本地变更文件")
    if local_errors:
        render_warning(f"{local_errors} 个文件推送失败")

    state.flush_all()
    return 0 if not errs and local_errors == 0 else 3
