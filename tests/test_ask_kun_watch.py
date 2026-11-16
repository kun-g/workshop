from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys

import pytest

from workshop.core.cards import question
from workshop.plugins.channels.ask_kun import AskKun
from workshop.watch import Watch


@pytest.fixture
def fake(tmp_path, monkeypatch, request):
    directory = tmp_path / "fake"
    directory.mkdir()
    command = directory / "ask-kun"
    source = Path(__file__).parent / "fakes/ask_kun.py"
    command.write_text(f"#!{sys.executable}\n" + source.read_text())
    command.chmod(0o755)
    (directory / "config.json").write_text("{}")
    attention = directory / "attention.py"
    attention.write_text('''import json,sys
from pathlib import Path
root = Path(__file__).parent
with (root / "attention.jsonl").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
raise SystemExit(json.loads((root / "config.json").read_text()).get("attention_exit", 0))
''')
    if getattr(request, "param", "in_process") == "in_process":
        # 状态转换测试运行同一份 CLI 替身代码，省掉重复启动解释器。
        # 下方边界用例另外以真实子进程跑一份；改写过的脚本仍走真实子进程。
        original = command.read_text()
        code = compile(original, str(command), "exec")
        run = subprocess.run

        def inline(argv, *args, **kwargs):
            if (args or not isinstance(argv, (list, tuple)) or not argv or str(argv[0]) != str(command)
                    or kwargs.get("env") is not None or kwargs.get("cwd") is not None
                    or not os.access(command, os.X_OK) or command.read_text() != original):
                return run(argv, *args, **kwargs)
            out, err = io.StringIO(), io.StringIO()
            status = 0
            with monkeypatch.context() as patch, redirect_stdout(out), redirect_stderr(err):
                patch.setattr(sys, "argv", list(argv))
                patch.setattr(sys, "stdin", io.StringIO(kwargs.get("input") or ""))
                try:
                    exec(code, {"__name__": "__main__", "__file__": str(command)})
                except SystemExit as exc:
                    status = exc.code or 0
            return subprocess.CompletedProcess(argv, status, out.getvalue(), err.getvalue())

        monkeypatch.setattr(subprocess, "run", inline)
    return command


def configure(fake, **settings):
    (fake.parent / "config.json").write_text(json.dumps(settings))


def calls(fake, command=None):
    path = fake.parent / "calls.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if command is None or row["args"][0] == command]


def channel(fake, tmp_path):
    return AskKun(fake, tmp_path / "state")


def card(external="Q1", payload=None, **fields):
    return {"id": "core-card", "external": external, "payload": payload or question("批准发布", approval=True), **fields}


@pytest.mark.parametrize("choice", ["yes", "no"])
def test_answered_card_survives_wait_403(fake, tmp_path, choice):
    c = channel(fake, tmp_path)
    q = question("批准发布", approval=True)
    external = c.ask("core-card", q)
    path = fake.parent / "questions.json"
    rows = json.loads(path.read_text())
    rows[0].update(status="answered", choice=choice, answer_claimed=True)
    path.write_text(json.dumps(rows))
    configure(fake, errors={"wait": [4, "等待租约将自行到期：client:test: HTTP 403；未确认成功"]})
    answer, = c.poll([card(external, q)]).answers
    assert (answer.card_id, answer.value, answer.approved) == ("core-card", choice, choice == "yes")


@pytest.mark.parametrize("fake", ["process", "in_process"], indirect=True)
def test_ask_maps_key_and_persists_external(fake, tmp_path):
    q = question("批准发布", approval=True)
    q["context"] = "长资料" * 3000
    c = channel(fake, tmp_path)
    assert c.ask("a", q) == "Q1"
    assert channel(fake, tmp_path).ask("a", q) == "Q1"
    args = calls(fake, "ask")[0]["args"]
    assert args[args.index("--answer-format") + 1] == "key"
    for option in q["choices"]:
        assert f"{option['key']}={option['label']}" in args
        assert f"{option['key']}={option['effect']}" in args
    assert len(calls(fake, "ask")) == 1
    assert "--default" not in args
    assert "完整背景" in json.loads((fake.parent / "questions.json").read_text())[0]["details"]


@pytest.mark.parametrize("fake", ["process", "in_process"], indirect=True)
def test_lost_reply_reconciles_original_and_does_not_resend(fake, tmp_path):
    configure(fake, drop_ask_reply=True)
    q = question("批准发布", approval=True)
    with pytest.raises(RuntimeError):
        channel(fake, tmp_path).ask("a", q)
    c = channel(fake, tmp_path)
    assert c.reconcile("a", {"mode": "ask", "payload": q}) == "Q1"
    assert c.ask("a", q) == "Q1"
    assert len(calls(fake, "ask")) == 1


@pytest.mark.parametrize("settings", [{"empty_list": True}, {"bad_list": True}])
def test_inconclusive_reconciliation_never_resends(fake, tmp_path, settings):
    configure(fake, drop_ask_reply=True, **settings)
    q = question("选择方案")
    with pytest.raises(RuntimeError):
        channel(fake, tmp_path).ask("a", q)
    c = channel(fake, tmp_path)
    assert c.reconcile("a", {"mode": "ask"}) is None
    with pytest.raises(RuntimeError, match="禁止重发"):
        c.ask("a", q)
    assert len(calls(fake, "ask")) == 1


