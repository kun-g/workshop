"""状态网页：每件一行、原文折叠、生成时间显眼、状态变了页面跟着重写（介入点 R6、核心 R1 补注、E4）。"""
import re
from datetime import datetime, timedelta, timezone

from workshop.reports.status_writer import PAGE_MAX_AGE, PAGE_MIN_GAP, StatusWriter
from workshop.reports.status_page import brief, page, sentence, waited
from conftest import event, finish

NOW = datetime(2026, 11, 16, 22, 50, 12, tzinfo=timezone(timedelta(hours=8)))
LONG = "grant-console 专用 App token 已能读 gc#174/179，但 workshop 专用 token 仍看不到该仓库；对专用测试单添加前置返回 HTTP 403。"
NEXT = "项目经理核对跨仓库关系调用所需的凭据范围或代执行路径，保持原单只读；补齐真实验收后再交 Kun 确认。"


def row(**extra):
    base = {"id": "workshop:184", "title": "会话放行 workshop:184", "target": "workshop", "flow": "issue_delivery",
            "stage": "执行", "state": "卡住", "shown": "卡住", "stuck": True, "who": "车间", "next": NEXT, "blocker": LONG,
            "reason": LONG, "advice": NEXT, "owner": "项目经理", "origin": {"title": "任务关系：车间认前置、总单和相关", "url": "https://github.com/kun-g/workshop/issues/184", "ref": "workshop#184", "quote": "我的意思是，车间里面的任务系统能不能加这个 relationships？"}, "since": (NOW - timedelta(minutes=95)).isoformat()}
    return {**base, **extra}


def view(*rows, asks=(), hidden=0):
    return {"rows": list(rows), "total": len(rows) + hidden, "ended": hidden, "hidden": hidden, "everything": False, "filtered": False,
            "unreadable": [], "asks": list(asks), "uncertain": [], "filters": {"states": [], "targets": [], "flows": []}}


def text(html):
    return re.sub(r"<style>.*?</style>|<script>.*?</script>", "", html, flags=re.S)


def summaries(html):
    return re.findall(r"<summary>(.*?)</summary>", html, flags=re.S)


def test_total_first_then_stamp_with_age_script():
    html = page(view(row(), row(id="a:1", title="跑", shown="跑着", state="跑着", stuck=False), hidden=3), NOW)
    body = text(html)
    assert body.index("活 2 件 · 卡住 1 · 跑着 1") < body.index("更新于 <b>2026-11-16 22:50:12</b>")
    assert "已结束" not in body and "没列出" not in body and "等你 0" not in body
    assert 'id="stamp" data-at="2026-11-16T22:50:12+08:00"' in html and "可能已过期" not in html and '刷新' not in html
    # 内嵌 data 图标仍可离线打开；其余自动加载外部资源的入口继续禁止。
    icons = re.findall(r'<link rel="icon" type="image/svg\+xml" href="data:image/svg\+xml,[^"<>]+">', html)
    assert len(icons) == 1
    offline = html.replace(icons[0], "")
    assert "stale=10" in html and not re.search(r"<link|<img|src=|@import|url\(", offline)


def test_top_card_has_project_and_flow_views():
    rows = [row(), row(id="g:1", target="grant-console", flow="issue_delivery", shown="跑着", state="跑着", stuck=False),
            row(id="w:2", flow="watch_updates", shown="定时", state="定时", stuck=False)]
    html = page(view(*rows), NOW)
    card = html[html.index('<section class="card top">'):html.index("</section>")]
    assert card.index("活 3 件") < card.index("更新于") < card.index("按项目") < card.index("按流程")
    target, flow = card.split('<div class="pane p-flow">')
    assert "grant-console" in target and "workshop" in target and "watch_updates" not in target
    assert "watch_updates" in flow and "issue_delivery" in flow
    assert "按流程分布" not in html  # 底部那张旧图已删，只留顶部卡片


