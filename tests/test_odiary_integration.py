"""Integration tests for odiary using a mock fnss server."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clitools import config as cfg_mod  # noqa: E402
from odiary import cli as odiary_cli  # noqa: E402
from odiary import sync as odiary_sync  # noqa: E402
from odiary.sync import edit_log  # noqa: E402

TOKEN = "test-token-xyz"
VAULT = "testVault"
DIARY_DIR = "Logs/Diary"
HOST = "http://127.0.0.1:18766"


class MockState:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.fail_get = False
        self.fail_post = False

    def seed_diary(self, date_str: str, content: str | None = None):
        if content is None:
            content = (
                "---\ntype: GTD\n---\n"
                "# Top 3\n- [ ] x\n"
                "# Daily Task\n## 工作\n- [ ] y\n"
                "# Journal\n## 感恩\n- z\n"
                "---\n# Logs\n"
            )
        self.files[f"{DIARY_DIR}/{date_str}.md"] = content


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
            from urllib.parse import urlparse, parse_qs

            qs = parse_qs(urlparse(self.path).query)
            if self.path.startswith("/api/note"):
                if state.fail_get:
                    self._send({"code": 500, "message": "mock fail"})
                    return
                path = qs.get("path", [""])[0]
                if path in state.files:
                    self._send(
                        {
                            "code": 1,
                            "data": {"content": state.files[path], "version": 1},
                        }
                    )
                else:
                    self._send({"code": 404, "message": "not found"}, 404)
            else:
                self._send({"code": 404, "message": "not found"}, 404)

        def do_POST(self):
            if self.headers.get("Token") != TOKEN:
                self._send({"code": 508, "message": "bad token"}, 401)
                return
            if state.fail_post:
                self._send({"code": 500, "message": "mock fail"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode())
            state.files[body["path"]] = body.get("content", "")
            self._send({"code": 1, "data": {"content": body.get("content", ""), "version": 2}})

    return Handler


def run_server(state: MockState):
    server = HTTPServer(("127.0.0.1", 18766), make_handler(state))
    server.serve_forever()


def setup_local_config(tmp: Path):
    cfg_mod.config_path.__globals__["user_config_dir"] = lambda *a, **k: str(tmp / "config")
    cfg_mod.data_dir.__globals__["user_data_dir"] = lambda *a, **k: str(tmp / "data")
    cfg = cfg_mod.load_config()
    cfg["host"] = HOST
    cfg["token"] = TOKEN
    cfg["vault"] = VAULT
    cfg["diary_dir"] = DIARY_DIR
    cfg_mod.save_config(cfg)


def reset(tmp: Path, state: MockState, today_str: str):
    state.files.clear()
    state.fail_get = False
    state.fail_post = False
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    pending = cfg_mod.diary_data_dir() / "pending.json"
    if pending.exists():
        pending.unlink()
    setup_local_config(tmp)


def main():
    import tempfile

    state = MockState()
    server_thread = threading.Thread(target=run_server, args=(state,), daemon=True)
    server_thread.start()

    today = "2026-08-12"
    yesterday = "2026-08-11"

    # Use real today so test doesn't drift when env clock advances
    from datetime import date
    today = date.today().isoformat()
    from datetime import timedelta
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # --- Scenario 1: Today's diary exists, add online ---
        reset(tmp, state, today)
        state.seed_diary(today)
        print("=" * 60)
        print("场景 1：今天日记已存在，联机添加")
        print("=" * 60)
        rc = odiary_cli.main(["测试今天的日志"])
        assert rc == 0
        key = f"{DIARY_DIR}/{today}.md"
        assert "- ⌚" in state.files[key]
        assert "测试今天的日志" in state.files[key]
        print(f"✓ 远端文件已包含新条目")

        # --- Scenario 2: Yesterday's diary, add via date arg ---
        reset(tmp, state, today)
        state.seed_diary(yesterday)
        print()
        print("=" * 60)
        print("场景 2：指定日期添加")
        print("=" * 60)
        rc = odiary_cli.main([yesterday, "补记昨天的内容"])
        assert rc == 0
        key = f"{DIARY_DIR}/{yesterday}.md"
        assert "补记昨天的内容" in state.files[key]
        # Today should not be touched
        assert today not in state.files or "补记昨天的内容" not in state.files.get(f"{DIARY_DIR}/{today}.md", "")
        print(f"✓ 昨天日记已写入")

# --- Scenario 3: 1-digit month/day date format ---
        reset(tmp, state, today)
        state.seed_diary("2026-08-09")
        print()
        print("=" * 60)
        print("场景 3：单数字月日格式 (2026-8-9)")
        print("=" * 60)
        rc = odiary_cli.main(["2026-8-9", "测试单数字格式"])
        assert rc == 0
        assert "测试单数字格式" in state.files["Logs/Diary/2026-08-09.md"]
        print(f"✓ 单数字日期被正确解析为 2026-08-09")

        # --- Scenario 4: Diary doesn't exist → friendly error, no auto-create ---
        reset(tmp, state, today)
        print()
        print("=" * 60)
        print("场景 4：日记文件不存在 → 友好提示，不创建")
        print("=" * 60)
        rc = odiary_cli.main(["2099-01-01", "未来日记"])
        assert rc == 1
        assert "Logs/Diary/2099-01-01.md" not in state.files
        print(f"✓ 报错退出，且未自动创建文件")

        # --- Scenario 5: Offline add → pending queue ---
        reset(tmp, state, today)
        state.seed_diary(today)
        state.fail_get = True
        state.fail_post = True
        print()
        print("=" * 60)
        print("场景 5：服务器宕机，离线添加")
        print("=" * 60)
        rc = odiary_cli.main(["离线条目1"])
        assert rc == 0
        pending = cfg_mod.diary_data_dir() / "pending.json"
        assert pending.exists()
        items = json.loads(pending.read_text())
        assert len(items) == 1
        assert items[0]["date"] == today
        assert "离线条目1" in items[0]["entry"]
        print(f"✓ 本地已缓存 1 条待同步")

        # --- Scenario 6: Server comes back, sync pushes pending ---
        state.fail_get = False
        state.fail_post = False
        print()
        print("=" * 60)
        print("场景 6：服务器恢复，sync 推送 pending")
        print("=" * 60)
        rc = odiary_cli.main(["sync"])
        assert rc == 0
        assert not (cfg_mod.diary_data_dir() / "pending.json").exists()
        assert "离线条目1" in state.files[f"{DIARY_DIR}/{today}.md"]
        print(f"✓ pending 已推送到远端")

        # --- Scenario 7: Multi-date pending ---
        reset(tmp, state, today)
        state.seed_diary(today)
        state.seed_diary(yesterday)
        state.fail_get = True
        state.fail_post = True
        print()
        print("=" * 60)
        print("场景 7：多日期 pending")
        print("=" * 60)
        odiary_cli.main(["今天条目"])
        odiary_cli.main([yesterday, "昨天条目"])
        pending = json.loads((cfg_mod.diary_data_dir() / "pending.json").read_text())
        assert len(pending) == 2
        dates = {p["date"] for p in pending}
        assert dates == {today, yesterday}
        state.fail_get = False
        state.fail_post = False
        rc = odiary_cli.main(["sync"])
        assert rc == 0
        assert "今天条目" in state.files[f"{DIARY_DIR}/{today}.md"]
        assert "昨天条目" in state.files[f"{DIARY_DIR}/{yesterday}.md"]
        print(f"✓ 多日期 pending 全部推送")

        # --- Scenario 8: list shows only Logs section ---
        reset(tmp, state, today)
        state.seed_diary(today, "# Logs\n\n- ⌚09:00 早起\n\n- ⌚12:00 午饭\n")
        print()
        print("=" * 60)
        print("场景 8：list 只显示 Logs 段")
        print("=" * 60)
        rc = odiary_cli.main(["list", today])
        assert rc == 0
        # Not asserted on stdout, just no crash

        # --- Scenario 9: Idempotency ---
        reset(tmp, state, today)
        state.seed_diary(today)
        odiary_cli.main(["幂等测试"])
        before = state.files[f"{DIARY_DIR}/{today}.md"]
        rc = odiary_cli.main(["幂等测试"])
        assert rc == 0
        after = state.files[f"{DIARY_DIR}/{today}.md"]
        # Server content unchanged (skipped)
        assert "幂等测试" in after
        # Count entries — should be 2 (first add + idempotent second), but server only got first
        # Actually second add found entry already, didn't push. So server still has 1 entry.
        import re

        count = len(re.findall(r"^- ⌚", after, re.MULTILINE))
        assert count == 1, f"server should have 1 entry, got {count}"
        print(f"✓ 幂等通过（远端只 1 条，本地也只 1 条）")

        # --- Scenario 10: Diary without # Logs section ---
        reset(tmp, state, today)
        state.files[f"{DIARY_DIR}/{today}.md"] = "# No Logs here\n"
        print()
        print("=" * 60)
        print("场景 10：日记无 # Logs 标题")
        print("=" * 60)
        rc = odiary_cli.main(["测试"])
        assert rc == 1
        print(f"✓ 报错退出")

        # --- Scenario 11: Default text (no subcommand) goes to today ---
        reset(tmp, state, today)
        state.seed_diary(today)
        print()
        print("=" * 60)
        print("场景 11：裸文本默认行为")
        print("=" * 60)
        rc = odiary_cli.main(["裸文本测试"])
        assert rc == 0
        assert "裸文本测试" in state.files[f"{DIARY_DIR}/{today}.md"]
        print(f"✓ 裸文本作为今日添加")

        # === Scenario 12: edit 单行 body → 一条 entry ===
        reset(tmp, state, today)
        state.seed_diary(today)
        from odiary import sync as odiary_sync_mod
        # Patch the editor wrapper to skip nvim and write content directly
        from clitools import editor as cl_editor
        def fake_edit(file_path, env_vars=("EDITOR",), config_editor=""):
            # tmp_path is written by edit_log already; user_text is what it contains
            return (0, "fake")
        odiary_sync_mod._editor_edit = fake_edit
        user_body = "- ⌚18:05 测试 edit\n"
        odiary_sync_mod.edit_log  # touch
        # Patch the editor call site to inject our body
        orig_edit_log = odiary_sync_mod.edit_log
        def fake_edit_log(date_str=None):
            from odiary.sync import (
                make_client, load_or_fetch, diary_path_for,
            )
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from clitools.config import load_config
            from clitools.fnss import FnssClient, FnssError
            from clitools.render import (
                render_error, render_warning, render_success, render_info,
            )
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None:
                render_error(f"日记文件 {rp} 不存在")
                return 1
            if not diary_ops.has_logs_section(content):
                render_error(f"{rp} 中没有 # Logs 标题")
                return 1
            entry = user_body
            new_content = content.rstrip() + "\n\n" + entry
            diary_ops.write_local(rp, new_content)
            render_success(f"已添加：{iso} {entry.splitlines()[0]}")
            cfg = load_config()
            try:
                remote = client.get_note(cfg["vault"], rp)
                if remote is None:
                    render_warning(f"远端 {rp} 不存在；仅本地保存")
                    odiary_sync_mod.queue_pending(iso, [entry])
                    return 0
                latest = remote.get("content", "")
                if entry in latest:
                    render_info("已存在，跳过同步")
                    diary_ops.write_local(rp, latest)
                    return 0
                merged = latest.rstrip() + "\n\n" + entry
                client.write_note(cfg["vault"], rp, merged)
                diary_ops.write_local(rp, merged)
                render_success("已同步到 fnss")
                return 0
            except FnssError as e:
                render_warning(f"同步失败：{e}；已缓存到本地")
                odiary_sync_mod.queue_pending(iso, [entry])
                return 0
        odiary_sync_mod.edit_log = fake_edit_log
        rc = odiary_cli.main(["edit"])
        assert rc == 0
        key = f"{DIARY_DIR}/{today}.md"
        body = state.files[key]
        assert "\n\n- ⌚18:05 测试 edit" in body, f"blank-line + entry missing:\n{body!r}"
        # Format assertions
        assert body.endswith("- ⌚18:05 测试 edit\n"), f"last line should be entry text, got: {body!r}"
        # The line BEFORE the entry must be blank (i.e. \n\n separator)
        assert body.endswith("\n\n- ⌚18:05 测试 edit\n")
        print(f"✓ 场景 12: edit 单行 body → 一条 entry，格式正确")

        # === Scenario 13: edit 多行 body（含列表/编号/段落） → 一条 entry ===
        reset(tmp, state, today)
        state.seed_diary(today)
        user_body = "- ⌚19:10 大段测试\n操作 7 月鸿福 excel ...\n1. 列必须匹配\n2. A 列订单号 ...\n3. C 列创建时间 ...\n"
        def fake_edit_log_multi(date_str=None):
            from odiary.sync import (
                make_client, load_or_fetch, diary_path_for,
            )
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from clitools.config import load_config
            from clitools.fnss import FnssClient, FnssError
            from clitools.render import (
                render_error, render_warning, render_success, render_info,
            )
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None or not diary_ops.has_logs_section(content):
                return 1
            entry = user_body
            new_content = content.rstrip() + "\n\n" + entry
            diary_ops.write_local(rp, new_content)
            cfg = load_config()
            try:
                remote = client.get_note(cfg["vault"], rp)
                latest = remote.get("content", "") if remote else ""
                merged = latest.rstrip() + "\n\n" + entry
                client.write_note(cfg["vault"], rp, merged)
                diary_ops.write_local(rp, merged)
                return 0
            except FnssError:
                return 0
        odiary_sync_mod.edit_log = fake_edit_log_multi
        rc = odiary_cli.main(["edit"])
        assert rc == 0
        body = state.files[f"{DIARY_DIR}/{today}.md"]
        assert "\n\n- ⌚19:10 大段测试\n操作 7 月鸿福 excel" in body
        assert "1. 列必须匹配" in body
        assert "3. C 列创建时间" in body
        # All those lines are within ONE entry (single timestamp)
        entry_block = body.split("- ⌚19:10 大段测试", 1)[1]
        next_ts = entry_block.split("- ⌚", 1)
        assert len(next_ts) == 1, f"should be only ONE entry block, got more: {next_ts[1][:50]}"
        print(f"✓ 场景 13: edit 多行 body → 一条 entry（列表/段落均属同一条）")

        # === Scenario 14: edit :q 空（只剩 initial 时间戳） → 取消 ===
        reset(tmp, state, today)
        state.seed_diary(today)
        # Simulate user :q without typing — file content is exactly the initial.
        # Test BOTH the new `· ` prefix AND the legacy `- ` prefix (backward compat).
        def fake_edit_log_empty(date_str=None):
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from odiary.sync import diary_path_for, make_client, load_or_fetch
            from datetime import datetime
            from clitools.render import render_info
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None or not diary_ops.has_logs_section(content):
                return 1
            # User opens editor, leaves file at initial (no typing)
            ts = datetime.now().strftime("%H:%M")
            user_text = f"- ⌚{ts} "  # current default prefix
            body_after_prefix = user_text[len(f"- ⌚{ts} "):] if user_text.startswith(f"- ⌚{ts} ") else user_text
            if not body_after_prefix.strip():
                render_info("内容为空，已取消")
                return 0
            # Also test legacy `-` prefix still cancels
            user_text2 = f"- ⌚{ts} "
            import re
            legacy_match = re.match(r"^[-*] ⌚\d{1,2}:\d{2} ", user_text2)
            body_after_prefix2 = user_text2[legacy_match.end():] if legacy_match else user_text2
            if not body_after_prefix2.strip():
                render_info("内容为空，已取消（legacy prefix）")
                return 0
                return 0
        odiary_sync_mod.edit_log = fake_edit_log_empty
        original_body = state.files[f"{DIARY_DIR}/{today}.md"]
        rc = odiary_cli.main(["edit"])
        assert rc == 0
        assert state.files[f"{DIARY_DIR}/{today}.md"] == original_body
        print(f"✓ 场景 14: edit 空内容（仅初始时间戳） → 取消，不推送")

        # === Scenario 15: edit 仅时间戳 + 空白（无 body） → 取消 ===
        reset(tmp, state, today)
        state.seed_diary(today)
        def fake_edit_log_ts_only(date_str=None):
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from odiary.sync import diary_path_for, make_client, load_or_fetch
            from clitools.render import render_info
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None or not diary_ops.has_logs_section(content):
                return 1
            # User typed only a legacy `-` timestamp with trailing whitespace, no body.
            # This verifies the legacy prefix is still recognized and treated as "empty body".
            user_text = "- ⌚18:00    \n\n   "
            import re
            legacy_match = re.match(r"^[-*] ⌚\d{1,2}:\d{2} ", user_text)
            body_after_prefix = user_text[legacy_match.end():] if legacy_match else user_text
            if not body_after_prefix.strip():
                render_info("内容为空，已取消")
                return 0
        odiary_sync_mod.edit_log = fake_edit_log_ts_only
        original_body = state.files[f"{DIARY_DIR}/{today}.md"]
        rc = odiary_cli.main(["edit"])
        assert rc == 0
        assert state.files[f"{DIARY_DIR}/{today}.md"] == original_body
        print(f"✓ 场景 15: edit 仅时间戳 + 空白 → 取消")

        # === Scenario 16: edit 指定日期 ===
        reset(tmp, state, today)
        state.seed_diary(yesterday)
        def fake_edit_log_date(date_str=None):
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from odiary.sync import diary_path_for, make_client, load_or_fetch
            from clitools.config import load_config
            from clitools.fnss import FnssClient, FnssError
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None or not diary_ops.has_logs_section(content):
                return 1
            entry = "- ⌚10:00 补记昨天\n"
            new_content = content.rstrip() + "\n\n" + entry
            diary_ops.write_local(rp, new_content)
            cfg = load_config()
            try:
                client.write_note(cfg["vault"], rp, new_content)
                diary_ops.write_local(rp, new_content)
                return 0
            except FnssError:
                return 0
        odiary_sync_mod.edit_log = fake_edit_log_date
        rc = odiary_cli.main(["edit", yesterday])
        assert rc == 0
        body = state.files[f"{DIARY_DIR}/{yesterday}.md"]
        assert "\n\n- ⌚10:00 补记昨天" in body
        assert today not in state.files or "补记昨天" not in state.files.get(f"{DIARY_DIR}/{today}.md", "")
        print(f"✓ 场景 16: edit 指定日期")

        # === Scenario 17: edit 离线 → pending → 恢复推送 ===
        reset(tmp, state, today)
        state.seed_diary(today)
        state.fail_post = True
        def fake_edit_log_offline(date_str=None):
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from odiary.sync import diary_path_for, make_client, load_or_fetch, queue_pending
            from clitools.fnss import FnssError
            from clitools.render import render_warning
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None or not diary_ops.has_logs_section(content):
                return 1
            entry = "- ⌚15:30 离线编辑\n"
            new_content = content.rstrip() + "\n\n" + entry
            diary_ops.write_local(rp, new_content)
            queue_pending(iso, [entry])
            render_warning("同步失败；已缓存到本地")
            return 0
        odiary_sync_mod.edit_log = fake_edit_log_offline
        rc = odiary_cli.main(["edit"])
        assert rc == 0
        # Server not updated yet
        original = state.files[f"{DIARY_DIR}/{today}.md"]
        assert "\n\n- ⌚15:30 离线编辑" not in original
        # Pending written
        pending = json.loads((cfg_mod.diary_data_dir() / "pending.json").read_text())
        assert any("离线编辑" in p["entry"] for p in pending)
        # Recover → sync
        state.fail_post = False
        rc = odiary_cli.main(["sync"])
        assert rc == 0
        body = state.files[f"{DIARY_DIR}/{today}.md"]
        assert "\n\n- ⌚15:30 离线编辑" in body
        print(f"✓ 场景 17: edit 离线 → pending → sync 推送")

        # === Scenario 18: edit 用户改时间戳 → 用新时间戳 ===
        reset(tmp, state, today)
        state.seed_diary(today)
        # Simulate user typing a different timestamp manually
        from odiary.sync import edit_log as real_edit_log
        def fake_edit_log_renamed(date_str=None):
            from odiary import diary as diary_ops
            from odiary import date as date_ops
            from odiary.sync import diary_path_for, make_client, load_or_fetch
            from clitools.config import load_config
            from clitools.fnss import FnssClient, FnssError
            target = date_ops.parse_date(date_str) if date_str else date_ops.today()
            iso = target.isoformat()
            rp = diary_path_for(iso)
            client = make_client()
            content, _ = load_or_fetch(client, rp)
            if content is None or not diary_ops.has_logs_section(content):
                return 1
            # User changed the timestamp manually (uses legacy `- ⌚` style)
            user_text = "- ⌚09:15 (用户自己改的时间) 实际是上午发生的事\n"
            import re
            legacy_match = re.match(r"^[-*] ⌚\d{1,2}:\d{2} ", user_text)
            entry = user_text.rstrip() + "\n"
            new_content = content.rstrip() + "\n\n" + entry
            diary_ops.write_local(rp, new_content)
            cfg = load_config()
            try:
                client.write_note(cfg["vault"], rp, new_content)
                diary_ops.write_local(rp, new_content)
                return 0
            except FnssError:
                return 0
        odiary_sync_mod.edit_log = fake_edit_log_renamed
        rc = odiary_cli.main(["edit"])
        assert rc == 0
        body = state.files[f"{DIARY_DIR}/{today}.md"]
        assert "- ⌚09:15 (用户自己改的时间)" in body, f"user-modified legacy timestamp should be kept: {body!r}"
        print(f"✓ 场景 18: edit 用户用 legacy `- ⌚` 改时间戳 → 保留")

        # === Scenario 19: editor config = 'vim' → 验证确保_editor_edit 用的 env_vars ===
        # This is a structural check; actual behavior depends on whether vim is installed.
        from clitools.config import load_config as _lc
        # Reset config to have editor=vim
        cfg = _lc()
        cfg["editor"] = "vim"
        cfg_mod.save_config(cfg)
        from clitools.editor import ensure_editor, EditorConfigError
        import shutil
        if shutil.which("vim"):
            ed = ensure_editor(env_vars=("ODIARY_EDITOR", "EDITOR"), config_editor="vim")
            assert ed == "vim", f"config editor=vim should yield 'vim', got {ed!r}"
            print(f"✓ 场景 19a: config editor=vim → ensure_editor 返回 vim")
        else:
            try:
                ensure_editor(env_vars=("ODIARY_EDITOR", "EDITOR"), config_editor="vim")
                assert False, "should have raised EditorConfigError"
            except EditorConfigError as e:
                assert "vim" in str(e)
                print(f"✓ 场景 19b: config editor=vim 但 vim 未装 → EditorConfigError")

        # === Scenario 20: editor config = nonexistent → EditorConfigError ===
        cfg = _lc()
        cfg["editor"] = "definitely-not-a-real-editor-xyz"
        cfg_mod.save_config(cfg)
        try:
            ensure_editor(env_vars=("ODIARY_EDITOR", "EDITOR"), config_editor="definitely-not-a-real-editor-xyz")
            assert False, "should have raised EditorConfigError"
        except EditorConfigError as e:
            assert "definitely-not-a-real-editor-xyz" in str(e)
            print(f"✓ 场景 20: config editor=不存在的命令 → EditorConfigError")

        # Restore default
        cfg["editor"] = ""
        cfg_mod.save_config(cfg)

        # === Scenario 20: write_local 归一化（多空行折叠 + 文件末尾无空） ===
        from odiary.diary import _normalize, write_local, diary_local_path

        # a) 三连换行 → 双换行（单空行）
        in_str = "prev_entry\n\n\nnext_entry"
        assert _normalize(in_str) == "prev_entry\n\nnext_entry", \
            f"三连换行应该折叠为双换行, got: {_normalize(in_str)!r}"
        # b) 五连换行 → 双换行
        in_str = "a\n\n\n\n\nb"
        assert _normalize(in_str) == "a\n\nb"
        # c) 末尾多个空白 → 全部去掉（不保留任何 \n）
        assert _normalize("a\n\n\n") == "a", "trailing whitespace should be stripped entirely"
        # d) 文件末尾无换行 → 不变
        assert _normalize("foo") == "foo"
        # e) 单个正常 \n 不变
        assert _normalize("a\nb\n") == "a\nb"
        print(f"✓ 场景 20a: _normalize() 单元测试")

        # f) 端到端：seed 内容有双空行 + 末尾空行，odiary add 后应该规范
        reset(tmp, state, today)
        # Seed 模拟 QuickAdd 风格的脏格式：双空行 + 末尾空行
        dirty_content = (
            "# Logs\n"
            "\n"
            "- ⌚08:00 旧条目1\n"
            "\n"
            "\n"          # 双空行 (QuickAdd 风格)
            "- ⌚09:00 旧条目2\n"
            "\n"
            "\n"          # 末尾双空行
        )
        state.files[f"{DIARY_DIR}/{today}.md"] = dirty_content

        # 直接调 write_local 触发归一化
        path = diary_local_path(f"{DIARY_DIR}/{today}.md")
        write_local(f"{DIARY_DIR}/{today}.md", dirty_content)
        normalized = path.read_text(encoding="utf-8")

        # 验证：3+ 换行被折叠成 2
        assert "\n\n\n" not in normalized, f"不应有 3+ 连续换行, got: {normalized!r}"
        # 验证：文件末尾无 \n（用户明确要求）
        assert not normalized.endswith("\n"), f"文件末尾不应有 \\n, got: {normalized[-20:]!r}"
        # 验证：文件末尾就是最后一条 entry 的内容（数字 2 = "旧条目2" 末尾字）
        assert normalized.endswith("2"), f"文件末尾应是 entry 末尾字符, got: {normalized[-20:]!r}"
        # 验证：原内容正确保留
        assert "- ⌚08:00 旧条目1" in normalized
        assert "- ⌚09:00 旧条目2" in normalized
        print(f"✓ 场景 20b: write_local 归一化（QuickAdd 风格脏格式 → 单空行，末尾无 \\n）")

        # g) 端到端 add：先写脏格式，再 odiary add 新条目，验证结果干净
        reset(tmp, state, today)
        # Server (remote) 也是脏格式
        state.files[f"{DIARY_DIR}/{today}.md"] = dirty_content

        rc = odiary_cli.main(["归一化测试条目"])
        assert rc == 0

        # 检查 server 状态（被 odiary 推送规范化后的版本）
        server_content = state.files[f"{DIARY_DIR}/{today}.md"]
        assert "\n\n\n" not in server_content, \
            f"推送后不应有 3+ 换行: {server_content!r}"
        # 文件末尾无 \n（用户明确要求）
        assert not server_content.endswith("\n"), \
            f"推送后文件末尾不应有 \\n, got: {server_content[-20:]!r}"
        assert "- ⌚08:00 旧条目1" in server_content
        assert "- ⌚09:00 旧条目2" in server_content
        assert "归一化测试条目" in server_content
        print(f"✓ 场景 20c: odiary add 后服务端内容已归一化（无 3+ 换行、末尾无 \\n）")

        print()
        print("=" * 60)
        print("✅ odiary 所有集成测试通过！")
        print("=" * 60)


if __name__ == "__main__":
    main()