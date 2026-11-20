"""核心 R37：推送式来源。入口快速应答、只写 spool 再叫醒守护进程；推送和轮询共用归一键；陈旧只留痕。

入口用本机 loopback 起真的 HTTP 服务（标准库），来源和守护进程主循环照生产；GitHub 一侧用测试目录里回放
真实响应的 gh 替身。等状态用「查条件 + 总超时」的循环，不用 sleep（E8）。
"""
from contextlib import contextmanager
import copy
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest
import yaml

from conftest import build, configuration
from test_issue_delivery import world, edit  # noqa: F401  world 是 pytest 夹具
from workshop.core.log import Log
from workshop.core.models import Evidence
from workshop.core.rules import Rules
from workshop.plugins.sources.github import GitHubSource
from workshop.plugins.sources.webhook import WebhookSource
from workshop.plugins.sources.webhook.config import settings
from workshop.plugins.sources.webhook.entry import serve

FIXTURES = Path(__file__).parent / "fixtures/github_push"
SECRET = "whsec-r37-" + "q7" * 16
REPO = "kun-g/workshop"
BASE = f"repos/{REPO}"
TRIGGERS = Rules.load(Path(__file__).parents[1] / "rules/workshop.yaml").target("workshop")["triggers"]
V5, V6, V7 = "2026-11-20T14:00:05Z", "2026-11-20T14:00:06Z", "2026-11-20T14:00:07Z"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def until(predicate, timeout=20, what="条件"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
    raise AssertionError(f"等{what}超时")


def hook(tmp_path, secret=SECRET, **extra):
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret_file = tmp_path / "webhook-secret"
    if secret is not None:
        secret_file.write_text(secret + "\n")
        secret_file.chmod(0o600)
    return {"listen": "127.0.0.1", "port": 0, "secret_file": str(secret_file),
            "spool": str(tmp_path / "spool"), **extra}


def spooled(config):
    root = Path(config["spool"])
    return sorted(root.glob("*.json")) if root.exists() else []


@contextmanager
def entry(config, log=None):
    server = serve(settings({"name": "alpha", "repo": REPO, "webhook": config}), log=log)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(port, payload, event, *, signature=True, host="127.0.0.1"):
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event,
               "X-GitHub-Delivery": str(uuid.uuid4())}
    if signature is True:
        headers["X-Hub-Signature-256"] = sign(body)
    elif signature:
        headers["X-Hub-Signature-256"] = signature
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        began = time.monotonic()
        connection.request("POST", "/", body, headers)
        response = connection.getresponse()
        return response.status, response.read().decode(), time.monotonic() - began
    finally:
        connection.close()


def labeled(**issue):
    payload = fixture("webhook-issues-labeled-315.json")
    payload["issue"].update(issue)
    return payload


def issue_change(action, **issue):
    payload = labeled(**issue)
    payload["action"] = action
    if action not in ("labeled", "unlabeled"):
        payload.pop("label")
    return payload


def comment_on_315(comment_id, updated_at, body="这里换个做法", action="created"):
    payload = fixture("webhook-issue_comment-created-168.json")
    payload["issue"] = fixture("rest-issue-315.json")
    payload["comment"].update(id=comment_id, body=body, created_at=updated_at, updated_at=updated_at,
                              issue_url=f"https://api.github.com/{BASE}/issues/315")
    payload["action"] = action
    return payload


def engine_for(world, config, sources=("webhook",)):
    data = configuration()
    target = {**world["target"], "sources": list(sources), "webhook": config,
              "flows": {"flow": {"triggers": TRIGGERS}}}
    target.pop("plugin", None)
    data["targets"]["alpha"] = target
    engine = build(world["root"] / "core", data)
    engine.plugins.register("source", "github", GitHubSource(str(world["commands"] / "gh")))
    engine.plugins.register("source", "webhook", WebhookSource())
    flow = engine.plugins.get("flow", "flow")
    # 每轮结束都停在「等资料」：只有新事件才会再起一轮，轮次增量就是事件触发的空跑与否。
    flow.assess = lambda item, receipt, target: Evidence([["true"]], reason="等来源补资料", awaits_update=True)
    return engine


