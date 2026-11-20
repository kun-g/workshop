from .manager_status import readable, sections
"""把总览（report.overview）排成一页自包含的网页：纯规则排版，不调模型，离线能开。

每件活占一行：标题 · 下一步（谁做什么；卡住的活下面补一句原因）；等了多久和原文（下一步、原因、卡在谁手里）折叠在行下。
生成时间和「等了多久」带着时间戳，页面里的小脚本按打开时的本机时钟重算，页面放旧了看得出。
"""
import json
import re
from collections import Counter, defaultdict
from importlib.resources import files
from urllib.parse import quote
from datetime import datetime
from html import escape as e
from .report import schedule_starved_line, source_isolation_line


ASSETS = files("workshop").joinpath("assets")
LOGO = ASSETS.joinpath("logo.svg").read_text(encoding="utf-8")
FAVICON = "data:image/svg+xml," + quote(ASSETS.joinpath("favicon.svg").read_text(encoding="utf-8"), safe="")

ENDED = ("完成", "放弃")
WAITING = ("等前置", "等子单", "等资料")  # 各自一栏，列头写在等什么
# 状态在页面里的先后和颜色类；没列到的状态（例如作业字样「等检查」）按在跑算。
ORDER = ["卡住", "等你", "跑着", "等前置", "等子单", "等资料", "排队", "定时", "暂停", "完成", "放弃"]
TONE = {"卡住": "stuck", "等你": "wait", "等前置": "timer", "等子单": "timer", "等资料": "timer", "跑着": "run", "排队": "timer", "定时": "timer", "暂停": "pause",
        "完成": "done", "放弃": "drop"}
LIMIT = 40  # 一句话最多这么多个字
STALE_MINUTES = 10  # 页面放了这么久，更新时间变红


def tone(state):
    return TONE.get(state, "run")


def due_at(row, field="due"):
    """到点时间，本机时区，写到分钟；活里没记就写「未记录」。"""
    try:
        return datetime.fromisoformat(row.get(field) or "").astimezone().strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "未记录"


def lane(row):
    return f'{row["target"]} · {row["flow"]}' if row["flow"] else row["target"]


def sentence(text, limit=LIMIT):
    """取第一句（到句号、分号或换行），超过 limit 个字就截断加省略号；不调模型，只切字。"""
    first = re.split(r"[。；;\n]", (text or "").strip(), maxsplit=1)[0].strip()
    return first if len(first) <= limit else first[:limit - 1] + "…"


