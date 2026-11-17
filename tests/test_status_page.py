"""状态网页：每件一行、原文折叠、生成时间显眼、状态变了页面跟着重写（介入点 R6、核心 R1 补注、E4）。"""
import re
from datetime import datetime, timedelta, timezone

from workshop.core.beat import PAGE_MAX_AGE, PAGE_MIN_GAP
from workshop.core.status_page import brief, page, sentence, waited
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
    assert 'id="stamp" data-at="2026-11-16T22:50:12+08:00"' in html and "可能已过期，以 <code>workshop status</code> 为准" in html
    assert "stale=10" in html and not re.search(r"<link|<img|src=|@import|url\(", html)  # 离线、不加载外部资源；来源链接是点了才去的


def test_top_card_has_project_and_flow_views():
    rows = [row(), row(id="g:1", target="grant-console", flow="issue_delivery", shown="跑着", state="跑着", stuck=False),
            row(id="w:2", flow="watch_updates", shown="定时", state="定时", stuck=False)]
    html = page(view(*rows), NOW)
    card = html[html.index('<section class="card top">'):html.index("</section>")]
    assert card.index("活 3 件") < card.index("更新于") < card.index("按项目") < card.index("按流程")
    target, flow = card.split('<div class="pane p-flow">')
    assert "grant-console" in target and "workshop" in target and "watch_updates" not in target
    assert "watch_updates" in flow and "issue_delivery" in flow
    assert html.index("按流程分布") > html.index("等你") if "等你 " in html else True  # 老的流程图留在最后


def test_channel_problems_folded_at_end_with_explanation():
    broken = view(row(), asks=[])
    broken["uncertain"] = [{"item": "g:1", "card": "c-1"}]
    html = page(broken, NOW)
    assert "消息通道有问题（项目经理处理，你不用动）" in html and "不敢重发" in html and html.index("消息通道有问题") > html.index("卡住 1 件")
    assert "消息通道" not in page(view(row()), NOW)


def test_one_line_per_item_with_four_short_fields():
    html = page(view(row()), NOW)
    (line,) = [s for s in summaries(html) if "任务关系" in s]
    fields = re.findall(r'class="(?:name|why|todo|since)"[^>]*>(.*?)(?:</span>(?=<span class="(?:why|todo|since)")|</span>$)', line)
    assert len(fields) == 4 and fields[0].startswith("任务关系：车间认前置、总单和相关") and fields[3] == "1 小时"
    assert "项目经理：" in fields[2]
    for value in fields[1:3]:
        assert 0 < len(value) <= 40 and "\n" not in value
    assert NEXT not in line and LONG not in line  # 摘要行里没有原文整段


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
    reason, todo = brief(row(owner=""), {})
    assert todo.startswith("项目经理：") and reason.startswith("grant-console") and not todo.startswith("项目经理：项目经理")
    ask = row(id="g:1", shown="等你", state="等你", stuck=False, owner="")
    assert brief(ask, {"g:1": "是否批准 #175"}) == ("执行阶段等你回答", "Kun：是否批准 #175")
    assert brief(row(shown="跑着", state="跑着", stuck=False, next="等回执"), {}) == ("执行阶段进行中", "车间：等回执")
    assert brief(row(shown="暂停", state="跑着", stuck=False), {})[1] == "项目经理：恢复后继续"
    for r in (ask, row(shown="定时", state="定时", stuck=False, next=NEXT)):
        assert all(len(part) <= 40 for part in brief(r, {"g:1": "很长" * 40}))


def test_page_rewritten_when_state_changes(engine, tmp_path):
    path = tmp_path / "status.html"
    engine.ingest(event())
    engine.tick()
    first = path.read_text()
    assert "活 1 件" in text(first) and "说清楚阶段" not in text(first)
    engine.clock.now += PAGE_MIN_GAP + timedelta(seconds=1)
    finish(engine, "说清楚")
    assert "说清楚阶段" in text(path.read_text())


def test_page_throttled_and_refreshed_when_old(engine, tmp_path):
    engine.ingest(event())
    engine.tick()
    path = tmp_path / "status.html"
    stamp = path.read_text()
    finish(engine, "说清楚")  # 有变化，但离上次重写不到最小间隔：这拍不写
    assert path.read_text() == stamp
    engine.clock.now += PAGE_MIN_GAP + timedelta(seconds=1)
    engine.tick()
    assert path.read_text() != stamp
    settled = path.read_text()
    engine.clock.now += PAGE_MIN_GAP + timedelta(seconds=1)
    engine.tick()  # 没变化、没到最大间隔：不重写
    assert path.read_text() == settled
    engine.clock.now += PAGE_MAX_AGE
    engine.tick()  # 没变化但放旧了：重写，生成时间更新
    assert path.read_text() != settled
