"""看门（workshop.watch）测试共用的替身：项目经理入口记录、心跳和轮次库。

原在 test_ask_kun_watch.py，#176 删掉 ask-kun 通道时一并删了该文件；看门本身还在，替身挪到这里。
看门不再走旧消息路径（ask-kun、提醒命令），fake 目录里只剩项目经理入口的记录和退出码开关。
"""
from datetime import datetime, timezone
import json
import sqlite3
import sys

import pytest

from workshop.watch import Watch

NOW = datetime(2026, 11, 13, 10, tzinfo=timezone.utc)


@pytest.fixture
def fake(tmp_path):
    """替身目录里的占位路径；fake.parent 放 config.json 和各入口的调用记录。"""
    directory = tmp_path / "fake"
    directory.mkdir()
    (directory / "config.json").write_text("{}")
    return directory / "unused"


def configure(fake, **settings):
    (fake.parent / "config.json").write_text(json.dumps(settings))


def calls(fake, command=None):
    """旧消息路径的调用记录；看门不该再写它，应始终为空。"""
    path = fake.parent / "calls.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if command is None or row["args"][0] == command]


def manager_command(fake):
    path = fake.parent / 'manager.py'
    path.write_text("""import json,sys
from pathlib import Path
root=Path(__file__).parent
value=json.loads(Path(sys.argv[-1]).read_text())
with (root/'manager.jsonl').open('a') as f: f.write(json.dumps(value)+'\\n')
raise SystemExit(json.loads((root/'config.json').read_text()).get('manager_exit',0))
""")
    return [sys.executable, str(path)]


def delegated(fake):
    path = fake.parent / 'manager.jsonl'
    unique = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        for incident in json.loads(line)['faults'].values():
            if incident['confirmed']:
                unique[incident['id']] = incident
    return list(unique.values())


def setup_watch(fake, tmp_path, heartbeat=NOW, rounds=()):
    state = tmp_path / "watch-state"
    state.mkdir()
    if heartbeat:
        (state / "heartbeat.json").write_text(json.dumps({"last_ok_at": heartbeat.isoformat()}))
    with sqlite3.connect(state / "workshop.db") as db:
        db.execute("CREATE TABLE rounds(id TEXT, body TEXT)")
        db.executemany("INSERT INTO rounds VALUES (?,?)", [(str(i), json.dumps(row)) for i, row in enumerate(rounds)])
    return Watch(state, manager_command=manager_command(fake))