def restart(engine, world, config, sources=("webhook",)):
    engine.log.close()
    return engine_for(world, config, sources)


def quiesce(engine, limit=12):
    """一拍一拍跑到停：没有在跑的轮，这一拍也没再记新事实（归件、起轮都要隔一拍）。"""
    for _ in range(limit):
        before = engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0]
        engine.tick()
        rows = engine.active()
        for row in rows:
            engine.submit(row["token"], {"outcome": "done", "summary": "看过"})
        if not rows and engine.log.db.execute("SELECT max(seq) FROM facts").fetchone()[0] == before:
            return
    raise AssertionError("活一直在起轮")


def rounds(engine):
    return len(engine.log.all("rounds"))


def github_facts(engine):
    return [f for f in engine.log.facts() if f["source"] == "github"]


def responses_315(issue, events=None, comments=()):
    return {BASE + "/issues?state=all&sort=updated&per_page=100": [issue],
            BASE + "/pulls/comments?sort=updated&direction=asc&per_page=100": [],
            BASE + "/issues/315": issue,
            BASE + "/issues/315/events?per_page=100": events if events is not None else fixture("rest-issue-315-events.json"),
            BASE + "/issues/315/comments?per_page=100": list(comments)}


def poll_into(engine, world, responses, cursor=None):
    """现有轮询插件的真实解析入口，结果经核心同一条收事件路径入库。"""
    edit(world, responses=responses)
    batch = engine.plugins.get("source", "github").poll(engine.rules.target("alpha"), cursor)
    engine.refresh_aliases()
    with engine.log.transaction():
        for event in batch.events:
            engine.ingest(event)
    return batch


# ── 入口：2 秒内验签、原子写 spool、回 2xx ─────────────────────────────

def test_entry_answers_within_two_seconds_and_spools_atomically(tmp_path):
    config = hook(tmp_path)
    payload = labeled()
    with entry(config) as port:
        status, body, elapsed = post(port, payload, "issues")
    assert 200 <= status < 300 and elapsed < 2
    files = spooled(config)
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["event"] == "issues" and saved["payload"] == payload
    # 原子写：只有改名后的成品，没有残留的临时文件。
    assert [p.name for p in Path(config["spool"]).rglob("*") if p.is_file()] == [files[0].name]


def test_signature_rejections_keep_secret_out_of_body_log_spool_and_executor_env(tmp_path):
    config = hook(tmp_path)
    log = io.StringIO()
    payload = labeled()
    with entry(config, log) as port:
        wrong = post(port, payload, "issues", signature="sha256=" + "0" * 64)
        missing = post(port, payload, "issues", signature=None)
    assert wrong[0] in (401, 403) and missing[0] in (401, 403)
    unset = hook(tmp_path / "unset", secret=None)
    with entry(unset, log) as port:
        no_secret = post(port, payload, "issues")
    assert no_secret[0] in (401, 403)
    for status, body, _ in (wrong, missing, no_secret):
        assert SECRET not in body
    assert SECRET not in log.getvalue() and log.getvalue()
    assert spooled(config) == [] and spooled(unset) == []
    # secret 只认部署目录里的文件，不写进规则。
    with pytest.raises(ValueError, match="secret"):
        settings({"name": "alpha", "repo": REPO, "webhook": {**config, "secret": SECRET}})


