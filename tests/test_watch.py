"""看门（workshop.watch）：心跳、超期轮次、慢拍，交项目经理入口；launchd 配置。

原在 test_ask_kun_watch.py，#176 删 ask-kun 通道时随文件删掉；看门本身还在，这里恢复与通道无关的用例。
"""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys

import pytest

from watch_helpers import fake, setup_watch, calls, configure, NOW, manager_command, delegated  # noqa: F401  fake 是夹具
from workshop.watch import WAIT_SERVICE_FAILURES, Watch, launch_label


def test_fresh_heartbeat_is_quiet(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    assert watch.run(NOW)["active"] == {}
    assert delegated(fake) == [] and calls(fake) == []


def test_stale_once_across_restart_date_then_recovery_and_new_failure(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    Watch(watch.directory, manager_command=manager_command(fake)).run(NOW + timedelta(days=1))
    assert len(delegated(fake)) == 1
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=4))
    watch.run(NOW + timedelta(minutes=5))
    rows = delegated(fake)
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"]


def test_recovered_tick_between_watch_runs_starts_new_incident(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": NOW.isoformat()}))
    watch.run(NOW + timedelta(minutes=5))
    watch.run(NOW + timedelta(minutes=6))
    assert len(delegated(fake)) == 2


@pytest.mark.parametrize("status", ["starting", "running", "finishing"])
def test_overdue_unsettled_round_even_if_stopped(fake, tmp_path, status):
    watch = setup_watch(fake, tmp_path, rounds=[{"status": status, "at": (NOW - timedelta(hours=3)).isoformat(), "stopped": True},
                                              {"status": "ended", "at": (NOW - timedelta(days=1)).isoformat()}])
    watch.run(NOW)
    watch.run(NOW + timedelta(minutes=1))
    assert len(delegated(fake)) == 1
    assert "超过期限" in delegated(fake)[0]["text"]


def test_missing_heartbeat_and_database_are_detected(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, None)
    (watch.directory / "workshop.db").unlink()
    result = watch.run(NOW)
    assert set(result["active"]) == {"heartbeat", "database"}
    assert not (watch.directory / "workshop.db").exists()


def test_watch_never_uses_old_message_routes(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    for n in range(3):
        watch.run(NOW + timedelta(minutes=n))
    assert len(delegated(fake)) == 1
    assert not calls(fake) and not (fake.parent / 'attention.jsonl').exists()


def test_manager_entry_failure_preserves_incident(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    configure(fake, manager_exit=1)
    with pytest.raises(RuntimeError):
        watch.run(NOW)
    first = json.loads((watch.directory / 'watch.json').read_text())
    configure(fake)
    second = watch.run(NOW + timedelta(minutes=1))
    assert first['active']['heartbeat']['id'] == second['active']['heartbeat']['id']
    assert 'manager_error' in first and 'manager_error' not in second
    assert not calls(fake)


def test_watch_cli_runs_with_fake_only(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    result = subprocess.run([sys.executable, "-m", "workshop.watch", "--state-dir", str(watch.directory), "--manager-command", json.dumps(manager_command(fake))], capture_output=True, text=True)
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


def test_recent_watch_run_alerts_on_first_stale_observation(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW)
    for seconds in (0, 60, 120, 180):
        watch.run(NOW + timedelta(seconds=seconds))
    assert delegated(fake) == []
    watch.run(NOW + timedelta(seconds=240))
    assert len(delegated(fake)) == 1
    watch.run(NOW + timedelta(seconds=300))
    assert len(delegated(fake)) == 1


def test_long_sleep_does_not_count_as_second_observation(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    watch.run(NOW)
    watch.run(NOW + timedelta(hours=8))
    assert delegated(fake) == []
    (watch.directory / "heartbeat.json").write_text(json.dumps({"last_ok_at": (NOW + timedelta(hours=8)).isoformat()}))
    watch.run(NOW + timedelta(hours=8, minutes=1))
    assert delegated(fake) == []


def test_repeated_delegation_retains_one_incident(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    first = watch.run(NOW)['active']['heartbeat']['id']
    for n in range(1,4):
        assert watch.run(NOW + timedelta(minutes=n))['active']['heartbeat']['id'] == first
    assert not calls(fake)


@pytest.mark.parametrize("fault", ["heartbeat", "round", "database", "delivery"])
def test_fault_details_are_delegated_without_messages(fake, tmp_path, fault):
    watch = setup_watch(fake, tmp_path, NOW - timedelta(minutes=4))
    if fault == 'database':
        (watch.directory / 'workshop.db').unlink()
    watch.run(NOW)
    state = watch.run(NOW + timedelta(minutes=1))
    assert state['active'] and all(i['text'] for i in delegated(fake))
    assert not calls(fake)


def test_system_python_watch_without_venv_and_quiet_unchanged_state(fake, tmp_path):
    watch = setup_watch(fake, tmp_path, datetime.now(timezone.utc))
    script = Path(__file__).resolve().parents[1] / "src/workshop/watch/__init__.py"
    system_python = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable
    # -I 丢弃 PYTHONPATH；脚本在任何工作目录都不需要安装 workshop。
    for _ in range(2):
        result = subprocess.run([system_python, "-I", str(script), "--state-dir", str(watch.directory), "--manager-command", json.dumps(manager_command(fake))],
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
    assert set(state["active"]) == {"delivery"} and len(delegated(fake)) == 1
    write(NOW + timedelta(seconds=300), 0)
    assert not watch.run(NOW + timedelta(seconds=300))["active"]


@pytest.mark.parametrize("phase", [0, 1, 19, 59])
def test_watch_detects_stop_within_four_minute_check_bound(fake, tmp_path, phase):
    watch = setup_watch(fake, tmp_path, NOW)
    watch.run(NOW - timedelta(seconds=phase))
    detected = None
    for seconds in range(60-phase, 301, 60):
        watch.run(NOW + timedelta(seconds=seconds))
        if delegated(fake):
            detected = seconds
            break
    assert 180 < detected <= 240




def write_tick(watch, at, elapsed):
    (watch.directory / "heartbeat.json").write_text(json.dumps(
        {"at": at.isoformat(), "last_ok_at": at.isoformat(), "tick_ms": elapsed}))


def test_tick_alert_after_three_distinct_samples_once_across_restart_and_recovery(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    for n in range(6):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, 101)
        state = Watch(watch.directory, manager_command=manager_command(fake)).run(at)
        assert len(delegated(fake)) == (0 if n < 2 else 1)
    first_id = state["active"]["tick"]["id"]
    write_tick(watch, NOW + timedelta(minutes=6), 100)
    assert "tick" not in watch.run(NOW + timedelta(minutes=6))["active"]
    for n in range(7, 10):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, 101)
        state = watch.run(at)
    assert len(delegated(fake)) == 2
    assert state["active"]["tick"]["id"] != first_id


@pytest.mark.parametrize("reset", [100, 0, None, "101", True, float("nan"), float("inf")])
def test_tick_good_or_invalid_sample_breaks_streak(fake, tmp_path, reset):
    watch = setup_watch(fake, tmp_path)
    for n, elapsed in enumerate([101, 101, reset, 101, 101]):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, elapsed)
        watch.run(at)
    assert not delegated(fake)
    write_tick(watch, NOW + timedelta(minutes=5), 101)
    assert "tick" in watch.run(NOW + timedelta(minutes=5))["active"]
    assert len(delegated(fake)) == 1


def test_tick_same_heartbeat_and_long_gap_do_not_accumulate(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    write_tick(watch, NOW, 101)
    for n in range(3):
        state = watch.run(NOW + timedelta(seconds=n * 30))
    assert state["tick_observation"]["count"] == 1
    assert not delegated(fake)
    write_tick(watch, NOW + timedelta(minutes=1), 101)
    watch.run(NOW + timedelta(minutes=1))
    write_tick(watch, NOW + timedelta(hours=8), 101)
    state = watch.run(NOW + timedelta(hours=8))
    assert state["tick_observation"]["count"] == 1
    assert not delegated(fake)


def test_tick_heartbeat_written_after_watch_started_still_counts(fake, tmp_path):
    # 守护进程一秒写约十次心跳，看门扫完故障再读时，心跳常比本次开始时间新几十毫秒（ws#167）。
    watch = setup_watch(fake, tmp_path)
    write_tick(watch, NOW + timedelta(milliseconds=40), 8)
    assert watch.run(NOW)["tick_observation"]["count"] == 0
    for n in range(3):
        write_tick(watch, NOW + timedelta(minutes=n + 1, milliseconds=40), 101)
        state = watch.run(NOW + timedelta(minutes=n + 1))
    assert state["tick_observation"]["count"] == 3 and "tick" in state["active"]
    write_tick(watch, NOW + timedelta(minutes=10), 8)
    assert "tick_observation" not in watch.run(NOW + timedelta(minutes=4))


def wait_service_rules(watch, endpoint='/tmp/service.sock'):
    """看门从核心的规则快照里取等待端口，不从别处另读一份。"""
    with sqlite3.connect(watch.directory / 'workshop.db') as db:
        db.execute('CREATE TABLE IF NOT EXISTS controls(id TEXT, body TEXT)')
        db.execute("INSERT OR REPLACE INTO controls VALUES ('@rules', ?)",
                   (json.dumps({'data': {'limits': {'round_timeout_seconds': 7200},
                                         'wait_services': {'intervention': {'endpoint': endpoint}}}}),))


def beat(watch, at):
    """心跳跟着测试的假时钟走；探活用例只关心等待端口，不制造心跳故障。"""
    (watch.directory / 'heartbeat.json').write_text(json.dumps({'last_ok_at': at.isoformat()}))


def test_wait_service_restarts_once_then_reports_only_if_still_down(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    wait_service_rules(watch)
    down, restarts = {'value': True}, []
    def probe(endpoint):
        if down['value']:
            raise TimeoutError('探活超时')
        return {'protocol': 1}
    def restart(endpoint):
        restarts.append(endpoint)
        return 'com.kun.workshop-intervention'
    watch.probe, watch.restart = probe, restart
    at = NOW
    for n in range(WAIT_SERVICE_FAILURES):
        at = NOW + timedelta(minutes=n)
        beat(watch, at)
        state = watch.run(at)
    assert restarts == ['/tmp/service.sock'] and not delegated(fake)
    failure = state['wait_services']['intervention']
    assert failure['failures'] == WAIT_SERVICE_FAILURES and failure['restart_label'] == 'com.kun.workshop-intervention'
    # 重启刚发出的一拍不报；下一拍仍不通才登记给项目经理。
    at += timedelta(minutes=1)
    beat(watch, at)
    state = watch.run(at)
    assert len(delegated(fake)) == 1 and delegated(fake)[0]['text'].startswith('等待端口 intervention')
    first_id = state['active']['service:intervention']['id']
    at += timedelta(minutes=1)
    beat(watch, at)
    assert watch.run(at)['active']['service:intervention']['id'] == first_id
    down['value'] = False
    at += timedelta(minutes=1)
    beat(watch, at)
    state = watch.run(at)
    assert state['active'] == {} and state['wait_services']['intervention']['failures'] == 0
    assert 'restarted_at' not in state['wait_services']['intervention']
    # 恢复后再次连续失败，是新的一段：重新自动重启，事项也重新登记。
    down['value'] = True
    for _ in range(WAIT_SERVICE_FAILURES):
        at += timedelta(minutes=1)
        beat(watch, at)
        state = watch.run(at)
    assert restarts == ['/tmp/service.sock', '/tmp/service.sock']
    at += timedelta(minutes=1)
    beat(watch, at)
    state = watch.run(at)
    assert state['active']['service:intervention']['id'] != first_id


def test_wait_service_failed_restart_is_reported_with_reason(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    wait_service_rules(watch, '/tmp/other.sock')
    def probe(endpoint):
        raise ConnectionRefusedError('拒绝连接')
    def restart(endpoint):
        raise RuntimeError('找不到指向等待端口的 launchd 定义')
    watch.probe, watch.restart = probe, restart
    for n in range(WAIT_SERVICE_FAILURES + 1):
        at = NOW + timedelta(minutes=n)
        beat(watch, at)
        state = watch.run(at)
    record = state['wait_services']['intervention']
    assert record['restart_error'] == '找不到指向等待端口的 launchd 定义'
    assert record['last_error'] == '拒绝连接'
    assert len(delegated(fake)) == 1


def test_launch_label_matches_endpoint_never_guesses(tmp_path):
    agents = tmp_path / 'agents'
    agents.mkdir()
    endpoint = tmp_path / 'run/service.sock'
    (agents / 'other.plist').write_bytes(plistlib.dumps(
        {'Label': 'other', 'ProgramArguments': ['x', '--endpoint', '/elsewhere/service.sock']}))
    (agents / 'one.plist').write_bytes(plistlib.dumps(
        {'Label': 'com.kun.workshop-intervention',
         'ProgramArguments': ['python', '-m', 'workshop.plugins.intervention', '--endpoint', str(endpoint), 'serve']}))
    assert launch_label(endpoint, agents) == 'com.kun.workshop-intervention'
    assert launch_label(tmp_path / 'missing.sock', agents) is None


def test_tick_incident_survives_manager_entry_failure(fake, tmp_path):
    watch = setup_watch(fake, tmp_path)
    for n in range(3):
        at = NOW + timedelta(minutes=n)
        write_tick(watch, at, 101)
        state = watch.run(at)
    first = state['active']['tick']['id']
    configure(fake, manager_exit=1)
    write_tick(watch, NOW + timedelta(minutes=3), 101)
    with pytest.raises(RuntimeError):
        watch.run(NOW + timedelta(minutes=3))
    configure(fake)
    write_tick(watch, NOW + timedelta(minutes=4), 101)
    assert watch.run(NOW + timedelta(minutes=4))['active']['tick']['id'] == first
    assert not calls(fake)
