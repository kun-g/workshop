import json
from pathlib import Path
import os
import subprocess

from workshop.cli import main
from workshop.core.models import Event
from conftest import build, reach, finish


def test_cli_on_completed_state(tmp_path, capsys, monkeypatch):
    engine = build(tmp_path)
    reach(engine, "收尾")
    finish(engine, leave={"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察"})
    engine.log.close()
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(tmp_path))
    assert main(["status"]) == 0
    current = capsys.readouterr().out
    assert "alpha:one" not in current and "另有 1 件没列出（已结束" in current
    assert main(["status", "--all"]) == 0
    assert "alpha:one\t收尾\t完成" in capsys.readouterr().out
    assert main(["status", "--state", "完成", "--target", "alpha"]) == 0
    assert "alpha:one\t收尾\t完成" in capsys.readouterr().out
    assert main(["status", "--target", "beta", "--all"]) == 0
    assert "alpha:one" not in capsys.readouterr().out
    assert main(["why", "alpha:one"]) == 0
    assert "起因：事实" in capsys.readouterr().out
    assert main(["usage"]) == 0
    assert "800 / 0 / 0 / 160" in capsys.readouterr().out


def test_cli_ingest_pause_and_select(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(tmp_path))
    assert main(["pause", "workshop"]) == 0
    payload = json.dumps(dict(source="orca", ext_id="1", target="workshop", object_key="1", title="测试", body="资料"))
    assert main(["event", "--json", payload]) == 0
    assert main(["resume", "workshop"]) == 0
    assert "尚未恢复" in capsys.readouterr().out
    from workshop.core.log import Log
    log = Log(tmp_path / "workshop.db")
    seen = ",".join(e["id"] for e in log.all("deferred"))
    log.close()
    assert main(["resume", "workshop", "--select", "all", "--seen", seen]) == 0
    assert "补 1 个事件" in capsys.readouterr().out
    assert main(["event", "--json", payload]) == 0
    assert "未重复记录" in capsys.readouterr().out


def test_cli_invalid_input_and_unknown_item(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(tmp_path))
    assert main(["event", "--json", "{"]) == 2
    assert main(["why", "missing"]) == 2
    assert "找不到活" in capsys.readouterr().err


def test_packaged_command_runs(tmp_path):
    result = subprocess.run(["uv", "run", "workshop", "--state-dir", str(tmp_path), "status"],
                            capture_output=True, text=True, check=True)
    assert "活 0 件" in result.stdout


def test_status_html_page(tmp_path, capsys, monkeypatch):
    engine = build(tmp_path)
    reach(engine, "收尾")
    finish(engine, leave={"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察"})
    engine.log.close()
    monkeypatch.setenv("WORKSHOP_STATE_DIR", str(tmp_path))
    assert main(["status", "--html"]) == 0
    path = Path(capsys.readouterr().out.strip())
    assert path == tmp_path / "status.html"
    current = path.read_text()
    assert current.startswith("<!doctype html>") and "活 0 件" in current and "<h2>卡住" not in current and "alpha:one" not in current
    assert "另有 1 件没列出（已结束）" in current
    target = tmp_path / "all.html"
    assert main(["status", "--all", "--html", str(target)]) == 0
    full = target.read_text()
    assert "已结束 1 件" in full and "<b>活</b>alpha:one" in full
