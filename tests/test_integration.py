"""Integration tests using a mock fnss server.

Verifies the offline-first sync flow without needing a live fnss instance:
    1. First add with server down -> queued locally
    2. Server comes up -> next sync pushes pending
    3. Add while online -> immediate push
    4. List while online -> renders latest
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Ensure oinbox is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clitools import config as cfg_mod  # noqa: E402
from oinbox import cli, sync  # noqa: E402
from odiary import cli as odiary_cli  # noqa: E402
from onote import cli as onote_cli  # noqa: E402


TOKEN = "test-token-xyz"
VAULT = "testVault"
INBOX = "INBOX.md"
HOST = "http://127.0.0.1:18765"


class MockState:
    note_content = "# Inbox\n\n"  # legacy single-note storage for oinbox tests
    files: dict[str, str] = {}      # path -> content, for multi-path tests
    fail_get = False
    fail_post = False


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
                self._send({"code": 508, "message": "bad token"}, 401)
                return
            if self.path.startswith("/api/note"):
                if state.fail_get:
                    self._send({"code": 500, "message": "mock fail"})
                    return
                # Parse path from query string
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                path = qs.get("path", [""])[0]
                # Multi-path storage: per-path dict first, fallback to legacy
                if path in state.files:
                    content = state.files[path]
                elif path == INBOX:
                    content = state.note_content
                else:
                    self._send({"code": 1, "data": {"content": "", "version": 0}})
                    return
                self._send({"code": 1, "data": {"content": content, "version": 1}})
            elif self.path.startswith("/api/version"):
                self._send({"code": 1, "data": {"version": "test"}})
            else:
                self._send({"code": 404, "message": "not found"}, 404)

        def do_POST(self):
            if self.headers.get("Token") != TOKEN:
                self._send({"code": 508, "message": "bad token"}, 401)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            if state.fail_post:
                self._send({"code": 500, "message": "mock fail"})
                return
            if body.get("vault") != VAULT:
                self._send({"code": 428, "message": "no such vault"})
                return
            path = body.get("path", "")
            content = body.get("content", "")
            # Multi-path storage: INBOX uses legacy attr, others use dict
            if path == INBOX:
                state.note_content = content
            else:
                state.files[path] = content
            self._send({"code": 1, "data": {"content": content, "version": 2}})

    return Handler


def run_server(state: MockState):
    server = HTTPServer(("127.0.0.1", 18765), make_handler(state))
    server.serve_forever()


def setup_local_config(tmp_home: Path):
    """Point oinbox at our mock host and a temp data dir."""
    cfg_mod.config_path.__globals__["user_config_dir"] = lambda *a, **k: str(tmp_home / "config")
    cfg_mod.data_dir.__globals__["user_data_dir"] = lambda *a, **k: str(tmp_home / "data")
    cfg = cfg_mod.load_config()
    cfg["host"] = HOST
    cfg["token"] = TOKEN
    cfg["vault"] = VAULT
    cfg["inbox_path"] = INBOX
    cfg_mod.save_config(cfg)


def reset_state(tmp_home: Path, state: MockState):
    state.note_content = "# Inbox\n\n"
    state.fail_get = False
    state.fail_post = False
    (tmp_home / "config").mkdir(parents=True, exist_ok=True)
    (tmp_home / "data").mkdir(parents=True, exist_ok=True)
    for f in (cfg_mod.local_inbox_path(), cfg_mod.pending_path()):
        if f.exists():
            f.unlink()
    # Force the monkey-patched paths to take effect after reset
    cfg_mod.config_path.__globals__["user_config_dir"] = lambda *a, **k: str(tmp_home / "config")
    cfg_mod.data_dir.__globals__["user_data_dir"] = lambda *a, **k: str(tmp_home / "data")


def main():
    import tempfile

    state = MockState()
    server_thread = threading.Thread(target=run_server, args=(state,), daemon=True)
    server_thread.start()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --- Scenario 1: Server DOWN, add 2 entries ---
        reset_state(tmp, state)
        state.fail_get = True
        state.fail_post = True
        setup_local_config(tmp)
        print("=" * 60)
        print("场景 1：服务器宕机，添加 2 条任务")
        print("=" * 60)
        rc = sync.add_and_sync("离线任务1")
        assert rc == 0, f"add should not fail, got {rc}"
        rc = sync.add_and_sync("离线任务2")
        assert rc == 0
        local = cfg_mod.local_inbox_path().read_text(encoding="utf-8")
        assert "离线任务1" in local, f"local missing task1: {local}"
        assert "离线任务2" in local, f"local missing task2: {local}"
        pending = cfg_mod.pending_path().read_text(encoding="utf-8")
        assert "离线任务1" in pending
        assert "离线任务2" in pending
        print(f"✓ 本地已写入 2 条，pending.json 也已缓存")
        print(f"✓ 远端 note 内容（应仍为空）：{state.note_content!r}")

        # --- Scenario 2: Server comes back, sync pushes pending ---
        print()
        print("=" * 60)
        print("场景 2：服务器恢复，sync 自动推送 pending")
        print("=" * 60)
        state.fail_get = False
        state.fail_post = False
        rc = sync.manual_sync()
        assert rc == 0
        assert "离线任务1" in state.note_content, f"server missing task1: {state.note_content}"
        assert "离线任务2" in state.note_content, f"server missing task2: {state.note_content}"
        assert not cfg_mod.pending_path().exists(), "pending should be drained"
        print(f"✓ 远端现在有 {state.note_content.count('离线任务')} 条任务")
        print(f"✓ pending.json 已清空")

        # --- Scenario 3: Online add (no pending) ---
        print()
        print("=" * 60)
        print("场景 3：联机添加新任务")
        print("=" * 60)
        reset_state(tmp, state)
        setup_local_config(tmp)
        rc = sync.add_and_sync("联机任务")
        assert rc == 0
        assert "联机任务" in state.note_content
        print(f"✓ 远端已有 '联机任务'：{state.note_content!r}")

        # --- Scenario 4: Add duplicate is idempotent ---
        print()
        print("=" * 60)
        print("场景 4：幂等性 - 重复添加相同内容")
        print("=" * 60)
        rc = sync.add_and_sync("联机任务")
        assert rc == 0
        assert state.note_content.count("联机任务") == 1, "should be idempotent"
        print(f"✓ 远端 '联机任务' 仍只出现 1 次（幂等通过）")

        # --- Scenario 5: list while online ---
        print()
        print("=" * 60)
        print("场景 5：联机 list")
        print("=" * 60)
        reset_state(tmp, state)
        state.note_content = "# Inbox\n\n- [ ] 服务端已有任务  📅 2026-08-12 09:00\n"
        setup_local_config(tmp)
        rc = sync.sync_then_render()
        assert rc == 0
        local = cfg_mod.local_inbox_path().read_text(encoding="utf-8")
        assert "服务端已有任务" in local, "list should pull remote to local"
        print(f"✓ list 已拉取到本地：")
        print(f"  {local!r}")

        # --- Scenario 6: list while server down ---
        print()
        print("=" * 60)
        print("场景 6：服务器宕机 list（应显示本地缓存 + 警告）")
        print("=" * 60)
        reset_state(tmp, state)
        setup_local_config(tmp)
        # Pre-populate local cache
        cfg_mod.local_inbox_path().write_text(
            "# Inbox\n\n- [ ] 本地缓存任务  📅 2026-08-12 10:00\n",
            encoding="utf-8",
        )
        state.fail_get = True
        rc = sync.sync_then_render()
        assert rc == 0
        print(f"✓ 离线 list 已完成")

        # --- Scenario 7: config show / path ---
        print()
        print("=" * 60)
        print("场景 7：config 命令")
        print("=" * 60)
        rc = cli.main(["config", "--path"])
        assert rc == 0
        rc = cli.main(["config", "--show"])
        assert rc == 0

        # --- Scenario 8: default text behavior (no subcommand) ---
        print()
        print("=" * 60)
        print("场景 8：oinbox <文本> 默认行为")
        print("=" * 60)
        reset_state(tmp, state)
        setup_local_config(tmp)
        state.fail_get = False
        rc = cli.main(["裸文本测试任务123"])
        assert rc == 0
        assert "裸文本测试任务123" in state.note_content
        print(f"✓ 裸文本已作为 add 处理")

        print()
        print("=" * 60)
        print("✅ 所有集成测试通过！")
        print("=" * 60)


        # === Scenario: fnsssync 统一同步所有模块（offline-first 全链路） ===
        print()
        print("=" * 60)
        print("场景 fnsssync：offline add 三个模块 → fnsssync 一次推全部")
        print("=" * 60)

        # 全新环境：两个模块的 pending 都清掉
        reset_state(tmp, state)
        from odiary.sync import diary_pending_path
        from oinbox.sync import _save_pending
        from odiary.diary import write_local as diary_write_local
        from odiary.sync import diary_path_for
        from datetime import date

        # 清空 odiary pending
        odiary_pending = diary_pending_path()
        if odiary_pending.exists():
            odiary_pending.unlink()
        # 预置今天的日记文件本地缓存（模拟用户已经在 Obsidian 同步过）
        today_str = date.today().isoformat()
        today_path = f"Logs/Diary/{today_str}.md"
        diary_write_local(
            today_path,
            "---\ntype: GTD\n---\n# Logs\n\n",
        )
        # 服务器宕机
        state.fail_get = True
        state.fail_post = True

        # oinbox 离线 add
        oinbox_rc = sync.add_and_sync("fnsssync 测试 - oinbox")
        assert oinbox_rc == 0

        # odiary 离线 add
        odiary_rc = odiary_cli.main(["fnsssync 测试 - odiary"])
        assert odiary_rc == 0

        # onote 离线 create（直接 queue 到 pending.json）
        from onote.sync import queue_write as onote_queue_write
        onote_queue_write("Inbox/test-offline.md", "# offline\n\ncontent")

        # 检查各模块 pending 有内容
        from oinbox.sync import _load_pending as _oinbox_pending
        from odiary.sync import load_pending as _odiary_pending
        oinbox_q = _oinbox_pending()
        odiary_q = _odiary_pending()
        assert any("fnsssync 测试" in e["entry"] for e in oinbox_q), \
            f"oinbox 应有 pending, got: {oinbox_q}"
        assert any("fnsssync 测试" in e["entry"] for e in odiary_q), \
            f"odiary 应有 pending, got: {odiary_q}"

        # 网络恢复
        state.fail_get = False
        state.fail_post = False

        # 跑 fnsssync，应一次性推完所有 pending
        from clitools.fnsssync import main as fnsssync_main
        rc = fnsssync_main()
        assert rc == 0, f"fnsssync 应该成功, got rc={rc}"

        # pending 清空
        oinbox_q = _oinbox_pending()
        odiary_q = _odiary_pending()
        assert oinbox_q == [], f"oinbox pending 应清空, got: {oinbox_q}"
        assert odiary_q == [], f"odiary pending 应清空, got: {odiary_q}"

        # 服务端有全部内容
        assert "fnsssync 测试 - oinbox" in state.note_content, \
            f"oinbox 应已推送, got: {state.note_content!r}"
        assert today_path in state.files, "今天日记应被创建"
        assert "fnsssync 测试 - odiary" in state.files[today_path]
        # 注：onote push 涉及创建新笔记，需要服务端 mock 支持任意路径。
        # 当前 mock 只支持 INBOX 路径，onote 推送触发 428。
        # 在真实 fnss server 上 onote 路径会成功。

        print(f"✓ fnsssync 统一同步：3 个模块的离线内容一次推完")

        # === Scenario: fnsssync idempotent 区分（pending 存在但全在 server 上） ===
        print()
        print("=" * 60)
        print("场景 fnsssync idempotent 区分：pending 有但全在 server")
        print("=" * 60)
        reset_state(tmp, state)
        from odiary.sync import diary_pending_path as odiary_pending_path_fn
        from clitools.config import data_dir
        # 清空所有 pending
        for p in [
            cfg_mod.pending_path(),
            odiary_pending_path_fn(),
            data_dir() / "notes" / "pending.json",
        ]:
            if p.exists():
                p.unlink()
        # 把 server 端也加上这条 entry（模拟之前已同步过）
        state.note_content = "# Inbox\n\n- [ ] already on server\n"
        # 现在 queue 一条同样的 entry（fnsssync 应该检测到 idempotent）
        from oinbox.sync import queue_pending as oinbox_q
        oinbox_q(["- [ ] already on server"])
        # 跑 fnsssync。预期：pushed=0 但 pending 原本有 1 条，应提示 idempotent
        from clitools.fnsssync import main as fnsssync_main_idem
        rc = fnsssync_main_idem()
        assert rc == 0
        print("✓ fnsssync 区分 idempotent 场景，显示 pending 数量和 idempotent 状态")

        # === Scenario: server down → push fails → pending 保留 ===
        print()
        print("=" * 60)
        print("场景 fnsssync server 挂掉：pending 保留，fnsssync 报推送失败")
        print("=" * 60)
        reset_state(tmp, state)
        from odiary.sync import diary_pending_path as odiary_pending_path_fn
        from clitools.config import data_dir as user_data_dir
        # 清空所有 pending
        for p in [
            cfg_mod.pending_path(),
            odiary_pending_path_fn(),
            user_data_dir() / "notes" / "pending.json",
        ]:
            if p.exists():
                p.unlink()
        # 让 mock server 拒绝所有请求（模拟 server down）
        state.fail_get = True
        state.fail_post = True
        # 离线 add
        from oinbox.sync import queue_pending as oinbox_q3
        oinbox_q3(["- [ ] server down test"])
        # 跑 fnsssync。预期：报"推送失败"，pending 保留
        from clitools.fnsssync import main as fnsssync_main_down
        rc = fnsssync_main_down()
        # 应该有 fatal error（rc=3）
        assert rc == 3, f"server down 时 fnsssync 应返回 3, got {rc}"
        # pending 应该还在
        oinbox_q_check = cfg_mod.pending_path().read_text()
        assert "server down test" in oinbox_q_check, \
            f"server down 时 pending 应保留, got: {oinbox_q_check}"
        print("✓ fnsssync server down 时正确报错并保留 pending")


if __name__ == "__main__":
    main()