def test_channel_problems_folded_at_end_with_explanation():
    broken = view(row(), asks=[])
    broken["uncertain"] = [{"item": "g:1", "card": "c-1"}]
    html = page(broken, NOW)
    assert "通道待对账" in html and "不敢重发" in html and html.index("通道待对账") > html.index("卡住 1 件")
    assert "消息通道" not in page(view(row()), NOW)


def test_one_line_per_item_title_and_next_step_with_wait_inside():
    html = page(view(row()), NOW)
    (line,) = [s for s in summaries(html) if "任务关系" in s]
    assert line.count('class="name"') == 1 and line.count('class="todo"') == 1 and 'class="why"' in line  # 卡住的活：下一步下面补一句原因
    assert "项目经理：" in line and "小时" not in line and NEXT not in line and LONG not in line
    assert re.search(r'<b>等了多久</b><span class="since"[^>]*>1 小时</span>', html[html.index(line):])  # 等了多久只在展开里
    assert '<span>下一步</span>' in html and "<span>原因</span>" not in html and "<span>要谁做什么</span>" not in html
    short = [len(v) for v in re.findall(r'class="(?:todo|why)">(?:项目经理：)?([^<]*)', line)]
    assert all(0 < n <= 76 for n in short)


def test_background_title_link_and_quote_on_the_line():
    html = page(view(row()), NOW)
    (line,) = [s for s in summaries(html) if "任务关系" in s]
    assert 'href="https://github.com/kun-g/workshop/issues/184"' in line and ">workshop#184</a>" in line
    assert "Kun 原话：我的意思是，车间里面的任务系统能不能加这个 relationships？" in line
    plain = page(view(row(origin=None)), NOW)  # 不是 issue 来的活：退回登记名，不出空链接
    assert "会话放行 workshop:184" in plain and "Kun 原话" not in plain.split('<div class="body">')[0]


def test_raw_text_folded_and_each_shown_once():
    html = page(view(row(blocker="另一个人手里")), NOW)
    body = html[html.index('<div class="body">'):]
    assert body.count(NEXT) == 1 and body.count(LONG) == 1 and "另一个人手里" in body and "workshop:184" in body
    assert html.count(NEXT) == 1  # 摘要里不再重复


def test_empty_blocks_not_shown_and_ended_folded_last():
    quiet = text(page(view(row(shown="跑着", state="跑着", stuck=False)), NOW))
    assert "<h2>卡住" not in quiet and "通道" not in quiet and "已结束" not in quiet
    done = row(id="a:2", title="好了", shown="完成", state="完成", stuck=False)
    html = page(view(row(), done), NOW)
    assert html.index("卡住 1 件") < html.index("已结束 1 件") and '<details class="fold"><summary><h2>已结束' in html


def test_missing_start_is_unknown_not_made_up():
    assert waited(None, NOW) == "时长未知" and waited("乱写", NOW) == "时长未知"
    assert waited((NOW - timedelta(seconds=20)).isoformat(), NOW) == "不到 1 分钟"
    assert waited((NOW - timedelta(minutes=59)).isoformat(), NOW) == "59 分钟"
    assert waited((NOW - timedelta(hours=26)).isoformat(), NOW) == "26 小时"


def test_brief_by_state():
    assert sentence("第一句。第二句") == "第一句" and len(sentence("长" * 80)) == 40 and sentence("长" * 80).endswith("…")
    todo, reason = brief(row(owner=""), {})
    assert todo.startswith("项目经理：") and not todo.startswith("项目经理：项目经理") and reason.startswith("grant-console")
    ask = row(id="g:1", shown="等你", state="等你", stuck=False, owner="")
    assert brief(ask, {"g:1": "是否批准 #175"}) == ("Kun：是否批准 #175", "")  # 等你、在动：只有下一步，没有原因
    assert brief(row(shown="跑着", state="跑着", stuck=False, next="等回执"), {}) == ("车间：等回执", "")
    assert brief(row(shown="暂停", state="跑着", stuck=False), {})[0].startswith("项目经理：")
    assert brief(row(shown="定时", state="定时", stuck=False, next="等反馈。别的"), {}) == ("车间：等反馈", "")
    for r in (ask, row(shown="定时", state="定时", stuck=False, next=NEXT), row()):
        assert all(len(part) <= 76 for part in brief(r, {"g:1": "很长" * 40}))