def test_executor_child_environment_has_no_webhook_secret(world, tmp_path):
    from workshop.plugins.executors.local.isolation import session_environment
    config = hook(tmp_path)
    engine = engine_for(world, config)
    with entry(config) as port:
        assert post(port, labeled(), "issues")[0] == 202
        runtime = engine.runtime("token-1")
        env = session_environment({"env": runtime["env"], "readonly": False})
        child = subprocess.run([sys.executable, "-c", "import os, json; print(json.dumps(dict(os.environ)))"],
                               env=env, capture_output=True, text=True, check=True)
    assert SECRET not in json.dumps(runtime) and SECRET not in child.stdout
    assert SECRET not in json.dumps(dict(os.environ))
    engine.log.close()


# ── 守护进程：在线即时叫醒，离线落盘、上线补消费 ─────────────────────────

def daemon_rules(config):
    data = configuration()
    data.pop("taskhome")  # 守护进程替身不装工单
    data["targets"] = {"alpha": {"repo": REPO, "sources": ["webhook"], "webhook": config,
                                 "flows": {"flow": {"triggers": [
                                     {"source": "github", "when": {"kind": "label"}, "action": "ready"},
                                     {"source": "github", "action": "update"}]}}}}
    return data


def launches(state):
    path = state / "launches.log"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def short_state():
    # macOS 的 Unix 地址长度有限（信箱 wake.sock）；不用 pytest 的长测试名组成地址。
    with tempfile.TemporaryDirectory(prefix="ws-hook-", dir="/tmp") as directory:
        yield Path(directory)


