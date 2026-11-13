from datetime import datetime, timedelta, timezone
import importlib.util
import json
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
def fake(tmp_path):
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
    answer, = channel(fake, tmp_path).poll([card(payload=q)])
    assert (answer.card_id, answer.value, answer.approved) == ("core-card", value, approved)
    assert calls(fake, "wait")[0]["args"] == ["wait", "Q1", "--timeout", "0"]


def test_pending_cancellation_and_failure_are_not_answers(fake, tmp_path):
    c = channel(fake, tmp_path)
    assert c.poll([card()]) == []
    configure(fake, answers={"Q1": [6, "已作废"]})
    cancelled, = c.poll([card()])
    assert cancelled.cancelled and not cancelled.approved
    configure(fake, answers={"Q1": [1, "yes"]})
    with pytest.raises(RuntimeError):
        c.poll([card()])
    assert c.poll([card(cancelled=True), card(answered=True)]) == []


def test_notify_idempotent_and_reconcile_reuses_key(fake, tmp_path):
    c = channel(fake, tmp_path)
    n = {"subject": "已完成", "text": "长通知" * 200, "fyi": True}
    configure(fake, notify_exit=1)
    with pytest.raises(RuntimeError):
        c.notify("notice-key", n)
    configure(fake)
    c = channel(fake, tmp_path)
    assert c.reconcile("notice-key", {"mode": "notice"}) == "notice-key"
    assert c.notify("notice-key", n) == "notice-key"
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
        if label.endswith("-watch"):
            assert plist["StartInterval"] == 60 and "KeepAlive" not in plist
            assert plist["ProgramArguments"][:2] == ["/usr/bin/python3", "-I"]
            assert plist["ProgramArguments"][2].endswith("watch/__init__.py")
        else:
            assert plist["KeepAlive"] is True and "StartInterval" not in plist
            assert plist["ProgramArguments"][-1] == "run"
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


@pytest.mark.parametrize("mode", ["usage", "missing", "permission", "local"])
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
    if mode != "local":
        with pytest.raises(ChannelRejected):
            c.reconcile("bad", {"mode": "ask"})
    assert calls(fake, "list") == []


def test_poll_one_failure_does_not_drop_other_answers(fake, tmp_path, capsys):
    configure(fake, answers={"Q1": [1, ""], "Q2": [0, "yes"], "Q3": [3, ""]})
    result = channel(fake, tmp_path).poll([card(), card("Q2", id="second"), card("Q3", id="third")])
    assert len(result) == 1 and result[0].card_id == "second" and result[0].approved
    assert "Q1" in capsys.readouterr().err


def test_thirty_day_cleanup_returns_cancelled(fake, tmp_path):
    c = channel(fake, tmp_path)
    c.ask("old", question("旧方案"))
    path = fake.parent / "questions.json"
    rows = json.loads(path.read_text())
    rows[0]["sent_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(rows))
    answer, = c.poll([card()])
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


def test_heartbeat_requires_two_separated_observations_and_recovers_quietly(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(seconds=10))
    assert calls(fake, "notify") == []
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW + timedelta(seconds=60))
    assert calls(fake, "notify") == []
    # 正常 60 秒调度下，从最后一次成功 tick 起五分钟内告警。
    for seconds in (120, 180, 240, 300):
        watch.run(NOW + timedelta(seconds=seconds))
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


def test_real_attention_keeps_visible_kind(fake, tmp_path):
    attention = Path.home() / "workspace/runtime/tools/stuck_watch/attention.py"
    if not attention.exists():
        pytest.skip("本机没有 attention.py")
    configure(fake, notify_exit=1)
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
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