def test_page_rewritten_when_state_changes(engine, tmp_path):
    path = tmp_path / "status.html"
    writer = StatusWriter(engine.log.path, clock=engine.clock)
    engine.ingest(event())
    engine.tick()
    writer.refresh()
    first = path.read_text()
    assert "活 1 件" in text(first) and '<span class="stage">说清楚</span>' not in text(first)
    engine.clock.now += PAGE_MIN_GAP + timedelta(seconds=1)
    finish(engine, "说清楚")
    writer.refresh()
    assert '<span class="stage">说清楚</span>' in text(path.read_text())


def test_page_throttled_and_refreshed_when_old(engine, tmp_path):
    writer = StatusWriter(engine.log.path, clock=engine.clock)
    engine.ingest(event())
    engine.tick()
    path = tmp_path / "status.html"
    writer.refresh()
    stamp = path.read_text()
    finish(engine, "说清楚")  # 有变化，但离上次重写不到最小间隔：这拍不写
    writer.refresh()
    assert path.read_text() == stamp
    engine.clock.now += PAGE_MIN_GAP + timedelta(seconds=1)
    engine.tick()
    writer.refresh()
    assert path.read_text() != stamp
    settled = path.read_text()
    engine.clock.now += PAGE_MIN_GAP + timedelta(seconds=1)
    engine.tick()  # 没变化、没到最大间隔：不重写
    writer.refresh()
    assert path.read_text() == settled
    engine.clock.now += PAGE_MAX_AGE
    engine.tick()  # 没变化但放旧了：重写，生成时间更新
    writer.refresh()
    assert path.read_text() != settled


def test_moving_rows_show_stage_and_duration_columns():
    moving = row(shown="跑着", state="跑着", stuck=False, stage="执行", next="等约好的时间")
    html = page(view(moving), NOW)
    head = html[html.index("在动 1 件"):]
    assert "<span>标题</span><span>阶段</span><span>执行时长</span>" in head and "<span>下一步</span>" not in head
    (line,) = [s for s in summaries(head) if "任务关系" in s]
    assert '<span class="stage">执行</span>' in line and re.search(r'class="since"[^>]*>1 小时</span>', line) and 'class="todo"' not in line
    assert "<b>等了多久</b>" not in head  # 已经在行里，展开里不再重复


def test_timed_and_waiting_rows_get_their_own_sections():
    timed = row(id="t:2", shown="定时", state="定时", stuck=False, next="2026-11-19T03:40:00+00:00", about="回查线上版本", due="2026-11-19T03:40:00+00:00")
    soon = row(id="t:1", shown="定时", state="定时", stuck=False, next="x", about="先到点", due="2026-11-19T01:00:00+00:00")
    wait = row(id="w:1", shown="等前置", state="等前置", stuck=False, next="等前置：ws#174、ws#175")
    html = page(view(timed, soon, wait), NOW)
    assert "在动" not in html
    assert "<span>标题</span><span>在等哪些前置</span>" in html and "<span>标题</span><span>到点做什么</span><span>到点时间</span>" in html
    assert "回查线上版本" in html and html.index("先到点") < html.index("回查线上版本")  # 先到点的排前面
    todo = [s for s in re.findall(r'<span class="todo">([^<]*)', html)]
    assert "ws#174、ws#175" in todo and not any("等前置：" in t for t in todo)


def test_copy_id_button_on_every_row():
    html = page(view(row(), row(id="g:1", title="跑", shown="跑着", state="跑着", stuck=False, origin=None)), NOW)
    lines = summaries(html)
    assert all('class="copy"' in line for line in lines if "name" in line)
    assert 'data-copy="workshop:184"' in html and 'data-copy="g:1"' in html and "已复制" in html  # 点了不展开该行：脚本里 preventDefault
    assert "preventDefault" in html


