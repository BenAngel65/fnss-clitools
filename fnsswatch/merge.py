"""行级 union merge — 基于行集合的三方合并。

核心理念：
你的文件（INBOX.md、日记）本质是"行的集合"——每条 todo 是一行，
每条日记记录也是一行。合并不应该按"谁覆盖谁"来想，而应该按
"两个设备各自改了哪些行"来想。

合并规则：
1. base 中有的行，local 和 remote 都没删 → 保留
2. base 中有的行，local 删了、remote 没动 → 尊重删除
3. base 中有的行，remote 删了、local 没动 → 尊重删除
4. base 中有的行，双方都删了 → 删除
5. local 新增的行 → 加入
6. remote 新增的行 → 加入
7. 同一行被双方改成不同内容 → 冲突，需用户选择

删除策略（保守，不丢数据）：
- 一方删了一方改了 → 保留改后的版本（不删）
- 双方都删 → 删除
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class MergeResult:
    """合并结果。"""
    content: str
    has_conflict: bool = False
    # 冲突详情（供用户参考）
    local_added: List[str] = field(default_factory=list)
    remote_added: List[str] = field(default_factory=list)
    local_deleted: List[str] = field(default_factory=list)
    remote_deleted: List[str] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)


def _split_lines(text: str) -> List[str]:
    """分割成行，保留换行符。最后一行无换行符也保留。"""
    if not text:
        return []
    return text.splitlines(keepends=True)


def _normalize(line: str) -> str:
    """去除行尾换行符，用于比较。"""
    return line.rstrip("\r\n")


def union_merge(base: str, local: str, remote: str) -> MergeResult:
    """行级 union merge。

    把文件看作行的集合，而不是字符序列。每行独立处理。

    Returns:
        MergeResult，包含合并后内容和冲突详情。
    """
    result = MergeResult(content="")

    # 快速路径
    if base == local:
        result.content = remote
        return result
    if base == remote:
        result.content = local
        return result
    if local == remote:
        result.content = local
        return result

    base_lines = _split_lines(base)
    local_lines = _split_lines(local)
    remote_lines = _split_lines(remote)

    # 用归一化后的行做比较
    base_norm = [_normalize(l) for l in base_lines]
    local_norm = [_normalize(l) for l in local_lines]
    remote_norm = [_normalize(l) for l in remote_lines]

    base_set = set(base_norm)
    local_set = set(local_norm)
    remote_set = set(remote_norm)

    # 新增的行
    local_added_norm = local_set - base_set
    remote_added_norm = remote_set - base_set

    # 删除的行
    local_deleted_norm = base_set - local_set
    remote_deleted_norm = base_set - remote_set

    # 记录用于用户展示
    result.local_added = [l for l in local_lines if _normalize(l) in local_added_norm]
    result.remote_added = [l for l in remote_lines if _normalize(l) in remote_added_norm]
    result.local_deleted = [l for l in base_lines if _normalize(l) in local_deleted_norm]
    result.remote_deleted = [l for l in base_lines if _normalize(l) in remote_deleted_norm]

    # 检测"修改"冲突：同一行被双方改成了不同的行
    # local 把 base 的某行改成了 local_new
    # remote 把 base 的同一行改成了 remote_new
    # 表现为：base 行在 local_deleted 和 remote_deleted 中都有
    # 但 local 和 remote 各自新增了不同的行

    # 构建 base 行到 local 替换行、remote 替换行的映射
    # 用 difflib 找 base→local 和 base→remote 的替换对
    local_replacements = _find_replacements(base_norm, local_norm)
    remote_replacements = _find_replacements(base_norm, remote_norm)

    # 修改冲突：同一个 base 行被双方替换成了不同的内容
    modify_conflicts: dict[str, tuple[str, str, str]] = {}
    # base_line_norm → (base_line, local_new_line, remote_new_line)
    conflict_base_keys = local_replacements.keys() & remote_replacements.keys()
    for key in conflict_base_keys:
        local_new = local_replacements[key]
        remote_new = remote_replacements[key]
        if local_new != remote_new:
            # 同一行被改成了不同内容 → 冲突
            modify_conflicts[key] = (key, local_new, remote_new)

    # 删除-修改冲突：一方删了一方改了
    delete_modify_conflicts: dict[str, tuple[str, str, str]] = {}
    for key in local_replacements:
        if key not in remote_replacements and key in remote_deleted_norm:
            # local 改了，remote 删了 → 冲突（保留修改后的版本）
            delete_modify_conflicts[key] = (key, local_replacements[key], "")
    for key in remote_replacements:
        if key not in local_replacements and key in local_deleted_norm:
            # remote 改了，local 删了 → 冲突（保留修改后的版本）
            delete_modify_conflicts[key] = (key, "", remote_replacements[key])

    all_conflicts = {**modify_conflicts, **delete_modify_conflicts}
    if all_conflicts:
        result.has_conflict = True
        result.conflicts = [
            {
                "base": v[0],
                "local": v[1],
                "remote": v[2],
            }
            for v in all_conflicts.values()
        ]

    # 构建合并后的行列表
    # 策略：遍历 local 文件的行，决定每行是否保留，再加上 remote 独有的新增行

    # 被双方都删除的行（不在 local 也不在 remote，且不在冲突中）
    both_deleted = (base_set - local_set - remote_set) - set(all_conflicts.keys())

    # remote 独有的新增行（local 没有的）——按 remote 中的顺序排列
    remote_only_added: list[str] = []
    for i, ln in enumerate(remote_norm):
        line_with_nl = remote_lines[i]
        if ln in remote_added_norm and ln not in local_set:
            # 这是 remote 新增的且 local 没有的
            if line_with_nl not in remote_only_added:
                remote_only_added.append(line_with_nl)

    # 构建最终结果
    merged_lines: list[str] = []

    # 1. 遍历 local 行，按顺序保留
    used_remote_added = set()
    for i, ln in enumerate(local_norm):
        line = local_lines[i]
        norm = _normalize(line)

        if norm in base_set:
            # 这是 base 中原有的行
            if norm in both_deleted:
                continue  # 双方都删了
            if norm in local_deleted_norm and norm not in remote_deleted_norm:
                # local 删了但 remote 没有 → 这行不该出现在 local 里
                # 但 local 没删它？矛盾。跳过。
                continue
            # 检查是否是一方删一方改的冲突
            if norm in all_conflicts:
                # 这行被改了或被删了，冲突已在 all_conflicts 中记录
                # local 侧的处理：如果 local 改了，用 local 的新行
                conflict = all_conflicts[norm]
                if conflict[1]:  # local 新内容非空
                    # 用 local 修改后的版本（但标记为冲突）
                    merged_lines.append(line)
                # 如果 local 删了（空），不输出
                continue
            # 普通行，保留
            merged_lines.append(line)
        else:
            # 这是 local 新增的行，保留
            merged_lines.append(line)

    # 2. 在末尾追加 remote 独有的新增行
    if remote_only_added:
        # 避免重复添加已经在 merged 中的行
        merged_norms = set(_normalize(l) for l in merged_lines)
        for line in remote_only_added:
            norm = _normalize(line)
            if norm not in merged_norms and norm not in both_deleted:
                merged_lines.append(line)
                merged_norms.add(norm)

    result.content = "".join(merged_lines)
    return result


def _find_replacements(
    base: list[str], other: list[str]
) -> dict[str, str]:
    """找出 base→other 中"替换"操作。

    返回 {base_line_norm: new_line_norm}，
    表示 base 中的某行被替换成了 other 中的某行。
    只返回"1 对 1"的替换（一行换一行），忽略删除和新增。
    """
    sm = difflib.SequenceMatcher(None, base, other)
    replacements: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "replace":
            # 一行换一行才算替换；多行替换不算（太复杂）
            if (i2 - i1) == 1 and (j2 - j1) == 1:
                replacements[base[i1]] = other[j1]
    return replacements


def has_conflict_markers(content: str) -> bool:
    """检查内容是否包含冲突标记。"""
    return "<<<<<<< local" in content and ">>>>>>> remote" in content


def merge_or_conflict(
    base: Optional[str], local: str, remote: str
) -> Tuple[MergeResult, bool]:
    """合并入口，返回 (MergeResult, 是否有冲突)。

    base 为 None 时（没有 base 缓存），无法做 3-way merge，
    退化为 union：两边的内容都保留。
    """
    if base is None:
        if local == remote:
            r = MergeResult(content=local)
            return r, False
        # 没有 base，做 union：local 的行 + remote 独有的行
        local_norms = set(_normalize(l) for l in _split_lines(local))
        merged_lines = list(_split_lines(local))
        for line in _split_lines(remote):
            if _normalize(line) not in local_norms:
                merged_lines.append(line)
        r = MergeResult(content="".join(merged_lines))
        r.local_added = _split_lines(local)
        r.remote_added = [
            l for l in _split_lines(remote)
            if _normalize(l) not in local_norms
        ]
        return r, False

    result = union_merge(base, local, remote)
    return result, result.has_conflict


def resolve_with_local(base: str, local: str, remote: str) -> str:
    """冲突解决：以本地为准。"""
    return local


def resolve_with_remote(base: str, local: str, remote: str) -> str:
    """冲突解决：以远端为准。"""
    return remote


def resolve_with_union(base: str, local: str, remote: str) -> str:
    """冲突解决：合并（union，两边的行都保留）。"""
    result, _ = merge_or_conflict(base, local, remote)
    return result.content
