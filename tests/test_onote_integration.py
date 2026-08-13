"""Integration tests for onote using a mock fnss server.

Verifies the offline-first CRUD flow without a live fnss instance.
nvim invocation is monkey-patched to skip the editor and write content
directly to the local cache file (simulating user editing in vim).
"""
import json
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clitools import config as cfg_mod  # noqa: E402
from onote import cli as onote_cli  # noqa: E402
from onote import editor as onote_editor  # noqa: E402
from onote import note as note_ops  # noqa: E402
from onote import sync as onote_sync  # noqa: E402

TOKEN = "test-token-xyz"
VAULT = "testVault"
HOST = "http://127.0.0.1:18767"


class MockState:
    def __init__(self):
        # path -> content
        self.files: dict[str, str] = {}
        self.fail_get = False
        self.fail_post = False
        self.fail_delete = False
        self.recycle_bin: set[str] = set()
        # codes to return for the *next* matching op, to test success code handling
        self.next_write_code: int = 1
        self.next_delete_code: int = 4

    def seed(self, path: str, content: str = "# Seed\n\n") -> None:
        self.files[path] = content


def make_handler(state: MockState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Token") != TOKEN:
                self._send({"code": 308, "message": "bad token"}, 401)
                return
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            if self.path.startswith("/api/notes"):
                if state.fail_get:
                    self._send({"code": 500, "message": "mock fail"})
                    return
                keyword = qs.get("keyword", [""])[0]
                mode = qs.get("searchMode", ["path"])[0]
                # Mimic fnss SQLite FTS5: split keyword into tokens, all must match (AND)
                tokens = keyword.split() if keyword else []
                items = []
                now_ms = int(datetime.now().timestamp() * 1000)
                for p, c in state.files.items():
                    hit = False
                    if not tokens:
                        hit = True
                    elif mode == "content":
                        hit = all(tok in c for tok in tokens)
                    else:
                        hit = all(tok in p for tok in tokens)
                    if hit:
                        items.append({
                            "path": p,
                            "pathHash": "h",
                            "version": 1,
                            "ctime": now_ms,
                            "mtime": now_ms,
                            "size": len(c.encode()),
                        })
                self._send({
                    "code": 1,
                    "data": {
                        "list": items,
                        "pager": {"page": 1, "pageSize": 100, "totalRows": len(items)},
                    },
                })
            elif self.path.startswith("/api/note"):
                if state.fail_get:
                    self._send({"code": 500, "message": "mock fail"})
                    return
                path = qs.get("path", [""])[0]
                if path in state.files:
                    self._send({
                        "code": 1,
                        "data": {"content": state.files[path], "version": 1, "path": path},
                    })
                else:
                    self._send({"code": 430, "message": "note not found"})
            else:
                self._send({"code": 404, "message": "not found"}, 404)

        def do_POST(self):
            if self.headers.get("Token") != TOKEN:
                self._send({"code": 308, "message": "bad token"}, 401)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            if state.fail_post:
                self._send({"code": 500, "message": "mock fail"})
                return
            if self.path.startswith("/api/note"):
                path = body.get("path", "")
                code = state.next_write_code
                state.files[path] = body.get("content", "")
                self._send({"code": code, "data": {"version": 2}})
                state.next_write_code = 1
            else:
                self._send({"code": 444, "message": "bad path"})

        def do_DELETE(self):
            if self.headers.get("Token") != TOKEN:
                self._send({"code": 308, "message": "bad token"}, 401)
                return
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            if state.fail_delete:
                self._send({"code": 500, "message": "mock fail"})
                return
            path = qs.get("path", [""])[0]
            if self.path.startswith("/api/note/recycle-clear"):
                # Hard delete from recycle bin
                state.files.pop(path, None)
                state.recycle_bin.discard(path)
                self._send({"code": 4, "data": {}})
            elif self.path.startswith("/api/note"):
                # Soft delete
                code = state.next_delete_code
                if path in state.files:
                    state.files.pop(path)
                state.recycle_bin.add(path)
                self._send({"code": code, "data": {}})
                state.next_delete_code = 4
            else:
                self._send({"code": 444, "message": "bad path"}, 404)

        def do_PUT(self):
            if self.headers.get("Token") != TOKEN:
                self._send({"code": 308, "message": "bad token"}, 401)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            # /api/note/restore
            self._send({"code": 1, "data": {}})

    return Handler


def run_server(state: MockState):
    server = HTTPServer(("127.0.0.1", 18767), make_handler(state))
    server.serve_forever()


def setup_config(tmp: Path):
    cfg_mod.config_path.__globals__["user_config_dir"] = lambda *a, **k: str(tmp / "config")
    cfg_mod.data_dir.__globals__["user_data_dir"] = lambda *a, **k: str(tmp / "data")
    cfg = cfg_mod.load_config()
    cfg["host"] = HOST
    cfg["token"] = TOKEN
    cfg["vault"] = VAULT
    cfg["notes_dir"] = "Inbox"
    cfg_mod.save_config(cfg)


def reset(tmp: Path, state: MockState):
    state.files.clear()
    state.recycle_bin.clear()
    state.fail_get = False
    state.fail_post = False
    state.fail_delete = False
    state.next_write_code = 1
    state.next_delete_code = 4
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    pending = note_ops.notes_data_dir() / "pending.json"
    if pending.exists():
        pending.unlink()
    last = note_ops.last_search_path()
    if last.exists():
        last.unlink()
    setup_config(tmp)


def patch_editor(content_to_write: str):
    """Skip real nvim: directly overwrite the local file with given content."""
    def fake_edit(file_path: Path):
        # Simulate user editing the file
        file_path.write_text(content_to_write, encoding="utf-8")
        return (0, "fake-nvim")
    return fake_edit


def patch_input(responses: list):
    """Patch builtins.input to return successive responses."""
    import builtins
    it = iter(responses)
    orig = builtins.input
    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    builtins.input = fake_input
    return lambda: setattr(builtins, "input", orig)


def main():
    import tempfile

    state = MockState()
    server_thread = threading.Thread(target=run_server, args=(state,), daemon=True)
    server_thread.start()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # === Scenario 1: strict create + write ===
        reset(tmp, state)
        target = "Inbox/topic1.md"
        new_body = "# topic1\n\nBody 1\n"
        onote_editor.edit_with_fallback = patch_editor(new_body)
        rc = onote_cli.main(["topic1"])
        assert rc == 0, f"create should succeed, got {rc}"
        assert target in state.files, f"server missing {target}"
        assert state.files[target] == new_body
        print(f"✓ 场景 1: strict create + POST 成功")

        # === Scenario 2: bare title 已存在 → 报错 ===
        reset(tmp, state)
        state.seed("Inbox/topic2.md", "existing")
        onote_editor.edit_with_fallback = patch_editor("edited body")
        rc = onote_cli.main(["topic2"])
        assert rc == 1, f"duplicate should fail, got {rc}"
        assert state.files["Inbox/topic2.md"] == "existing"
        print(f"✓ 场景 2: bare title 已存在 → 报错")

        # === Scenario 3: online edit (path) ===
        reset(tmp, state)
        state.seed("Inbox/topic3.md", "old body")
        new_body3 = "old body\n\nappended line\n"
        onote_editor.edit_with_fallback = patch_editor(new_body3)
        rc = onote_cli.main(["edit", "Inbox/topic3.md"])
        assert rc == 0
        assert state.files["Inbox/topic3.md"] == new_body3
        print(f"✓ 场景 3: online edit 推送成功")

        # === Scenario 4: offline edit → pending → sync ===
        reset(tmp, state)
        state.seed("Inbox/topic4.md", "before")
        state.fail_post = True
        edited = "before\n\noffline edit\n"
        onote_editor.edit_with_fallback = patch_editor(edited)
        rc = onote_cli.main(["edit", "Inbox/topic4.md"])
        assert rc == 0, "offline edit should not fail"
        assert state.files["Inbox/topic4.md"] == "before", "server should not be updated yet"
        pending = json.loads((note_ops.notes_data_dir() / "pending.json").read_text())
        assert len(pending) == 1 and pending[0]["op"] == "write"
        assert pending[0]["content"] == edited
        # Recover network → sync
        state.fail_post = False
        rc = onote_cli.main(["sync"])
        assert rc == 0
        assert state.files["Inbox/topic4.md"] == edited
        assert not (note_ops.notes_data_dir() / "pending.json").exists()
        print(f"✓ 场景 4: offline edit 入 pending，恢复后 sync 推送")

        # === Scenario 5: search path mode ===
        reset(tmp, state)
        state.seed("Inbox/alpha.md", "alpha body")
        state.seed("Inbox/beta.md", "beta body")
        state.seed("Other/gamma.md", "gamma body")
        rc = onote_cli.main(["search", "alpha"])
        assert rc == 0
        ls = note_ops.read_last_search()
        assert ls is not None and "alpha" in ls["results"][0]
        assert len(ls["results"]) == 1
        print(f"✓ 场景 5: path 搜索 → 1 条结果，写入 last_search")

        # === Scenario 6: search content mode ===
        reset(tmp, state)
        state.seed("Inbox/notes1.md", "我们今天讨论了 Python")
        state.seed("Inbox/notes2.md", "今天天气不错")
        rc = onote_cli.main(["search", "-c", "Python"])
        assert rc == 0
        ls = note_ops.read_last_search()
        assert len(ls["results"]) == 1 and ls["mode"] == "content"
        print(f"✓ 场景 6: content 搜索命中并标记 mode=content")

        # === Scenario 7: edit/open/delete via number ===
        reset(tmp, state)
        state.seed("Inbox/seven.md", "original seven")
        onote_cli.main(["search", "seven"])
        onote_editor.edit_with_fallback = patch_editor("edited seven via number")
        rc = onote_cli.main(["edit", "1"])
        assert rc == 0
        assert state.files["Inbox/seven.md"] == "edited seven via number"
        # open
        rc = onote_cli.main(["open", "1"])
        assert rc == 0
        print(f"✓ 场景 7: edit/open 用编号引用")

        # === Scenario 8: delete via path with --yes ===
        reset(tmp, state)
        state.seed("Inbox/eight.md", "to be deleted")
        rc = onote_cli.main(["delete", "Inbox/eight.md", "--yes"])
        assert rc == 0
        assert "Inbox/eight.md" not in state.files
        assert "Inbox/eight.md" not in state.recycle_bin
        print(f"✓ 场景 8: delete 路径 + --yes")

        # === Scenario 9: delete offline → pending → sync hard-deletes ===
        reset(tmp, state)
        state.seed("Inbox/nine.md", "delete offline")
        state.fail_delete = True
        rc = onote_cli.main(["delete", "Inbox/nine.md", "--yes"])
        assert rc == 0
        pending = json.loads((note_ops.notes_data_dir() / "pending.json").read_text())
        assert len(pending) == 1 and pending[0]["op"] == "delete"
        state.fail_delete = False
        rc = onote_cli.main(["sync"])
        assert rc == 0
        assert "Inbox/nine.md" not in state.files
        print(f"✓ 场景 9: delete offline → pending → sync 硬删")

        # === Scenario 10: last_search TTL 过期 ===
        reset(tmp, state)
        state.seed("Inbox/ten.md", "x")
        onote_cli.main(["search", "ten"])
        # Force expire by backdating last_search.json
        ls_path = note_ops.last_search_path()
        data = json.loads(ls_path.read_text())
        old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
        data["ts"] = old
        ls_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        # Now edit 1 should fail (no valid last_search)
        onote_editor.edit_with_fallback = patch_editor("edited ten")
        rc = onote_cli.main(["edit", "1"])
        assert rc == 1, "expired last_search should fail edit"
        print(f"✓ 场景 10: last_search TTL 过期 → 编号引用失效")

        # === Scenario 11: edit a note that doesn't exist anywhere ===
        reset(tmp, state)
        onote_editor.edit_with_fallback = patch_editor("fresh content")
        rc = onote_cli.main(["edit", "Inbox/fresh.md"])
        assert rc == 0
        assert "Inbox/fresh.md" in state.files
        assert state.files["Inbox/fresh.md"].strip() == "fresh content"
        print(f"✓ 场景 11: edit 不存在的远端笔记 → 当 create 处理")

        # === Scenario 12: open offline + local cache ===
        reset(tmp, state)
        cached = "# cached\n\nlocal copy"
        note_ops.write_local("Inbox/cached.md", cached)
        rc = onote_cli.main(["open", "Inbox/cached.md"])
        assert rc == 0
        print(f"✓ 场景 12: open 显示本地缓存")

        # === Scenario 13: fnss 4 种成功码 (1/2/3/4/6) 全部正确处理 ===
        for code in (1, 2, 3, 4, 6):
            reset(tmp, state)
            state.seed("Inbox/codes.md", "old")
            state.next_write_code = code
            onote_editor.edit_with_fallback = patch_editor(f"new for code {code}")
            rc = onote_cli.main(["edit", "Inbox/codes.md"])
            assert rc == 0, f"code={code} should be accepted"
            assert state.files["Inbox/codes.md"] == f"new for code {code}"
        print(f"✓ 场景 13: fnss 成功码 1/2/3/4/6 全部正确接受")

        # === Scenario 14: bare title normalization (subdir path) ===
        reset(tmp, state)
        onote_editor.edit_with_fallback = patch_editor("# subdir body")
        rc = onote_cli.main(["subdir/topic"])  # bare "subdir/topic" → "subdir/topic.md"
        assert rc == 0
        assert "subdir/topic.md" in state.files
        assert state.files["subdir/topic.md"].strip() == "# subdir body"
        print(f"✓ 场景 14: bare 'subdir/topic' → subdir/topic.md (子目录无前缀)")

        # === Scenario 15: delete with confirmation prompt ===
        reset(tmp, state)
        state.seed("Inbox/fifteen.md", "x")
        restore = patch_input(["n"])
        rc = onote_cli.main(["delete", "Inbox/fifteen.md"])
        restore()
        assert rc == 0
        assert "Inbox/fifteen.md" in state.files
        restore = patch_input(["y"])
        rc = onote_cli.main(["delete", "Inbox/fifteen.md"])
        restore()
        assert rc == 0
        assert "Inbox/fifteen.md" not in state.files
        print(f"✓ 场景 15: delete 二次确认 (n 取消 / y 删除)")

        # === Scenario 16: open via number ===
        reset(tmp, state)
        state.seed("Inbox/sixteen.md", "# sixteen body\n\nmore")
        onote_cli.main(["search", "sixteen"])
        rc = onote_cli.main(["open", "1"])
        assert rc == 0
        print(f"✓ 场景 16: open 用编号")

        # === Scenario 17: create new + :q empty → cleanup, no push ===
        reset(tmp, state)
        target = "Inbox/seventeen.md"
        onote_editor.edit_with_fallback = patch_editor("")  # user :q without typing
        rc = onote_cli.main(["seventeen"])
        assert rc == 0
        assert target not in state.files, "should not push empty content"
        local_path = note_ops.local_note_path(target)
        assert not local_path.exists(), f"local empty file should be cleaned: {local_path}"
        assert not (note_ops.notes_data_dir() / "pending.json").exists()
        print(f"✓ 场景 17: create 空内容 → 清理本地 + 不推送")

        # === Scenario 18: edit non-existent + :q empty → cleanup ===
        reset(tmp, state)
        target = "Inbox/eighteen.md"
        onote_editor.edit_with_fallback = patch_editor("")
        rc = onote_cli.main(["edit", target])
        assert rc == 0
        assert target not in state.files, "should not push empty content"
        local_path = note_ops.local_note_path(target)
        assert not local_path.exists(), f"local empty file should be cleaned: {local_path}"
        print(f"✓ 场景 18: edit 远端不存在 + 空内容 → 清理本地 + 不推送")

        # === Scenario 19: edit existing + user empties it → cleanup local, server untouched ===
        reset(tmp, state)
        target = "Inbox/nineteen.md"
        original = "real content here"
        state.seed(target, original)
        onote_editor.edit_with_fallback = patch_editor("   \n\n  ")  # whitespace only
        rc = onote_cli.main(["edit", target])
        assert rc == 0
        assert state.files.get(target) == original, "server should keep original content"
        local_path = note_ops.local_note_path(target)
        assert not local_path.exists(), f"local emptied file should be cleaned: {local_path}"
        print(f"✓ 场景 19: edit 已存在 + 用户清空内容 → 清理本地，服务端保留")

        # === Scenario 20: orphan local file (pending lost) → next sync recovers ===
        reset(tmp, state)
        orphan_path = "Inbox/orphan.md"
        orphan_content = "# orphan\n\ncreated offline, pending was lost\n"
        # Simulate orphan: write local cache, no pending.json entry
        note_ops.write_local(orphan_path, orphan_content)
        # Server doesn't have it
        assert orphan_path not in state.files
        # Next sync should detect and push
        rc = onote_cli.main(["sync"])
        assert rc == 0
        assert state.files.get(orphan_path) == orphan_content, "orphan should be recovered"
        print(f"✓ 场景 20: 本地孤儿文件 → sync 自动恢复")

        # === Scenario 21: cache from server (search/open) → reconcile sees equal, skips ===
        reset(tmp, state)
        server_path = "Inbox/server-cached.md"
        server_content = "downloaded from server\n"
        state.seed(server_path, server_content)
        # Simulate a cache download: write_local with the server content (matches)
        note_ops.write_local(server_path, server_content)
        # Create an unrelated new note (this triggers reconcile)
        onote_editor.edit_with_fallback = patch_editor("# unrelated new\n")
        rc = onote_cli.main(["unrelated-new"])
        assert rc == 0
        # The cached file should NOT have been re-pushed (content unchanged on server)
        assert state.files[server_path] == server_content
        # And the new unrelated note should have been pushed
        assert "Inbox/unrelated-new.md" in state.files
        print(f"✓ 场景 21: 与服务端一致的本地缓存 → reconcile 跳过不重传")

        # === Scenario 22: diverged local file (offline edit, pending lost) → next op pushes ===
        reset(tmp, state)
        target = "Inbox/diverged.md"
        server_content = "old server content\n"
        state.seed(target, server_content)
        # Simulate offline edit: local cache has different content, no pending
        new_local = "new offline edit content\n"
        note_ops.write_local(target, new_local)
        # Trigger any operation that calls reconcile — sync is simplest
        rc = onote_cli.main(["sync"])
        assert rc == 0
        assert state.files.get(target) == new_local, "diverged local should win (last write)"
        print(f"✓ 场景 22: 本地与服务端分歧 → sync 自动推送本地版本")

        # === Scenario 23: reconcile triggered by create_new (not just sync) ===
        reset(tmp, state)
        orphan_path = "Inbox/recover-on-create.md"
        orphan_content = "# orphan\n"
        note_ops.write_local(orphan_path, orphan_content)
        assert orphan_path not in state.files
        onote_editor.edit_with_fallback = patch_editor("# brand new\n")
        rc = onote_cli.main(["brand-new"])
        assert rc == 0
        # Both should be on the server
        assert state.files.get(orphan_path) == orphan_content, "orphan recovered before create"
        assert "Inbox/brand-new.md" in state.files
        print(f"✓ 场景 23: create_new 自动恢复孤儿，无需先手动 sync")

        # === Scenario 24: multi-word search ===
        reset(tmp, state)
        state.seed("Inbox/foo-bar.md", "no match here")
        state.seed("Inbox/食堂-提示词.md", "matches both keywords")
        rc = onote_cli.main(["search", "食堂", "提示词"])
        assert rc == 0
        ls = note_ops.read_last_search()
        assert ls is not None
        assert ls["query"] == "食堂 提示词"
        assert any("食堂" in p for p in ls["results"]), f"results should include multi-keyword match: {ls['results']}"
        print(f"✓ 场景 24: 多关键词 search → 自动 join 成 '食堂 提示词'")

        print()
        print("=" * 60)
        print("✅ onote 所有集成测试通过！")
        print("=" * 60)


if __name__ == "__main__":
    main()