def test_same_subject_distinguishes_keys(fake, tmp_path):
    q = question("同一标题")
    c = channel(fake, tmp_path)
    assert c.ask("a", q) == "Q1"
    assert c.ask("b", q) == "Q2"
    rows = json.loads((fake.parent / "questions.json").read_text())
    assert rows[0]["from"] != rows[1]["from"]


@pytest.mark.parametrize("kind,value,approved", [("approval", "yes", True), ("approval", "no", False),
    ("approval", "批准", False), ("approval", "随便", False), ("reversible", "yes", False)])
def test_poll_approval_only_stable_yes(fake, tmp_path, kind, value, approved):
    configure(fake, answers={"Q1": [0, value]})
    q = question("批准发布", approval=kind == "approval")
    answer, = channel(fake, tmp_path).poll([card(payload=q)]).answers
    assert (answer.card_id, answer.value, answer.approved) == ("core-card", value, approved)
    assert calls(fake, "wait")[0]["args"] == ["wait", "Q1", "--timeout", "0"]


def test_pending_cancellation_and_failure_are_not_answers(fake, tmp_path):
    c = channel(fake, tmp_path)
    c.ask("pending", question("批准方案", approval=True))
    assert c.poll([card()]).answers == []
    configure(fake, answers={"Q1": [6, "已作废"]})
    cancelled, = c.poll([card()]).answers
    assert cancelled.cancelled and not cancelled.approved
    configure(fake, answers={"Q1": [1, "yes"]})
    assert c.poll([card()]).errors
    assert c.poll([card(cancelled=True), card(answered=True)]).answers == []


@pytest.mark.parametrize("fake", ["process", "in_process"], indirect=True)
def test_notify_idempotent_and_reconcile_reuses_key(fake, tmp_path):
    c = channel(fake, tmp_path)
    n = {"subject": "已完成", "text": "长通知" * 200, "fyi": True}
    configure(fake, notify_exit=1)
    with pytest.raises(RuntimeError):
        c.notify("notice-key", n)
    configure(fake)
    c = channel(fake, tmp_path)
    assert c.reconcile("notice-key", {"mode": "notice"}) == c._request_id("notice-key")
    assert c.notify("notice-key", n) == c._request_id("notice-key")
    rows = calls(fake, "notify")
    assert len(rows) == 2
    assert rows[0] == rows[1]
    assert rows[0]["args"] == ["notify", "--from", "车间", "--request-id", c._request_id("notice-key"), "-"]
    assert rows[0]["input"] == n["subject"] + "\n" + n["text"]


def test_cancel_reconciles_and_is_idempotent(fake, tmp_path):
    configure(fake, drop_ask_reply=True)
    c = channel(fake, tmp_path)
    with pytest.raises(RuntimeError):
        c.ask("a", question("选择方案"))
    c.cancel("a", None)
    channel(fake, tmp_path).cancel("a", None)
    assert len(calls(fake, "cancel")) == 1


NOW = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)


def setup_watch(fake, tmp_path, heartbeat=NOW, rounds=()):
    state = tmp_path / "watch-state"
    state.mkdir()
    if heartbeat:
        (state / "heartbeat.json").write_text(json.dumps({"last_ok_at": heartbeat.isoformat()}))
    with sqlite3.connect(state / "workshop.db") as db:
        db.execute("CREATE TABLE rounds(id TEXT, body TEXT)")
        db.executemany("INSERT INTO rounds VALUES (?,?)", [(str(i), json.dumps(row)) for i, row in enumerate(rounds)])
    return Watch(state, ask_kun=fake, attention=fake.parent / "attention.py")


def test_fresh_heartbeat_is_quiet(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    assert watch.run(NOW)["active"] == {}
    assert calls(fake) == []


def test_stale_once_across_restart_date_then_recovery_and_new_failure(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    Watch(watch.directory, ask_kun=fake).run(NOW + timedelta(days=1))
    assert len(calls(fake, "notify")) == 1
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=4))
    watch.run(NOW + timedelta(minutes=5))
    rows = calls(fake, "notify")
    assert len(rows) == 2
    assert rows[0]["args"][2] != rows[1]["args"][2]


def test_recovered_tick_between_watch_runs_starts_new_incident(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW + timedelta(minutes=5))
    watch.run(NOW + timedelta(minutes=6))
    assert len(calls(fake, "notify")) == 2


@pytest.mark.parametrize("status", ["starting", "running", "finishing"])
def test_overdue_unsettled_round_even_if_stopped(fake, tmp_path, status):
    watch = setup_watch(fake, tmp_path, rounds=[{"status": status, "at": (NOW - timedelta(hours=3)).isoformat(), "stopped": True},
                                              {"status": "ended", "at": (NOW - timedelta(days=1)).isoformat()}])
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    assert len(calls(fake, "notify")) == 1
    assert "超过期限" in calls(fake, "notify")[0]["input"]


