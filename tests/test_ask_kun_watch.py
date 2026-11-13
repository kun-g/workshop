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
    assert json.loads((fake.parent / "questions.json").read_text())[0]["details"] == q["context"][:6000]


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
    with pytest.raises(RuntimeError, match="已在上游作废"):
        c.poll([card()])
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
    assert rows[0]["args"] == ["notify", "--from", "车间", "--request-id", "notice-key", "-"]
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
    Watch(watch.directory, ask_kun=fake).run(NOW + timedelta(days=1))
    assert len(calls(fake, "notify")) == 1
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=4))
    rows = calls(fake, "notify")
    assert len(rows) == 2
    assert rows[0]["args"][2] != rows[1]["args"][2]


def test_recovered_tick_between_watch_runs_starts_new_incident(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW + timedelta(minutes=5))
    assert len(calls(fake, "notify")) == 2


@pytest.mark.parametrize("status", ["starting", "running", "finishing"])
def test_overdue_unsettled_round_even_if_stopped(fake, tmp_path, status):
    watch = setup_watch(fake, tmp_path, rounds=[{"status": status, "at": (NOW - timedelta(hours=3)).isoformat(), "stopped": True},
                                              {"status": "ended", "at": (NOW - timedelta(days=1)).isoformat()}])
    watch.run(NOW)
    watch.run(NOW)
    assert len(calls(fake, "notify")) == 1
    assert "超过两小时" in calls(fake, "notify")[0]["input"]


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
    watch.run(NOW)
    lines = (fake.parent / "attention.jsonl").read_text().splitlines()
    assert len(lines) == 1
    args = json.loads(lines[0])
    assert args[:3] == ["--writer", "车间", "--row"]
    row = json.loads(args[3])
    assert row["种类"] == "等你" and row["推飞书"] is False
    assert "Kun" in row["要什么"] and row["推荐"]
    assert row["来源会话"] == calls(fake, "notify")[0]["args"][2]


def test_both_notifications_fail_retry_same_incident(fake, tmp_path):
    configure(fake, notify_exit=1, attention_exit=1)
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    with pytest.raises(RuntimeError):
        watch.run(NOW)
    configure(fake)
    watch.run(NOW + timedelta(minutes=1))
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
            assert plist["StartInterval"] == 300 and "KeepAlive" not in plist
            assert "workshop.watch" in plist["ProgramArguments"]
        else:
            assert plist["KeepAlive"] is True and "StartInterval" not in plist
            assert plist["ProgramArguments"][-1] == "run"
    with pytest.raises(ValueError):
        module.payload("com.kun.workshop", {"keep_alive": False})
