"""Note path resolution, content I/O, last_search caching.

Paths here are vault-relative, using forward slashes (e.g. "Inbox/topic.md").
Local cache mirrors the vault tree under data_dir()/notes/.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from clitools.config import data_dir, load_config

DEFAULT_NOTES_DIR = "Inbox"
NOTES_SUBDIR = "notes"
LAST_SEARCH_TTL_SECONDS = 24 * 3600


def notes_data_dir() -> Path:
    """Root for onote local cache."""
    return data_dir() / NOTES_SUBDIR


def local_note_path(remote_path: str) -> Path:
    """Map vault-relative path to local cache file."""
    return (notes_data_dir() / remote_path).resolve()


def read_local(remote_path: str) -> Optional[str]:
    p = local_note_path(remote_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def write_local(remote_path: str, content: str) -> Path:
    p = local_note_path(remote_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    p.write_text(content, encoding="utf-8")
    return p


def delete_local(remote_path: str) -> bool:
    """Remove local cache; return True if a file was removed."""
    p = local_note_path(remote_path)
    if p.exists():
        p.unlink()
        return True
    return False


def scan_notes_dir() -> list[str]:
    """Walk the configured notes_dir under local cache; return vault-relative paths.

    Only files inside ``notes_data_dir()/<notes_dir>/`` are scanned (e.g. Inbox/).
    Other subdirs of notes_data_dir/ (e.g. Archiver/ from search/open caching) are
    ignored — they are server-originated and not "user-authored" orphans.

    Returns sorted list of vault-relative paths (forward slashes).
    """
    cfg = load_config()
    base_name = cfg.get("notes_dir", DEFAULT_NOTES_DIR).strip("/")
    if not base_name:
        root = notes_data_dir()
    else:
        root = notes_data_dir() / base_name
    if not root.exists():
        return []
    paths: list[str] = []
    for p in sorted(root.rglob("*.md")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(notes_data_dir()).as_posix()
        except ValueError:
            continue
        paths.append(rel)
    return paths


# ---------- path normalization ----------

def normalize_path(ref: str) -> str:
    """Resolve bare title or partial path to vault-relative path.

    - 'topic.md'   -> 'topic.md'        (already qualified)
    - 'a/b'        -> 'a/b.md'          (subdir + bare stem)
    - 'topic'      -> 'Inbox/topic.md'  (bare title → notes_dir)
    - '/a/b'       -> 'a/b'             (strip leading slash)
    """
    ref = ref.strip().lstrip("/")
    if not ref:
        raise ValueError("路径不能为空")
    if ref.endswith(".md"):
        return ref
    if "/" in ref:
        return f"{ref}.md"
    cfg = load_config()
    base = cfg.get("notes_dir", DEFAULT_NOTES_DIR).strip("/")
    return f"{base}/{ref}.md"


def resolve_ref(ref: str) -> str:
    """Resolve ref (number from last_search or path string) to vault-relative path."""
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("引用不能为空")
    if ref.isdigit():
        ls = read_last_search()
        if not ls:
            raise ValueError(f"编号 {ref} 无效（没有可用的 last_search 缓存，请先 onote search）")
        idx = int(ref) - 1
        results = ls.get("results", [])
        if idx < 0 or idx >= len(results):
            raise ValueError(f"编号 {ref} 超出范围 1..{len(results)}")
        return results[idx]
    return normalize_path(ref)


# ---------- last_search.json ----------

def last_search_path() -> Path:
    return notes_data_dir() / "last_search.json"


def read_last_search() -> Optional[dict]:
    p = last_search_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    ts_str = data.get("ts", "")
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return None
    age = (datetime.now() - ts).total_seconds()
    if age > LAST_SEARCH_TTL_SECONDS:
        return None
    return data


def save_last_search(query: str, mode: str, results: list[str]) -> None:
    p = last_search_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "query": query,
        "mode": mode,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def clear_last_search() -> None:
    p = last_search_path()
    if p.exists():
        p.unlink()