def test_missing_heartbeat_and_database_are_detected(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, None)
    (watch.directory / "workshop.db").unlink()
    result = watch.run(NOW)
    assert set(result["active"]) == {"heartbeat", "database"}
    assert not (watch.directory / "workshop.db").exists()


def test_failed_ask_uses_attention_once(fake, tmp_path):
    configure(fake, notify_exit=1)
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    watch.run(NOW + timedelta(minutes=2))
    lines = (fake.parent / "attention.jsonl").read_text().splitlines()
    assert len(lines) == 1
    args = json.loads(lines[0])
    assert args[:3] == ["--writer", "车间", "--row"]
    row = json.loads(args[3])
    assert row["种类"] == "等你" and row["推飞书"] is True
    assert "需要你检查" in row["要什么"] and row["推荐"]
    assert row["来源会话"] == calls(fake, "notify")[0]["args"][2]


def test_both_notifications_fail_retry_same_incident(fake, tmp_path):
    configure(fake, notify_exit=1, attention_exit=1)
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    with pytest.raises(RuntimeError):
        watch.run(NOW + timedelta(minutes=1))
    configure(fake)
    watch.run(NOW + timedelta(minutes=2))
    assert calls(fake, "notify")[0] == calls(fake, "notify")[1]


def test_watch_cli_runs_with_fake_only(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    result = subprocess.run([sys.executable, "-m", "workshop.watch", "--state-dir", str(watch.directory), "--ask-kun", str(fake)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_launch_agents_have_absolute_paths_and_independent_schedule(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/launch_agents.py"
    spec = importlib.util.spec_from_file_location("launch_agents", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for label, config in module.SPECS.items():
        plist = plistlib.loads(module.payload(label, config, home=tmp_path))
        assert Path(plist["ProgramArguments"][0]).is_absolute()
        assert str(tmp_path / ".local/state/workshop") in plist["StandardErrorPath"]
        assert plist["EnvironmentVariables"]["WORKSHOP_STATE_DIR"] == str(tmp_path / ".local/state/workshop")
        # 执行者的 CLI 不在 launchd 默认 PATH：claude、cursor-agent 在 ~/.local/bin，orca 在 Orca.app 里。
        search = plist["EnvironmentVariables"]["PATH"].split(":")
        assert str(tmp_path.resolve() / ".local/bin") in search
        assert "/Applications/Orca.app/Contents/Resources/bin" in search and "/opt/homebrew/bin" in search
        if label.endswith("-watch"):
            assert plist["StartInterval"] == 60 and "KeepAlive" not in plist
            assert plist["ProgramArguments"][:2] == ["/usr/bin/python3", "-I"]
            assert plist["ProgramArguments"][2].endswith("watch/__init__.py")
        else:
            assert plist["KeepAlive"] is True and "StartInterval" not in plist
            assert plist["ProgramArguments"][-1] == "run"
            # 不声明时 launchd 按 Standard 限流，子进程跑 check.sh 慢约 5 倍（#164 实测 107 秒对 21 秒）。
            assert plist["ProcessType"] == "Interactive"
    with pytest.raises(ValueError):
        module.payload("com.kun.workshop", {"keep_alive": False})


def test_readability_repairs_and_true_upstream_validator(fake, tmp_path):
    q = question("PR #6 改动", approval=True)
    q.update(context="提交 1dfcbe2；路径 /tmp/check.py；卡 Q18", decision="采用\nPR #6 吗", reason="检查\t通过")
    q["choices"][0].update(label="确认", effect="确认")
    q["choices"][1].update(label="确认", effect="确认\r\n")
    c = channel(fake, tmp_path)
    assert c.ask("readable", q) == "Q1"
    args = calls(fake, "ask")[0]["args"]
    assert args[args.index("--context") + 1] == "完整依据见下方详情"
    details = json.loads((fake.parent / "questions.json").read_text())[0]["details"]
    assert "1dfcbe2" in details and "/tmp/check.py" in details and "PR #6" in details
    upstream = Path.home() / "workspace/runtime/worktrees/oh-my-assistant/workbench/switchboard/entry/ask-kun"
    if upstream.exists():
        # 只调用真实 CLI 的纯校验函数，绝不调用 main/发送/状态写入。
        import runpy
        legacy = runpy.run_path(str(upstream))
        parsed = legacy["build_parser"]().parse_args(args)
        fields = legacy["structured"](parsed)
        assert fields["answer_format"] == "key"
        assert fields["choices"][0]["key"] == "yes"


@pytest.mark.parametrize("mode", ["usage", "local"])
def test_definite_rejection_is_not_reconciliation(fake, tmp_path, mode):
    from workshop.core.ports import ChannelRejected
    c = channel(fake, tmp_path)
    q = question("选择方案")
    if mode == "usage":
        configure(fake, ask_exit=2)
    elif mode == "missing":
        fake.unlink()
    elif mode == "permission":
        fake.chmod(0o600)
    else:
        q["choices"][0]["key"] = "坏 key"
    with pytest.raises(ChannelRejected) as raised:
        c.ask("bad", q)
    assert str(raised.value)
    with pytest.raises(ChannelRejected):
        c.reconcile("bad", {"mode": "ask"})
    assert c._read("bad").get("rejected") and not c._read("bad").get("not_started")
    assert calls(fake, "list") == []


def test_poll_one_failure_does_not_drop_other_answers(fake, tmp_path, capsys):
    configure(fake, answers={"Q1": [1, ""], "Q2": [0, "yes"], "Q3": [3, ""]})
    result = channel(fake, tmp_path).poll([card(), card("Q2", id="second"), card("Q3", id="third")]).answers
    assert len(result) == 1 and result[0].card_id == "second" and result[0].approved
    assert "Q1" in capsys.readouterr().err


def test_thirty_day_cleanup_returns_cancelled(fake, tmp_path):
    c = channel(fake, tmp_path)
    c.ask("old", question("旧方案"))
    path = fake.parent / "questions.json"
    rows = json.loads(path.read_text())
    rows[0]["sent_at"] = "2000-01-01T00:00:00+00:00"
    rows[0]["status"] = "answered"
    path.write_text(json.dumps(rows))
    local = c._read("old")
    local["sent_at"] = rows[0]["sent_at"]
    c._save("old", local)
    configure(fake, legacy_list=True)
    answer, = c.poll([card(id="old")]).answers
    assert answer.cancelled and not answer.approved and answer.value == ""
    assert len(calls(fake, "ask")) == 1


def test_notifications_separate_state_directories_and_rebuilt_databases(fake, tmp_path):
    n = {"subject": "通知", "text": "已完成", "fyi": True}
    first = AskKun(fake, tmp_path / "one")
    second = AskKun(fake, tmp_path / "two")
    first.notify("same", n)
    second.notify("same", n)
    assert calls(fake, "notify")[0]["args"] != calls(fake, "notify")[1]["args"]
    db = first.directory.parent / "workshop.db"
    db.touch()
    rebuilt = AskKun(fake, db.parent)
    rebuilt.notify("same", n)
    assert rebuilt._request_id("same") != first._request_id("same")


def test_recent_watch_run_alerts_on_first_stale_observation(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW)
    for seconds in (0, 60, 120, 180):
        watch.run(NOW + timedelta(seconds=seconds))
    assert calls(fake, "notify") == []
    watch.run(NOW + timedelta(seconds=240))
    assert len(calls(fake, "notify")) == 1
    watch.run(NOW + timedelta(seconds=300))
    assert len(calls(fake, "notify")) == 1


def test_long_sleep_does_not_count_as_second_observation(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(hours=8))
    assert calls(fake, "notify") == []
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": (NOW + timedelta(hours=8)).isoformat()}))
    watch.run(NOW + timedelta(hours=8, minutes=1))
    assert calls(fake, "notify") == []


def test_attention_delivery_retries_notify_same_id_until_success(fake, tmp_path):
    configure(fake, notify_exit=1)
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    state = watch.run(NOW + timedelta(minutes=1))
    assert state["active"]["heartbeat"]["reported"] is False
    watch.run(NOW + timedelta(minutes=2))
    assert len((fake.parent / "attention.jsonl").read_text().splitlines()) == 1
    configure(fake)
    state = watch.run(NOW + timedelta(minutes=3))
    assert state["active"]["heartbeat"]["reported"] is True
    assert len(calls(fake, "notify")) == 3
    assert all(row == calls(fake, "notify")[0] for row in calls(fake, "notify"))


@pytest.mark.parametrize("fault", ["heartbeat", "round", "database", "delivery"])
def test_real_attention_keeps_visible_kind(fake, tmp_path, fault):
    attention = Path.home() / "workspace/runtime/tools/stuck_watch/attention.py"
    if not attention.exists():
        pytest.skip("本机没有 attention.py")
    configure(fake, notify_exit=1)
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    if fault != "heartbeat":
        (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat(),
            **({"delivery_failed_count": 1, "delivery_failed_since": (NOW-timedelta(minutes=5)).isoformat()} if fault == "delivery" else {})}))
    if fault == "database":
        (watch.directory / "workshop.db").unlink()
    if fault == "round":
        with sqlite3.connect(watch.directory / "workshop.db") as db:
            db.execute("INSERT INTO rounds VALUES ('r', ?)", (json.dumps({"status": "running", "at": (NOW-timedelta(hours=3)).isoformat()}),))
    watch.attention = str(attention)
    watch.attention_directory = tmp_path / "real-attention"
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    row = json.loads((watch.attention_directory / "车间.jsonl").read_text())
    assert row["种类"] == "等你" and row["推飞书"] is True


def test_system_python_watch_without_venv_and_quiet_unchanged_state(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, datetime.now(timezone.utc))
    script = Path(__file__).resolve().parents[1] / "src/workshop/watch/__init__.py"
    system_python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    # -I 丢弃 PYTHONPATH；脚本在任何工作目录都不需要安装 workshop。
    for _ in range(2):
        result = subprocess.run([system_python, "-I", str(script), "--state-dir", str(watch.directory), "--ask-kun", str(fake)],
                                cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""


def test_deadline_and_grace_override_fixed_two_hours_and_skip_bad_row(fake, tmp_path, capsys):
    rounds = [{"status": "running", "at": (NOW-timedelta(hours=3)).isoformat(), "deadline": (NOW+timedelta(hours=1)).isoformat()},
              {"status": "finishing", "at": (NOW-timedelta(minutes=5)).isoformat(), "deadline": (NOW-timedelta(seconds=61)).isoformat()},
              {"status": "running", "at": (NOW-timedelta(minutes=5)).isoformat(), "deadline": (NOW-timedelta(seconds=30)).isoformat()}]
    watch = setup_watch(fake, tmp_path, rounds=rounds)
    with sqlite3.connect(watch.directory / "workshop.db") as db:
        db.execute("INSERT INTO rounds VALUES ('broken', 'not json')")
    faults = watch.faults(NOW)
    assert set(faults) == {"round:1"}
    assert "broken" in capsys.readouterr().err


def test_old_round_uses_saved_rules_timeout(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, rounds=[{"status": "running", "at": (NOW-timedelta(minutes=8)).isoformat()}])
    with sqlite3.connect(watch.directory / "workshop.db") as db:
        db.execute("CREATE TABLE controls(id TEXT, body TEXT)")
        db.execute("INSERT INTO controls VALUES ('@rules', ?)", (json.dumps({"data": {"limits": {"round_timeout_seconds": 300}}}),))
    assert "round:0" in watch.faults(NOW)


@pytest.mark.parametrize("mode", ["missing", "permission", "uv"])
@pytest.mark.parametrize("operation", ["ask", "notify"])
def test_startup_failure_is_retryable_absent_not_rejection(fake, tmp_path, mode, operation):
    from workshop.plugins.channels.ask_kun import CommandNotStarted
    c = channel(fake, tmp_path)
    original = fake.read_text()
    q = question("批准方案") if operation == "ask" else {"subject": "通知", "text": "已完成", "fyi": True}
    if mode == "missing":
        fake.unlink()
    elif mode == "permission":
        fake.chmod(0o600)
    else:
        configure(fake, errors={operation: [2, "error: No interpreter found for Python 3.13"]})
    with pytest.raises(CommandNotStarted):
        getattr(c, operation)("retry", q)
    assert c._read("retry").get("not_started") and not c._read("retry").get("rejected")
    assert c.reconcile("retry", {"mode": operation}) == "absent"
    if mode == "missing":
        fake.write_text(original)
    fake.chmod(0o755)
    configure(fake)
    external = getattr(c, operation)("retry", q)
    assert external == ("Q1" if operation == "ask" else c._request_id("retry"))
    assert not c._read("retry").get("not_started")


@pytest.mark.parametrize("operation", ["ask", "notify"])
@pytest.mark.parametrize("code,error,rejected", [(2, "usage: ask-kun ask", True), (2, "尚未发送", True),
                                                  (4, "尚未发送", False), (2, "wrapper failed", False)])
def test_rejection_needs_both_send_exit_two_and_upstream_marker(fake, tmp_path, operation, code, error, rejected):
    from workshop.core.ports import ChannelRejected
    configure(fake, errors={operation: [code, error]})
    c = channel(fake, tmp_path)
    payload = question("选择方案") if operation == "ask" else {"subject": "通知", "text": "已完成", "fyi": True}
    with pytest.raises(ChannelRejected if rejected else RuntimeError):
        getattr(c, operation)("test", payload)
    assert bool(c._read("test").get("rejected")) == rejected


@pytest.mark.parametrize("operation", ["list", "wait", "cancel"])
def test_query_failure_never_rejects_sent_card(fake, tmp_path, operation):
    c = channel(fake, tmp_path)
    c.ask("sent", question("选择方案"))
    configure(fake, errors={operation: [2, "usage: ask-kun；尚未发送"]}, answers={"Q1": [0, "yes"]}, legacy_list=True)
    # cancel 需要 waiting 状态，避免已答的合法幂等短路。
    if operation == "cancel":
        configure(fake, errors={operation: [2, "usage: ask-kun；尚未发送"]})
    if operation == "cancel":
        with pytest.raises(RuntimeError):
            c.cancel("sent", "Q1")
    else:
        result = c.poll([card()])
        assert not result.answers and "usage: ask-kun" in result.errors["core-card"]
    assert c._read("sent")["external"] == "Q1" and not c._read("sent").get("rejected")


def test_list_environment_failure_does_not_reject_core_sent_card(fake, engine):
    from conftest import event, item
    engine.ingest(event())
    c = AskKun(fake, engine.log.path.parent)
    engine.plugins.register("channel", "channel", c)
    cid = engine.card(item(engine), "a", "ask", question("选择方案"))
    engine.channels()
    configure(fake, errors={"list": [2, "error: Failed to prepare uv environment"]})
    engine.channels()
    record = engine.log.get("cards", cid)
    assert record["sent"] and not record.get("rejected") and not record["cancelled"]


def test_http_403_requires_complete_absent_list(fake, tmp_path):
    c = channel(fake, tmp_path)
    c.ask("core-card", question("旧方案"))
    record = c._read("core-card")
    record["sent_at"] = "2000-01-01T00:00:00+00:00"
    c._save("core-card", record)
    configure(fake, answers={"Q1": [4, ""]}, legacy_list=True)
    assert c.poll([card()]).errors  # 编号还在，不得把权限/读取故障当成作废。
    configure(fake, answers={"Q1": [4, ""]}, remove_on_wait=True, legacy_list=True)
    answer, = c.poll([card()]).answers
    assert answer.cancelled
    assert len(calls(fake, "wait")) == 2


def test_poll_lists_once_and_only_waits_terminal_cards(fake, tmp_path):
    configure(fake, answers={"Q1": [3, ""], "Q2": [3, ""], "Q3": [0, "yes"], "Q4": [6, ""]})
    result = channel(fake, tmp_path).poll([card(f"Q{i}", id=str(i)) for i in range(1, 5)]).answers
    assert len(calls(fake, "list")) == 1
    assert [row["args"][1] for row in calls(fake, "wait")] == ["Q3", "Q4"]
    assert [a.card_id for a in result] == ["3", "4"]
    assert result[0].approved and result[1].cancelled


def test_incomplete_list_does_not_cancel_any_card(fake, tmp_path):
    configure(fake, empty_list=True, has_more=True)
    result = channel(fake, tmp_path).poll([card()])
    assert not result.answers and "列表不完整" in result.errors["core-card"]
    assert not calls(fake, "wait")


@pytest.mark.parametrize("technical", ["PR # 12", "pull  request", "a1b2c3d", "~/tmp/a.py"])
def test_titles_keep_human_work_name_and_match_upstream_notation(fake, tmp_path, technical):
    c = channel(fake, tmp_path)
    for name in ("订单同步", "库存校对"):
        q = question(name + " " + technical)
        assert c.ask(name, q)
    rows = json.loads((fake.parent / "questions.json").read_text())
    assert "订单同步" in rows[0]["subject"] and "库存校对" in rows[1]["subject"]
    assert technical not in rows[0]["subject"]
    assert rows[0]["subject"] != rows[1]["subject"]
    upstream = Path.home() / "workspace/runtime/worktrees/oh-my-assistant/workbench/switchboard/entry/ask-kun"
    if upstream.exists():
        import runpy
        from workshop.plugins.channels.ask_kun import TECHNICAL
        true = runpy.run_path(str(upstream))
        assert TECHNICAL.pattern == true["TECHNICAL"].pattern
        for call in calls(fake, "ask"):
            true["structured"](true["build_parser"]().parse_args(call["args"]))


def test_watch_delivery_failure_threshold_and_recovery(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    heartbeat = watch.directory / "heartbeat.json"
    def write(at, count=2):
        heartbeat.write_text(json.dumps({"last_ok_at": at.isoformat(), "delivery_failed_count": count,
            "delivery_failed_since": NOW.isoformat() if count else None}))
    for seconds in (0, 60, 120, 180, 240):
        at = NOW + timedelta(seconds=seconds)
        write(at)
        state = watch.run(at)
    assert set(state["active"]) == {"delivery"} and len(calls(fake, "notify")) == 1
    write(NOW + timedelta(seconds=300), 0)
    assert not watch.run(NOW + timedelta(seconds=300))["active"]


@pytest.mark.parametrize("phase", [0, 1, 19, 59])
def test_watch_detects_stop_within_four_minute_check_bound(fake, tmp_path, phase):
    watch = setup_watch(fake, tmp_path, NOW)
    watch.run(NOW - timedelta(seconds=phase))
    detected = None
    for seconds in range(60-phase, 301, 60):
        watch.run(NOW + timedelta(seconds=seconds))
        if calls(fake, "notify"):
            detected = seconds
            break
    assert 180 < detected <= 240



@pytest.mark.parametrize("lookup", ["list", "http403"])
@pytest.mark.parametrize("age", [None, 0, 28, 29, 30])
def test_missing_card_only_expires_after_known_twenty_nine_days(fake, tmp_path, lookup, age, capsys):
    c = AskKun(fake, tmp_path / "state", clock=lambda: NOW)
    c.ask("tracked", question("审批方案", approval=True))
    record = c._read("tracked")
    assert record["sent_at"] == NOW.isoformat()
    if age is None:
        record.pop("sent_at")
    else:
        record["sent_at"] = (NOW - timedelta(days=age)).isoformat()
    c._save("tracked", record)
    if lookup == "list":
        configure(fake, empty_list=True)
    else:
        configure(fake, answers={"Q1": [4, ""]}, remove_on_wait=True, legacy_list=True)
    if age == 30:
        answer, = c.poll([card(id="tracked")]).answers
        assert answer.cancelled and not answer.approved
    else:
        result = c.poll([card(id="tracked")])
        assert not result.answers and "不能确认清理" in result.errors["tracked"]
        assert "Q1" in capsys.readouterr().err
        assert not c._read("tracked").get("cancelled")
    assert len(calls(fake, "ask")) == 1


def test_new_hidden_card_does_not_drop_visible_answer(fake, tmp_path, capsys):
    c = channel(fake, tmp_path)
    c.ask("hidden", question("审批方案", approval=True))
    configure(fake, missing_ids=["Q1"], answers={"Q2": [0, "yes"]})
    answers = c.poll([card(id="hidden"), card("Q2", id="visible")]).answers
    assert len(answers) == 1 and answers[0].card_id == "visible" and answers[0].approved
    assert "Q1" in capsys.readouterr().err


def test_local_invalid_key_is_rejected_once_in_core_and_explained(fake, engine):
    from conftest import event, item
    from workshop.core.report import why
    engine.ingest(event())
    c = AskKun(fake, engine.log.path.parent)
    engine.plugins.register("channel", "channel", c)
    q = question("选择方案")
    q["choices"][0]["key"] = q["recommend"] = "bad key"
    cid = engine.card(item(engine), "bad-key", "ask", q)
    for _ in range(3):
        engine.channels()
    assert engine.log.get("cards", cid)["rejected"]
    assert "选项 key" in why(engine.log, item(engine)["id"])
    assert not calls(fake, "ask")
    assert len(calls(fake, "notify")) == 1
    assert c._read(cid).get("rejected") and not c._read(cid).get("not_started")


def test_recovered_send_keeps_upstream_send_time(fake, tmp_path):
    configure(fake, drop_ask_reply=True)
    c = channel(fake, tmp_path)
    with pytest.raises(RuntimeError):
        c.ask("lost", question("选择方案"))
    assert c.reconcile("lost", {"mode": "ask"}) == "Q1"
    upstream = json.loads((fake.parent / "questions.json").read_text())[0]
    assert c._read("lost")["sent_at"] == upstream["sent_at"]


def write_tick(watch, at, elapsed):
    (watch.directory / "heartbeat.json").write_text(json.dumps(
        {"at": at.isoformat(), "last_ok_at": at.isoformat(), "tick_ms": elapsed}))


def test_tick_alert_after_three_distinct_samples_once_across_restart_and_recovery(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    for n in range(6):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, 101)
        state = Watch(watch.directory, ask_kun=fake).run(at)
        assert len(calls(fake, "notify")) == (0 if n < 2 else 1)
    first_id = state["active"]["tick"]["id"]
    write_tick(watch, NOW + timedelta(minutes=6), 100)
    assert "tick" not in watch.run(NOW + timedelta(minutes=6))["active"]
    for n in range(7, 10):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, 101)
        state = watch.run(at)
    assert len(calls(fake, "notify")) == 2
    assert state["active"]["tick"]["id"] != first_id


@pytest.mark.parametrize("reset", [100, 0, None, "101", True, float("nan"), float("inf")])
def test_tick_good_or_invalid_sample_breaks_streak(fake, tmp_path, reset):
    watch = setup_watch(fake, tmp_path)
    for n, elapsed in enumerate([101, 101, reset, 101, 101]):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, elapsed)
        watch.run(at)
    assert not calls(fake, "notify")
    write_tick(watch, NOW + timedelta(minutes=5), 101)
    assert "tick" in watch.run(NOW + timedelta(minutes=5))["active"]
    assert len(calls(fake, "notify")) == 1


def test_tick_same_heartbeat_and_long_gap_do_not_accumulate(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    write_tick(watch, NOW, 101)
    for n in range(3):
        state = watch.run(NOW + timedelta(seconds=n * 30))
    assert state["tick_observation"]["count"] == 1
    assert not calls(fake, "notify")
    write_tick(watch, NOW + timedelta(minutes=1), 101)
    watch.run(NOW + timedelta(minutes=1))
    write_tick(watch, NOW + timedelta(hours=8), 101)
    state = watch.run(NOW + timedelta(hours=8))
    assert state["tick_observation"]["count"] == 1
    assert not calls(fake, "notify")


def test_tick_alert_retries_same_incident_after_delivery_failure(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    configure(fake, notify_exit=1)
    for n in range(3):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, 101)
        state = watch.run(at)
    assert not state["active"]["tick"]["reported"]
    assert state["active"]["tick"]["via"] == "attention"
    configure(fake)
    write_tick(watch, NOW + timedelta(minutes=3), 101)
    state = watch.run(NOW + timedelta(minutes=3))
    assert state["active"]["tick"]["reported"]
    assert calls(fake, "notify")[0] == calls(fake, "notify")[1]


@pytest.mark.parametrize('change', [
    {'status': 'waiting', 'choice': 'yes'}, {'status': 'waiting', 'choice': ''},
    {'choice': ''}, {'choice': 'unknown'}, {'choice': None},
    {'from': '车间:someone-else'}, {'subject': '另一张卡'}, {'answer_format': 'label'},
    {'status': 'broken'}, {'status': 'defaulted', 'choice': 'yes'},
])
def test_list_answer_boundaries(fake, tmp_path, change):
    c = channel(fake, tmp_path)
    q = question('批准方案', approval=True)
    c.ask('core-card', q)
    path = fake.parent / 'questions.json'
    rows = json.loads(path.read_text())
    rows[0].update(status='answered', choice='yes')
    rows[0].update(change)
    path.write_text(json.dumps(rows))
    configure(fake, errors={'wait': [4, 'HTTP 403']})
    result = c.poll([card(payload=q)])
    assert not result.answers
    if change.get('status') == 'waiting':
        assert not result.errors
    else:
        assert result.errors
    assert not calls(fake, 'wait')


@pytest.mark.parametrize('damage', ['missing', 'malformed', 'wrong-card', 'changed-payload'])
def test_list_answers_require_original_send_record(fake, tmp_path, damage):
    c = channel(fake, tmp_path)
    q = question('批准方案', approval=True)
    c.ask('core-card', q)
    path = fake.parent / 'questions.json'
    rows = json.loads(path.read_text())
    rows[0].update(status='answered', choice='yes')
    path.write_text(json.dumps(rows))
    local = c._read('core-card')
    if damage == 'missing':
        c._path('core-card').unlink()
    elif damage == 'malformed':
        c._path('core-card').write_text('{')
    else:
        if damage == 'wrong-card':
            local['external'] = 'Q2'
        else:
            local['payload']['context'] = 'changed'
        c._save('core-card', local)
    result = c.poll([card(payload=q)])
    assert not result.answers and result.errors


def test_real_channel_mixed_results_and_one_time_core_consumption(fake, engine):
    from conftest import event, item
    from workshop.core.report import status
    engine.ingest(event())
    c = AskKun(fake, engine.log.path.parent)
    engine.plugins.register('channel', 'channel', c)
    first = engine.card(item(engine), 'bad', 'ask', question('批准方案甲', approval=True))
    second = engine.card(item(engine), 'good', 'ask', question('批准方案乙', approval=True))
    engine.change(item(engine), state='等你')
    engine.channels()
    path = fake.parent / 'questions.json'
    rows = json.loads(path.read_text())
    rows[0].update(status='answered', choice='unknown')
    rows[1].update(status='answered', choice='no', answer_claimed=True)
    path.write_text(json.dumps(rows))
    configure(fake, errors={'wait': [4, 'HTTP 403']})
    for _ in range(10):
        engine.channels()
    assert 'Q1 连续 10 次读取失败' in status(engine.log)
    assert engine.log.get('cards', second)['answer'] == 'no'
    assert not engine.log.get('cards', second)['approved']
    assert not engine.log.get('cards', first)['answered']
    rows[0]['choice'] = 'yes'
    path.write_text(json.dumps(rows))
    engine.channels()
    engine.channels()
    assert engine.log.get('cards', first)['approved']
    assert item(engine)['state'] == '空闲'
    assert '连续 10 次读取失败' not in status(engine.log)
    assert len([f for f in engine.log.facts() if f['kind'] == 'answer']) == 2
    assert not calls(fake, 'wait')


def test_answer_read_does_not_invalidate_its_own_background_result(fake, engine, monkeypatch):
    from conftest import event, item
    from workshop.core.jobs import Jobs, encode, write
    from workshop.core.wake import Mailbox, Wake
    engine.ingest(event())
    c = AskKun(fake, engine.log.path.parent)
    engine.plugins.register('channel', 'channel', c)
    cid = engine.card(item(engine), 'answer', 'ask', question('批准方案', approval=True))
    engine.change(item(engine), state='等你')
    engine.channels()
    watched = fake.parent / 'questions.json'
    rows = json.loads(watched.read_text())
    rows[0].update(status='answered', choice='yes', answer_claimed=True)
    watched.write_text(json.dumps(rows))
    configure(fake, answers={'Q1': [0, 'yes']})
    # 使用真实文件变化检测，不需要 kqueue 或墙钟等待。
    box = Mailbox.__new__(Mailbox)
    box.paths = {watched: Mailbox.signature(watched)}
    jobs = Jobs(engine.log.path.parent / 'jobs', [], clock=lambda: 1.0)
    def start(path, spec):
        path.mkdir(parents=True, exist_ok=True)
        write(path / 'spec.json', spec)
    monkeypatch.setattr(jobs, 'start', start)
    engine.jobs = jobs
    try:
        engine.channels()  # 登记后台读取；主循环不等待。
        result = c.poll([engine.log.get('cards', cid)])
        path = jobs.path('channel', 'channel', 'poll', ['poll'])
        # 旧 wait 会重写 watched；结果即使成功也会被 awaken 标记丢弃。
        engine.awaken(Wake(paths=box.changed_paths()), {watched: {('channel', 'channel', ('poll',))}})
        write(path / 'result.json', {'ok': encode(result), 'finished': 1.0})
        engine.channels()
        assert engine.log.get('cards', cid)['answered']
        assert item(engine)['state'] == '空闲'
        for _ in range(3):
            engine.channels()  # 同一缓存结果也只消费一次。
        assert len([f for f in engine.log.facts() if f['kind'] == 'answer']) == 1
        assert not calls(fake, 'wait')
    finally:
        jobs.launcher.shutdown(wait=True)