def waited(since, now):
    """等了多久：「N 分钟」或「N 小时」；没有起点写「时长未知」，不拿别的时间冒充。"""
    try:
        minutes = int((now - datetime.fromisoformat(since)).total_seconds() // 60)
    except (TypeError, ValueError):
        return "时长未知"
    return "不到 1 分钟" if minutes < 1 else f"{minutes} 分钟" if minutes < 60 else f"{minutes // 60} 小时"


def brief(row, asks):
    """下一步（谁做什么）和补一句的原因：只有卡住的活才有原因可说；只用活里已有的字段拼，缺了退回原文首句。"""
    state = row["shown"]
    wide = 2 * LIMIT - 4
    if row["stuck"]:
        owner = row.get("owner") or "项目经理"
        advice = row["advice"].removeprefix(owner).lstrip("：: ，,")  # 建议动作常以执行者开头，别写成「项目经理：项目经理…」
        when = due_at(row, "retry_at") if row.get("auto_retry") else ""
        suffix = f"（下次 {when}）" if when and when != "未记录" else ""
        return f"{owner}：{sentence(advice, wide - len(owner) - 1 - len(suffix))}{suffix}", sentence(row["reason"] or "原因未记录", wide)
    if state == "等你":
        return f"Kun：{sentence(asks.get(row['id']) or row['next'], wide - 4)}", ""
    if state == "暂停":
        return "项目经理：恢复已暂停的交付对象", ""
    if state in ENDED:
        return "无需处理", ""
    if state in WAITING:  # 栏名已经写了在等什么，行里不再重复「车间：等前置：」
        return sentence(row["next"].removeprefix(state).lstrip("：: "), wide), ""
    if state == "定时":  # next 有时是到点的时间戳，说做什么的是工序登记的 about
        return f"车间：{sentence(row.get('about') or row['next'], wide - 3)}", ""
    if state == "排队":
        return f"车间：{sentence(row['next'], wide - 3)}", ""
    return f"{row['who']}：{sentence(row['next'], wide - len(row['who']) - 1)}", ""


def item(row, asks, now, *, badge=True, running=False, due=False):
    todo, reason = brief(row, asks)
    pill = f'<span class="pill {tone(row["shown"])}">{e(row["shown"])}</span>' if badge else ""
    since = row.get("since") or ""
    origin = row.get("origin") or {}
    # 名字用来源资料的标题（「会话放行 workshop:184」这类登记名认不出是什么事）；链接放在简称上。
    title = origin.get("title") or row["title"]
    ref = (f'<a href="{e(origin["url"])}" onclick="event.stopPropagation()">{e(origin["ref"])}</a>' if origin.get("url") else e(origin.get("ref") or row["id"]))
    quote = f'<span class="quote">Kun 原话：{e(sentence(origin["quote"], 60))}</span>' if origin.get("quote") else ""
    clock = f'<span class="since" data-since="{e(since)}">{e(waited(since, now))}</span>'
    raw = (("" if running else "等了多久", clock),
           ("下一步原文", row["next"]), ("原因", row["reason"] if row["state"] == "卡住" else ""),
           ("卡在谁手里", "" if row["blocker"] in ("—", row["reason"]) else row["blocker"]),
           ("Kun 原话", origin.get("quote") or ""), ("等你回答", asks.get(row["id"], "")),
           ("优先级", row.get("priority", "P1")), ("等待", row.get("wait_detail", "")), ("流程", lane(row)), ("" if running else "阶段", f'{row["stage"]} · {row["who"]}'), ("活", row["id"]))
    body = "".join(f"<p><b>{k}</b>{v if k == '等了多久' else e(v)}</p>" for k, v in raw if k and v)
    return (f'<details class="item"><summary><span class="name">{e(title)}{pill}<span class="ref">{ref}</span><button type="button" class="copy" data-copy="{e(row["id"])}" title="复制 {e(row["id"])}">复制 ID</button>{quote}</span>'
            + (f'<span class="stage">{e(row["stage"])}</span>{clock}' if running else
               f'<span class="todo">{e(todo)}' + (f'<span class="why">{e(reason)}</span>' if reason else "") + '</span>'
               + (f'<span class="due">{e(due_at(row))}</span>' if due else ""))
            + '</summary>'
            + f'<div class="body">{body}</div></details>')


def section(title, rows, asks, now, *, head2="下一步", note="", badge=True, folded=False, warn=False, running=False, due=False):
    if not rows:
        return ""
    longest = sorted(rows, key=lambda r: (r.get("due") if due else r.get("since")) or "9")  # 等得最久的（定时的是先到点的）排前面；没起点的排最后
    head = f"{e(title)} {len(rows)} 件"
    body = f'<div class="group{" warn" if warn else ""}{" running" if running or due else ""}"><div class="cols"><span>标题</span>' + ("<span>阶段</span><span>执行时长</span>" if running else f"<span>{e(head2)}</span>" + ("<span>到点时间</span>" if due else "")) + '</div>' \
           + "".join(item(r, asks, now, badge=badge, running=running, due=due) for r in longest) + "</div>"
    if folded:
        return f'<details class="fold"><summary><h2>{head}</h2></summary>{body}</details>'
    return f'<h2>{head}</h2>' + (f'<p class="note">{e(note)}</p>' if note else "") + body


def chart(rows, key):
    """每组一根横条，按状态分段上色；key 决定怎么分组。"""
    groups = defaultdict(Counter)
    for r in rows:
        groups[key(r) or "（无）"]["卡住" if r["stuck"] else r["shown"]] += 1
    if not groups:
        return ""
    widest = max(sum(c.values()) for c in groups.values())
    seen = sorted({s for c in groups.values() for s in c}, key=lambda s: ORDER.index(s) if s in ORDER else 2.5)
    out = ['<div class="chart">']
    for name, c in sorted(groups.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        segs = "".join(f'<span class="seg {tone(s)}" style="flex:{c[s]}" title="{e(s)} {c[s]}">{c[s] if c[s] / widest >= 0.04 else ""}</span>'
                       for s in seen if c[s])
        out.append(f'<div class="lane"><div class="lane-name">{e(name)}</div><div class="track"><div class="bar" style="width:{total / widest * 100:.1f}%">{segs}</div></div><div class="lane-total">{total}</div></div>')
    out.append('<div class="legend">' + "".join(f'<span><i class="seg {tone(s)}"></i>{e(s)}</span>' for s in seen) + "</div></div>")
    return "".join(out)


def manager_sections(view):
    """每条记录一行，详情跟着自己那一条折叠；没有记录时只有「没有」。lines 和 details 一一对应（「没有」占位时 details 为空）。"""
    out = []
    for h, v in sections(view).items():
        details = v['details'] if len(v['details']) == len(v['lines']) else [''] * len(v['lines'])
        rows = ''.join('<div class="rec"><p>' + e(line) + '</p>'
                       + ('<details><summary>详情</summary>' + ''.join('<p><b>' + e(k) + '</b>' + e(v) + (f'<button type="button" class="copy" data-copy="{e(v)}" title="复制 {e(v)}">复制</button>' if k in ('事项', '等待号') else '') + '</p>' for k, v in readable(json.loads(d))) + '</details>' if d else '')
                       + '</div>' for line, d in zip(v['lines'], details))
        out.append('<h2 id="' + e(h) + '">' + e(h) + '</h2><div class="card mgr">' + rows + '</div>')
    return ''.join(out)


def page(view, now=None):
    rows = view["rows"]
    now = now or datetime.now().astimezone()
    asks = {}
    for wait in view.get('waits', []):
        asks[wait['item']] = wait['question'] + '；推荐：' + wait['recommend']
        for row in rows:
            if row['id'] == wait['item']:
                row['since'] = wait.get('record', {}).get('started_at', row.get('since'))
                row['wait_detail'] = wait['ref'] + '；版本：' + wait.get('content', {}).get('version', '—') + '；原因：' + wait['reason']
    stuck = [r for r in rows if r["stuck"]]
    waiting = [r for r in rows if r["shown"] == "等你" and not r["stuck"]]
    ended = [r for r in rows if r["state"] in ENDED]
    others = [r for r in rows if r not in stuck and r not in waiting and r not in ended]
    by_state = {s: [r for r in others if r["shown"] == s] for s in ("排队", *WAITING, "定时")}
    moving = [r for r in others if not any(r in rs for rs in by_state.values())]
    counts = Counter("卡住" if r["stuck"] else r["shown"] for r in rows if r["state"] not in ENDED)
    filters = "；".join(f"{k} {'、'.join(v)}" for k, v in (("状态", view["filters"]["states"]), ("交付对象", view["filters"]["targets"]),
                                                          ("流程", view["filters"]["flows"])) if v)
    scope = ("全部活" if view["everything"] else "当前的活") + (f"，筛选：{filters}" if filters else "")
    hidden = f"另有 {view['hidden']} 件被筛掉" if view["hidden"] and view["filtered"] else ""  # 已结束的件数不提
    total = f"活 {len([r for r in rows if r['state'] not in ENDED])} 件" + "".join(f" · {s} {counts[s]}" for s in sorted(counts, key=lambda s: ORDER.index(s) if s in ORDER else 2.5))
    channel = "".join(f'<p><b>停了新派发</b><code>{e(h["target"])}</code>{e(h["reason"])}</p>' for h in view.get("holds", []))
    isolated = ''.join(f'<p>{e(source_isolation_line(c))}</p>' for c in view.get('isolated_sources', []))
    starved = ''.join(f'<p>{e(schedule_starved_line(r))}</p>' for r in view.get('starved_schedules', []))
    stamp = (f'<p class="stamp" id="stamp" data-at="{e(now.isoformat())}">更新于 <b>{e(now.strftime("%Y-%m-%d %H:%M:%S"))}</b>（<span id="ago">刚刚</span>）</p>')
    live = [r for r in rows if r["state"] not in ENDED]
    card = (f'<section class="card top"><p class="total">{e(total)}</p>' + stamp +
            '<div class="tabs"><input type="radio" name="by" id="by-target" checked><label for="by-target">按项目</label>'
            '<input type="radio" name="by" id="by-flow"><label for="by-flow">按流程</label>'
            f'<div class="pane p-target">{chart(live, lambda r: r["target"])}</div>'
            f'<div class="pane p-flow">{chart(live, lambda r: r["flow"])}</div></div></section>') if live else f'<p class="total">{e(total)}</p>' + stamp
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>车间状态</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<style>{STYLE}</style></head>
<body><main>
<h1 class="brand"><span class="brand-mark" aria-hidden="true">{LOGO}</span>车间状态</h1>
{card}
<p class="sub">{e(scope)}{(" · " + e(hidden)) if hidden else ""}</p>
{section("卡住", stuck, asks, now, badge=False, warn=True)}
{f'<h2>来源隔离</h2><div class="card">{isolated}</div>' if isolated else ''}
{f'<h2>周期任务停摆</h2><div class="card">{starved}</div>' if starved else ''}
{manager_sections(view)}
{section("在动", moving, asks, now, running=True)}
{section("排队", by_state["排队"], asks, now, badge=False)}
{section("等前置", by_state["等前置"], asks, now, badge=False, head2="在等哪些前置")}
{section("等子单", by_state["等子单"], asks, now, badge=False, head2="在等哪些子单")}
{section("等资料", by_state["等资料"], asks, now, badge=False, head2="在等什么资料")}
{section("定时", by_state["定时"], asks, now, badge=False, head2="到点做什么", due=True)}
{section("已结束", ended, asks, now, folded=True)}
{f'<h2>停了新派发</h2><div class="card"><p class="note">这些交付对象不派新的轮；在跑的轮、程序作业和等待都保留。</p>{channel}</div>' if channel else ""}
<p class="note foot">由 <code>workshop status --html</code> 生成，排版是固定规则，不经模型。</p>
</main><script>{SCRIPT}</script></body></html>
"""


SCRIPT = """
(function(){
var stale=%d;
function span(ms){var m=Math.floor(ms/60000);return m<1?"不到 1 分钟":m<60?m+" 分钟":Math.floor(m/60)+" 小时"}
function tick(){
 var s=document.getElementById("stamp"),t=Date.parse(s.dataset.at),ms=Date.now()-t;
 if(!isNaN(t)){document.getElementById("ago").textContent=(ms<60000?"刚刚":span(ms)+"前");
  s.classList.toggle("old",ms>=stale*60000)}
 document.querySelectorAll(".since").forEach(function(el){
  var b=Date.parse(el.dataset.since);if(!isNaN(b)){el.textContent=span(Date.now()-b)}})}
tick();setInterval(tick,30000);
document.addEventListener("click",function(ev){
 var b=ev.target.closest&&ev.target.closest("button.copy");if(!b)return;
 ev.preventDefault();ev.stopPropagation();
 function done(){var old=b.textContent;b.textContent="已复制";setTimeout(function(){b.textContent=old},1200)}
 function fallback(){var t=document.createElement("textarea");t.value=b.dataset.copy;document.body.appendChild(t);t.select();
  try{document.execCommand("copy");done()}catch(e){}document.body.removeChild(t)}
 if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(b.dataset.copy).then(done,fallback)}else{fallback()}
},true)})();
""" % STALE_MINUTES


STYLE = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a64;--line:#e3e1d8;--run:#2f6fde;--wait:#c07a00;--timer:#8a8fa0;
--pause:#7a5bb5;--stuck:#d2462f;--done:#3f9a5b;--drop:#bdbab0;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#17171a;--card:#212125;--ink:#ecebe6;--mute:#9d9b94;--line:#33333a;--timer:#6d7286;--drop:#55534d;color-scheme:dark}}
body{background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;margin:0}
main{max-width:960px;margin:0 auto;padding:28px 18px 60px}
.brand{display:flex;align-items:center;gap:8px}.brand-mark{display:inline-flex;flex:0 0 40px;width:40px;height:40px}.brand-mark svg{width:100%;height:100%}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;display:inline-block}
h3{margin:0 0 4px;font-size:14px}h3 em{font-style:normal;color:var(--mute);margin-left:6px;font-weight:400}
.sub,.note{color:var(--mute)}.sub{margin:0 0 16px}.note{font-size:12px;margin:0 0 8px}.foot{margin-top:40px}
.total{font-size:20px;font-weight:600;margin:0 0 6px}
.tabs>input{position:absolute;opacity:0}.tabs>label{display:inline-block;padding:3px 14px;margin:6px 6px 6px 0;border:1px solid var(--line);border-radius:999px;cursor:pointer;color:var(--mute);font-size:13px}
#by-target:checked~label[for=by-target],#by-flow:checked~label[for=by-flow]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.pane{display:none}#by-target:checked~.p-target,#by-flow:checked~.p-flow{display:block}.top .chart{border:0;padding:0;margin:0}
.stamp{font-size:15px;margin:0 0 6px}.stamp b{font-variant-numeric:tabular-nums}.stamp.old{color:var(--stuck);font-weight:600}
.chart,.card,.group{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 16px;margin:10px 0}
.group.warn{border-left:4px solid var(--stuck)}
.lane{display:grid;grid-template-columns:150px 1fr 36px;align-items:center;gap:10px;margin:6px 0}
.lane-name{color:var(--mute);text-align:right;font-size:13px;overflow-wrap:anywhere}.lane-total{color:var(--mute);font-variant-numeric:tabular-nums}
.bar{display:flex;height:22px;border-radius:5px;overflow:hidden;gap:2px}
.seg{display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;min-width:3px}
.stuck.seg,.pill.stuck{background:var(--stuck)}.wait.seg,.pill.wait{background:var(--wait)}.run.seg,.pill.run{background:var(--run)}
.timer.seg,.pill.timer{background:var(--timer)}.pause.seg,.pill.pause{background:var(--pause)}
.done.seg,.pill.done{background:var(--done)}.drop.seg,.pill.drop{background:var(--drop);color:var(--ink)}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:12px;color:var(--mute)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.list{padding-left:14px}
.mgr .rec{padding:8px 0;border-top:1px solid var(--line)}.mgr .rec:first-child{border-top:0}.mgr .rec p{margin:0}
.mgr details p{margin:4px 0 0;font-size:13px}.mgr details b{min-width:7em}
.mgr details{margin-top:4px}.mgr summary{cursor:pointer;color:var(--mute);font-size:12px}
details.item{border-top:1px solid var(--line)}
details.item>summary,.cols{display:grid;grid-template-columns:minmax(160px,1fr) minmax(0,1.6fr);gap:4px 12px;align-items:baseline}
details.item>summary{padding:7px 0;cursor:pointer;list-style:none}
.cols{color:var(--mute);font-size:12px;padding-bottom:4px}
.since{font-variant-numeric:tabular-nums}
details>summary::-webkit-details-marker{display:none}
details.item>summary::before{content:"▸";position:absolute;margin-left:-14px;color:var(--mute);font-size:11px}
details.item[open]>summary::before{content:"▾"}
.name{font-weight:600;overflow-wrap:anywhere}.copy{margin-left:8px;font:inherit;font-size:11px;font-weight:400;padding:0 8px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--mute);cursor:pointer}.copy:hover{color:var(--ink)}
.ref{margin-left:8px;font-weight:400;font-size:12px}.ref a{color:var(--run)}
.quote{display:block;font-weight:400;font-size:12px;color:var(--mute)}.todo{overflow-wrap:anywhere}.why{display:block;color:var(--mute);font-size:12px}
.pill{color:#fff;font-size:11px;border-radius:4px;padding:0 6px;margin-left:6px;font-weight:400}
.body{padding:2px 0 10px}.body p,.card p{margin:4px 0}
.body b,.card b{display:inline-block;min-width:52px;color:var(--mute);font-weight:500;margin-right:8px}
code{font:12px ui-monospace,Menlo,monospace;overflow-wrap:anywhere;margin-right:8px}
details.fold>summary{cursor:pointer;color:var(--mute);font-size:12px;list-style:none}
details.fold>summary{color:var(--ink);font-size:inherit}details.fold>summary::before{content:"▸ ";color:var(--mute)}
details.fold[open]>summary::before{content:"▾ "}
.running details.item>summary,.running .cols{grid-template-columns:minmax(160px,1fr) minmax(0,1fr) 8em}.since{color:var(--ink)}.due{font-variant-numeric:tabular-nums}
@media (max-width:600px){details.item>summary,.running details.item>summary{grid-template-columns:1fr}.cols{display:none}.lane{grid-template-columns:80px 1fr 28px}}
"""