def test_background_writer_delay_and_error_do_not_block_ticks(engine, tmp_path, monkeypatch, capsys):
    import builtins
    import threading
    from workshop.reports import status_writer

    entered, release, failed, written = [threading.Event() for _ in range(4)]
    original = status_writer.write_page
    calls = []

    def delayed_write(path, content):
        calls.append(path)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3), 'test did not release writer'
            raise RuntimeError('page failure')
        original(path, content)
        written.set()

    def error_print(*args, **kwargs):
        builtins.print(*args, **kwargs)
        failed.set()

    monkeypatch.setattr(status_writer, 'write_page', delayed_write)
    monkeypatch.setattr(status_writer, 'print', error_print, raising=False)
    engine.ingest(event())
    with StatusWriter(engine.log.path, engine.clock) as writer:
        try:
            assert entered.wait(3)
            engine.tick()
            engine.clock.now += timedelta(seconds=1)
            engine.tick()
            assert (tmp_path / 'heartbeat.json').exists()
            assert engine.log.all('rounds')
            assert not written.is_set()
        finally:
            release.set()
        assert failed.wait(3)
        assert 'page failure' in capsys.readouterr().err
        writer.wake()
        assert written.wait(3)
        assert '活 1 件' in (tmp_path / 'status.html').read_text()


def test_writer_notices_fact_without_engine_dirty_flag_and_preserves_page_on_error(engine, tmp_path, monkeypatch):
    import pytest
    from workshop.reports import status_writer
    engine.ingest(event())
    writer = StatusWriter(engine.log.path, engine.clock)
    writer.refresh()
    path = tmp_path / 'status.html'
    before = path.read_text()
    seq = writer.seq
    engine.log.append('work_blocked', {'state': '卡住', 'blocked_by': 'test blocker'}, 'alpha:one')
    engine.clock.now += PAGE_MIN_GAP
    original = status_writer.write_page
    monkeypatch.setattr(status_writer, 'write_page', lambda *args: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='disk full'):
        writer.refresh()
    assert path.read_text() == before and writer.seq == seq
    monkeypatch.setattr(status_writer, 'write_page', original)
    writer.refresh()
    assert 'test blocker' in path.read_text() and writer.seq > seq
    assert not list(tmp_path.glob('status.html.*.tmp'))


def test_unreadable_wait_falls_back_to_last_read_record():
    from workshop.reports.report import waiting_records

    class FakeLog:
        def __init__(self, waits):
            self.waits = waits

        def all(self, table):
            return self.waits

    class Service:
        broken = False

        def read(self, ref, config):
            if self.broken:
                raise OSError('Connection refused')
            return {'content': {'question': '看卡', 'recommend': '回过'}, 'started_at': '2026-11-19T01:00:00+00:00'}

    service = Service()
    plugins = type('P', (), {'get': lambda self, port, name: service})()
    log = FakeLog([{'ref': 'wait:1', 'item': 'a:1', 'provider': 'p', 'state': 'waiting'}])
    rules = {'wait_services': {'p': {}}}
    cache = {}
    (fresh,) = waiting_records(log, rules, plugins, cache=cache)
    assert fresh['question'] == '看卡' and 'stale' not in fresh and 'wait:1' in cache
    service.broken = True
    (old,) = waiting_records(log, rules, plugins, cache=cache)
    assert old['question'] == '看卡' and old['recommend'] == '回过' and old['stale'] and 'Connection refused' in old['stale_error']
    (none,) = waiting_records(log, rules, plugins, cache={})  # 没有旧数据：照旧写读取失败
    assert none['question'].startswith('等待服务读取失败')
    log.waits = []  # 不再等了：旧数据不留
    waiting_records(log, rules, plugins, cache=cache)
    assert cache == {}