def test_daemon_wakes_on_push_and_drains_spool_written_while_offline(tmp_path, short_state):
    state = short_state
    config = hook(tmp_path)
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(daemon_rules(config)))
    with entry(config) as port:
        # 守护进程停着：入口照样 2 秒内 2xx，已落盘。
        status, _, elapsed = post(port, labeled(), "issues")
        assert status == 202 and elapsed < 2 and len(spooled(config)) == 1
        assert not (state / "workshop.db").exists()
        daemon = subprocess.Popen([sys.executable, str(Path(__file__).parent / "webhook_daemon.py"),
                                   str(state), str(rules)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            first = until(lambda: launches(state), what="上线后补消费并起轮")
            log = Log.readonly(state / "workshop.db")
            try:
                kinds = {f["body"]["body"]["kind"] for f in log.facts() if f["source"] == "github"}
            finally:
                log.close()
            assert "label" in kinds
            done = subprocess.run([sys.executable, "-m", "workshop.cli", "--state-dir", str(state), "receipt",
                                   first[0]["token"], "--json", json.dumps({"outcome": "done", "summary": "看过"})],
                                  capture_output=True, text=True)
            assert done.returncode == 0, done.stderr

            def settled():
                log = Log.readonly(state / "workshop.db")
                try:
                    return (log.get("items", "alpha:315") or {}).get("state") == "等资料"
                finally:
                    log.close()
            until(settled, what="上一轮结算")
            before = len(launches(state))
            # 守护进程在跑：推送到达后马上起轮，不等常规的 60 秒周期。
            pushed = time.monotonic()
            assert post(port, comment_on_315(91000001, V7), "issue_comment")[0] == 202
            until(lambda: len(launches(state)) > before, timeout=10, what="推送叫醒守护进程")
            assert time.monotonic() - pushed < 10
        finally:
            daemon.send_signal(signal.SIGTERM)
            daemon.wait(10)


# ── spool：坏文件隔离；写事实表前后强杀 ───────────────────────────────

CRASH = """
import json, os, signal, sys
from pathlib import Path
from workshop.core.engine import Engine
from workshop.core.log import Log
from workshop.core.rules import Rules
from workshop.plugins.sources.webhook import WebhookSource
from workshop.registry import Registry

state, rules, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

class Crashing(WebhookSource):
    def poll(self, target, cursor):
        batch = super().poll(target, cursor)
        if mode == 'before-facts' and batch.events:
            os.kill(os.getpid(), signal.SIGKILL)  # 读完 spool、事实还没写
        return batch

    def forget(self, root, names):
        if mode == 'before-delete' and names:
            os.kill(os.getpid(), signal.SIGKILL)  # 事实和游标已提交、spool 还没删
        return super().forget(root, names)

registry = Registry()
registry.register('source', 'webhook', Crashing())
engine = Engine(Log(state / 'workshop.db'), Rules.validate(json.loads(rules.read_text())), registry)
engine.sources()
"""


def crash_run(state, rules, mode):
    return subprocess.run([sys.executable, "-c", CRASH, str(state), str(rules), mode], capture_output=True, text=True)


def test_bad_spool_file_is_quarantined_and_crash_points_neither_lose_nor_duplicate(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    config = hook(tmp_path)
    data = daemon_rules(config)
    data["targets"]["alpha"]["flows"]["flow"]["triggers"] = [{"source": "github", "action": "update"}]
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps(data))
    spool = Path(config["spool"])
    spool.mkdir(parents=True)
    (spool / "0000000000000000001-torn.json").write_text('{"event": "issues", "payload": {"acti')
    with entry(config) as port:
        assert post(port, comment_on_315(91000002, V5), "issue_comment")[0] == 202

    def facts():
        with sqlite3.connect(state / "workshop.db") as db:
            return db.execute("SELECT count(*) FROM facts WHERE source='github' AND kind='event'").fetchone()[0]

    # 读完 spool、写事实表前被强杀：不丢，重启后照常消费。
    killed = crash_run(state, rules, "before-facts")
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    # 读的时候坏文件已挪进隔离区，合法的那份还在 spool 里。
    assert facts() == 0 and len(spooled(config)) == 1
    assert [p.name for p in (spool / "quarantine").iterdir()] == ["0000000000000000001-torn.json"]
    assert crash_run(state, rules, "normal").returncode == 0
    assert facts() == 1
    assert len(spooled(config)) == 1  # 已消费的等下一次读取时删
    kept = spooled(config)[0].read_text()
    # 事实和游标已提交、删 spool 前被强杀：重启后只删不重放，不产生重复行。
    killed = crash_run(state, rules, "before-delete")
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert len(spooled(config)) == 1
    assert crash_run(state, rules, "normal").returncode == 0
    assert facts() == 1 and spooled(config) == []
    # 同一份投递再落一次（游标丢失或重投）：靠 (source, ext_id) 去重。
    (spool / "0000000000000000009-again.json").write_text(kept)
    assert crash_run(state, rules, "normal").returncode == 0
    assert facts() == 1


def test_next_tick_drains_spool_and_duplicate_push_is_deduplicated(world, tmp_path):
    config = hook(tmp_path)
    engine = engine_for(world, config)
    with entry(config) as port:
        for _ in range(2):  # 同一次变化推两次（重投的 delivery id 不同）
            assert post(port, labeled(), "issues")[0] == 202
        assert len(spooled(config)) == 2
        engine.tick()
    keys = [f["ext_id"] for f in github_facts(engine) if f["kind"] == "event"]
    assert len(keys) == len(set(keys)) == 2  # 标签一行、对象快照一行
    assert engine.log.get("items", "alpha:315")
    engine.tick()
    assert spooled(config) == []
    engine.log.close()


# ── 归一键：推拉逐字符一致，入库严格 1 行 ───────────────────────────────

def test_real_label_and_comment_get_identical_keys_from_push_and_poll(world, tmp_path):
    config = hook(tmp_path)
    issue168 = fixture("rest-issue-168.json")
    comment = fixture("rest-comment-6969514937.json")
    responses = {**responses_315(fixture("rest-issue-315.json")),
                 BASE + "/issues/168": issue168, BASE + "/issues/168/events?per_page=100": [],
                 BASE + "/issues/168/comments?per_page=100": [comment]}
    responses[BASE + "/issues?state=all&sort=updated&per_page=100"].append(issue168)
    edit(world, responses=responses)
    target = {**world["target"], "webhook": config}
    known = json.dumps({"cache": {}, "known": {"168": True}, "seen": {}})
    polled = GitHubSource(str(world["commands"] / "gh")).poll(target, known).events
    with entry(config) as port:
        assert post(port, fixture("webhook-issues-labeled-315.json"), "issues")[0] == 202
        assert post(port, fixture("webhook-issue_comment-created-168.json"), "issue_comment")[0] == 202
    pushed = WebhookSource().poll(target, None).events

    def pick(events, kind):
        return [(e.source, e.ext_id, e.action_id) for e in events if e.body["kind"] == kind]

    assert pick(pushed, "label") == pick(polled, "label") and len(pick(polled, "label")) == 1
    assert pick(pushed, "issue-comment") == pick(polled, "issue-comment") and len(pick(polled, "issue-comment")) == 1
    snapshot = [(e.ext_id, e.version) for e in polled if e.body["kind"] == "issue" and e.object_key == "315"]
    assert [(e.ext_id, e.version) for e in pushed if e.body["kind"] == "issue"] == snapshot
    for order in ((pushed, polled), (polled, pushed)):
        engine = engine_for(world, config)
        for batch in order:
            with engine.log.transaction():
                for event in batch:
                    engine.ingest(event)
        for _, key, action in pick(pushed, "label") + pick(pushed, "issue-comment"):
            rows = [f for f in github_facts(engine) if key in f["ext_id"] or action in f["ext_id"]]
            assert len(rows) == 1, rows
        engine.log.close()
        (world["root"] / "core" / "workshop.db").unlink()


def test_same_second_label_flaps_and_snapshot_correction(world, tmp_path):
    config = hook(tmp_path)
    engine = engine_for(world, config)
    at = fixture("rest-issue-315.json")["updated_at"]
    ready = fixture("rest-issue-315.json")["labels"]
    with entry(config) as port:
        # 同一秒：先到的是「去标签」那次的快照（标签为空），再是两次「加标签」。
        assert post(port, issue_change("unlabeled", labels=[]) | {"label": ready[0]}, "issues")[0] == 202
        for _ in range(2):
            assert post(port, labeled(), "issues")[0] == 202
        quiesce(engine)
        rows = [f for f in github_facts(engine) if f["body"]["body"]["kind"] in ("label", "unlabel")]
        assert len(rows) == 2  # 两次同标签加同秒合并；去与加不同行
        assert {f["body"]["body"]["kind"] for f in rows} == {"label", "unlabel"}
        item = engine.log.get("items", "alpha:315")
        assert item["source_state"]["labels"] == []  # 去重后的行数推不出最终状态
        # 轮询的当前快照校正：同键不再落行，但状态改成「有」。
        poll_into(engine, world, responses_315(fixture("rest-issue-315.json")))
        quiesce(engine)
        assert engine.log.get("items", "alpha:315")["source_state"]["labels"] == ["Ready for Agent"]
        assert engine.log.get("items", "alpha:315")["source_version"] == at
        # 评论创建后再编辑：两行。
        before = len(github_facts(engine))
        assert post(port, comment_on_315(91000003, V5), "issue_comment")[0] == 202
        assert post(port, comment_on_315(91000003, V6, body="改过的", action="edited"), "issue_comment")[0] == 202
        engine.tick()
    assert len(github_facts(engine)) - before == 2
    engine.log.close()


# ── 旧键迁移：别名表独立、可重建，旧数据重放不落行不起轮 ─────────────────────

def old_event(kind, number, identity, value, *, edited=False, initial=False):
    """升级前轮询插件产出的事件（键带响应摘要或 GitHub 事件号），照原公式拼。"""
    from workshop.core.models import Event
    signature = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    ext = f"{REPO}:{kind}:{number}:{identity}"
    actor, author = value.get("actor") or {}, value.get("user") or {}
    body = {"kind": kind, "repo": REPO, "number": number, "data": value,
            "actor": actor.get("login", ""), "author": author.get("login", ""),
            "label": (value.get("label") or {}).get("name", ""), "text": (value.get("body") or "").strip(),
            "edited": edited, "by_workshop": initial or actor.get("type") == "Bot" or author.get("type") == "Bot"}
    return Event("github", f"{ext}:{signature}", "alpha", number, f"{REPO} #{number}", body,
                 action_id=ext, previous_ids=[ext, f"{ext}:{signature}"])


def fact_rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT seq,kind,source,ext_id,item,body,at FROM facts ORDER BY seq").fetchall()


def alias_rows(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT source,ext_id,seq FROM fact_aliases ORDER BY source,ext_id").fetchall()


def test_old_keys_alias_without_touching_facts_and_rebuild_from_facts(world, tmp_path):
    config = hook(tmp_path)
    issue = fixture("rest-issue-315.json")
    label = next(e for e in fixture("rest-issue-315-events.json") if e["event"] == "labeled")
    comment = {**fixture("rest-comment-6969514937.json"), "issue_url": f"https://api.github.com/{BASE}/issues/315"}
    engine = engine_for(world, config, sources=("github", "webhook"))
    # 升级前的状态目录：旧格式 ext_id 的事实。
    with engine.log.transaction():
        for event in (old_event("label", "315", label["id"], label), old_event("issue", "315", "body", issue),
                      old_event("issue-comment", "315", comment["id"], comment)):
            engine.ingest(event)
    quiesce(engine)
    database = world["root"] / "core" / "workshop.db"
    history = fact_rows(database)
    olds = len([f for f in history if f[2] == "github"])
    launched = rounds(engine)
    assert engine.log.get("items", "alpha:315")["state"] == "等资料"
    edit(world, responses=responses_315(issue, comments=[comment]))
    comment_push = fixture("webhook-issue_comment-created-168.json") | {"issue": issue, "comment": comment}

    def replay(engine):
        engine.fact("cursor", {"id": "alpha:github", "cursor": None})  # 游标丢了，轮询从头再读一遍
        with entry(config) as port:
            assert post(port, fixture("webhook-issues-labeled-315.json"), "issues")[0] == 202
            assert post(port, comment_push, "issue_comment")[0] == 202
            quiesce(engine)
        assert len(github_facts(engine)) == olds
        assert rounds(engine) == launched

    # 升级：新代码打开同一状态目录。原有事实行零修改。
    engine = restart(engine, world, config, sources=("github", "webhook"))
    engine.refresh_aliases()
    assert fact_rows(database)[:len(history)] == history
    aliases = alias_rows(database)
    assert aliases
    replay(engine)
    assert fact_rows(database)[:len(history)] == history
    # 删掉别名表再重启：从事实表重新算出来，逐行一致；再次重放仍是 0。
    engine.log.close()
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE fact_aliases")
    engine = engine_for(world, config, sources=("github", "webhook"))
    engine.refresh_aliases()
    assert alias_rows(database) == aliases
    replay(engine)
    # 旧对象上一条真正的新变化：新增 1 行，正常起 1 轮。
    with entry(config) as port:
        assert post(port, comment_on_315(91000004, V7), "issue_comment")[0] == 202
        quiesce(engine)
    assert len(github_facts(engine)) == olds + 1
    assert rounds(engine) == launched + 1
    engine.log.close()


# ── 陈旧：留痕、不回拨、不空跑、不进批次、不推水位 ─────────────────────────

def test_stale_state_is_recorded_skipped_and_never_batched(world, tmp_path):
    config = hook(tmp_path)
    engine = engine_for(world, config)
    with entry(config) as port:
        assert post(port, labeled(), "issues")[0] == 202
        quiesce(engine)
        launched = rounds(engine)
        assert post(port, issue_change("closed", state="closed", updated_at=V6), "issues")[0] == 202
        quiesce(engine)
        assert rounds(engine) == launched + 1
        assert engine.log.get("items", "alpha:315")["source_state"]["state"] == "closed"
        # 重启守护进程后，经推送和轮询分别注入陈旧的 open v5。
        engine = restart(engine, world, config)
        before = len(engine.log.facts())
        launched = rounds(engine)
        assert post(port, issue_change("reopened", state="open", updated_at=V5), "issues")[0] == 202
        quiesce(engine)
        poll_into(engine, world, responses_315({**fixture("rest-issue-315.json"), "state": "open", "updated_at": V5}))
        quiesce(engine)
        stale = [f for f in engine.log.facts("alpha:315") if f["kind"] == "event" and f["body"].get("stale")]
        assert stale and len(engine.log.facts()) > before
        item = engine.log.get("items", "alpha:315")
        assert item["source_state"]["state"] == "closed" and item["source_version"] == V6
        assert rounds(engine) == launched
        # 同一件活上一条合法新事件：只起 1 轮，批次里只有它。
        assert post(port, comment_on_315(91000005, V7), "issue_comment")[0] == 202
        engine.tick()
        assert rounds(engine) == launched + 1
        row = engine.active()[0]
        comment_seq = next(f["seq"] for f in engine.log.facts("alpha:315")
                           if f["kind"] == "event" and f["body"]["body"]["kind"] == "issue-comment")
        assert row["batch"] == [comment_seq]
        brief = engine.log.brief(row)
        assert not {f["seq"] for f in stale} & {m.get("seq") for m in brief["materials"]}
        # 上一轮在跑时来了 seq14（普通）和 seq15（陈旧）。
        assert post(port, comment_on_315(91000006, V7), "issue_comment")[0] == 202
        assert post(port, issue_change("reopened", state="open", updated_at="2026-11-20T14:00:04Z"), "issues")[0] == 202
        engine.tick()
        events = [f for f in engine.log.facts("alpha:315") if f["kind"] == "event"]
        seq14 = next(f["seq"] for f in events if f["body"]["body"].get("data", {}).get("id") == 91000006)
        seq15 = next(f["seq"] for f in events if f["body"].get("stale") and f["seq"] > seq14)
        engine.submit(row["token"], {"outcome": "done", "summary": "看过"})
        engine.tick()
    item = engine.log.get("items", "alpha:315")
    assert item["watermark"] < seq14
    batches = [r["batch"] for r in engine.log.all("rounds")]
    assert batches[-1] == [seq14]
    assert not any(seq15 in batch for batch in batches)
    engine.log.close()


def test_late_comment_is_processed_without_rewinding_state_and_replay_is_deduplicated(world, tmp_path):
    config = hook(tmp_path)
    engine = engine_for(world, config)
    with entry(config) as port:
        assert post(port, labeled(), "issues")[0] == 202
        assert post(port, issue_change("closed", state="closed", updated_at=V6), "issues")[0] == 202
        quiesce(engine)
        launched = rounds(engine)
        late = comment_on_315(91000007, V5)
        assert post(port, late, "issue_comment")[0] == 202
        quiesce(engine)
        assert rounds(engine) == launched + 1  # 增量变更按自己的 id 处理，不因对象版本更新而丢
        assert engine.log.get("items", "alpha:315")["source_version"] == V6
        assert post(port, late, "issue_comment")[0] == 202
        quiesce(engine)
    assert rounds(engine) == launched + 1
    assert engine.log.get("items", "alpha:315")["source_version"] == V6
    engine.log.close()


def test_poll_still_delivers_when_push_entry_is_down(world, tmp_path):
    config = hook(tmp_path)
    engine = engine_for(world, config, sources=("github", "webhook"))
    edit(world, responses=responses_315(fixture("rest-issue-315.json")))
    engine.tick()  # 入口没起：spool 是空的，轮询照常取到变化
    assert spooled(config) == []
    assert {f["body"]["body"]["kind"] for f in github_facts(engine) if f["kind"] == "event"} >= {"label", "issue"}
    assert engine.log.get("items", "alpha:315")
    engine.log.close()


# ── 默认关闭、只听本机 ───────────────────────────────────────────────

def cli_rules(tmp_path, webhook):
    data = configuration()
    target = {"repo": REPO, "sources": {"github": {}}, "flows": {"memory": {"triggers": [], "stages": ["接单", "执行", "收尾"]}}}
    if webhook is not None:
        target["sources"]["webhook"] = {}
        target["webhook"] = webhook
    data["targets"] = {"alpha": target}
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def refused(host, port):
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return True
    return False


def test_off_by_default_and_loopback_only(tmp_path):
    from workshop.cli import main
    port = free_port()
    state = tmp_path / "state"
    off = cli_rules(tmp_path / "off", None)
    assert main(["--state-dir", str(state), "--rules", str(off), "webhook", "--target", "alpha"]) == 2
    assert refused("127.0.0.1", port) and not (state / "workshop.db").exists()
    exposed = cli_rules(tmp_path / "exposed", hook(tmp_path, listen="0.0.0.0", port=port))
    assert main(["--state-dir", str(state), "--rules", str(exposed), "webhook", "--target", "alpha"]) == 2
    assert refused("127.0.0.1", port)
    with pytest.raises(ValueError, match="loopback"):
        settings({"name": "alpha", "repo": REPO, "webhook": hook(tmp_path, listen="0.0.0.0")})
    six = hook(tmp_path / "six", listen="::1")
    with entry(six) as listening:
        assert post(listening, labeled(), "issues", host="::1")[0] == 202
    config = hook(tmp_path / "on", port=port)
    rules = cli_rules(tmp_path / "on", config)
    process = subprocess.Popen([sys.executable, "-m", "workshop.cli", "--state-dir", str(state), "--rules", str(rules),
                                "webhook", "--target", "alpha"], stderr=subprocess.PIPE, text=True)
    try:
        until(lambda: not refused("127.0.0.1", port) or process.poll() is not None, what="入口起来")
        assert process.poll() is None, process.stderr.read()
        assert post(port, labeled(), "issues")[0] == 202
        assert len(spooled(config)) == 1
    finally:
        process.terminate()
        process.wait(10)
    assert not (state / "workshop.db").exists()


def test_entry_never_imports_or_opens_the_fact_store(tmp_path):
    config = hook(tmp_path)
    script = f"""
import json, sys, threading, urllib.request, hmac, hashlib
from workshop.plugins.sources.webhook.config import settings
from workshop.plugins.sources.webhook.entry import serve
config = json.loads(sys.argv[1])
server = serve(settings({{'name': 'alpha', 'repo': '{REPO}', 'webhook': config}}))
threading.Thread(target=server.serve_forever, daemon=True).start()
body = b'{{"zen": "ok"}}'
request = urllib.request.Request(f'http://127.0.0.1:{{server.server_address[1]}}/', body, {{
    'X-GitHub-Event': 'ping', 'X-GitHub-Delivery': 'd-1',
    'X-Hub-Signature-256': 'sha256=' + hmac.new(sys.argv[2].encode(), body, hashlib.sha256).hexdigest()}})
status = urllib.request.urlopen(request, timeout=5).status
server.shutdown()
print(json.dumps({{'status': status, 'modules': sorted(m for m in sys.modules if m.startswith('workshop.core.') or m == 'sqlite3')}}))
"""
    result = subprocess.run([sys.executable, "-c", script, json.dumps(config), SECRET],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == 202 and len(spooled(config)) == 1
    assert "workshop.core.log" not in report["modules"] and "sqlite3" not in report["modules"]
    assert set(report["modules"]) <= {"workshop.core.models", "workshop.core.ports"}
    assert not list(tmp_path.rglob("*.db"))
