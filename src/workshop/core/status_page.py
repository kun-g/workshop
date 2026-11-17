"""把总览（report.overview）排成一页自包含的网页：纯规则排版，不调模型，离线能开。

每件活占一行：标题 · 一句话原因 · 要谁做什么 · 等了多久；原文（下一步、原因、卡在谁手里）折叠在行下。
生成时间和「等了多久」带着时间戳，页面里的小脚本按打开时的本机时钟重算，页面放旧了看得出。
"""
import re
from collections import Counter, defaultdict
from datetime import datetime
from html import escape as e

ENDED = ("完成", "放弃")
# 状态在页面里的先后和颜色类；没列到的状态（例如作业字样「等检查」）按在跑算。
ORDER = ["卡住", "等你", "跑着", "排队", "定时", "暂停", "完成", "放弃"]
TONE = {"卡住": "stuck", "等你": "wait", "跑着": "run", "排队": "timer", "定时": "timer", "暂停": "pause",
        "完成": "done", "放弃": "drop"}
LIMIT = 40  # 一句话最多这么多个字
STALE_MINUTES = 10  # 页面放了这么久就标「可能已过期」


def tone(state):
    return TONE.get(state, "run")


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
    """一句话原因，和要谁做什么：只用活里已有的字段拼，缺了退回原文首句。"""
    state, stage = row["shown"], row["stage"]
    if row["stuck"]:
        owner = row.get("owner") or "项目经理"
        advice = row["advice"].removeprefix(owner).lstrip("：: ，,")  # 建议动作常以执行者开头，别写成「项目经理：项目经理…」
        return sentence(row["reason"] or "原因未记录"), f"{owner}：{sentence(advice, LIMIT - len(owner) - 1)}"
    if state == "等你":
        decision = asks.get(row["id"]) or row["next"]
        return f"{stage}阶段等你回答", f"Kun：{sentence(decision, LIMIT - 4)}"
    if state == "暂停":
        return f"交付对象已暂停，{stage}阶段不动", "项目经理：恢复后继续"
    if state in ENDED:
        return f"{state}，停在{stage}阶段", "无需处理"
    if state in ("定时", "排队"):
        return sentence(row["next"]), "车间：到点自动继续"
    return f"{stage}阶段进行中", f"{row['who']}：{sentence(row['next'], LIMIT - len(row['who']) - 1)}"


def item(row, asks, now, *, badge=True):
    reason, todo = brief(row, asks)
    pill = f'<span class="pill {tone(row["shown"])}">{e(row["shown"])}</span>' if badge else ""
    since = row.get("since") or ""
    origin = row.get("origin") or {}
    # 名字用来源资料的标题（「会话放行 workshop:184」这类登记名认不出是什么事）；链接放在简称上。
    title = origin.get("title") or row["title"]
    ref = (f'<a href="{e(origin["url"])}" onclick="event.stopPropagation()">{e(origin["ref"])}</a>' if origin.get("url") else e(origin.get("ref") or row["id"]))
    quote = f'<span class="quote">Kun 原话：{e(sentence(origin["quote"], 60))}</span>' if origin.get("quote") else ""
    raw = (("下一步", row["next"]), ("原因", row["reason"] if row["state"] == "卡住" else ""),
           ("卡在谁手里", "" if row["blocker"] in ("—", row["reason"]) else row["blocker"]),
           ("Kun 原话", origin.get("quote") or ""), ("等你回答", asks.get(row["id"], "")),
           ("流程", lane(row)), ("阶段", f'{row["stage"]} · {row["who"]}'), ("活", row["id"]))
    body = "".join(f"<p><b>{k}</b>{e(v)}</p>" for k, v in raw if v)
    return (f'<details class="item"><summary><span class="name">{e(title)}{pill}<span class="ref">{ref}</span>{quote}</span>'
            f'<span class="why">{e(reason)}</span><span class="todo">{e(todo)}</span>'
            f'<span class="since" data-since="{e(since)}">{e(waited(since, now))}</span></summary>'
            f'<div class="body">{body}</div></details>')


def section(title, rows, asks, now, *, note="", badge=True, folded=False, warn=False):
    if not rows:
        return ""
    longest = sorted(rows, key=lambda r: r.get("since") or "9")  # 等得最久的排前面；没起点的排最后
    head = f"{e(title)} {len(rows)} 件"
    body = f'<div class="group{" warn" if warn else ""}"><div class="cols"><span>标题</span><span>原因</span><span>要谁做什么</span><span>等了多久</span></div>' \
           + "".join(item(r, asks, now, badge=badge) for r in longest) + "</div>"
    if folded:
        return f'<details class="fold"><summary><h2>{head}</h2></summary>{body}</details>'
    return f'<h2>{head}</h2>' + (f'<p class="note">{e(note)}</p>' if note else "") + body


def chart(rows, key=lane):
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


def page(view, now=None):
    rows = view["rows"]
    now = now or datetime.now().astimezone()
    asks = {c["item"]: c["decision"] for c in view["asks"]}
    stuck = [r for r in rows if r["stuck"]]
    waiting = [r for r in rows if r["shown"] == "等你" and not r["stuck"]]
    ended = [r for r in rows if r["state"] in ENDED]
    moving = [r for r in rows if r not in stuck and r not in waiting and r not in ended]
    counts = Counter("卡住" if r["stuck"] else r["shown"] for r in rows if r["state"] not in ENDED)
    filters = "；".join(f"{k} {'、'.join(v)}" for k, v in (("状态", view["filters"]["states"]), ("交付对象", view["filters"]["targets"]),
                                                          ("流程", view["filters"]["flows"])) if v)
    scope = ("全部活" if view["everything"] else "当前的活") + (f"，筛选：{filters}" if filters else "")
    hidden = f"另有 {view['hidden']} 件被筛掉" if view["hidden"] and view["filtered"] else ""  # 已结束的件数不提
    total = f"活 {len([r for r in rows if r['state'] not in ENDED])} 件" + "".join(f" · {s} {counts[s]}" for s in sorted(counts, key=lambda s: ORDER.index(s) if s in ORDER else 2.5))
    channel = "".join(
        [f'<p><b>读取失败</b><code>{e(c["item"])}</code>{e(c["reason"])}</p>' for c in view["unreadable"]]
        + [f'<p><b>待对账</b><code>{e(c["item"])}</code>发给你的卡片连续 3 次不能确定飞书有没有送出，车间不敢重发，也不敢当没发<br><span class="note">卡号 {e(c["card"])}</span></p>'
           for c in view["uncertain"]])
    stamp = (f'<p class="stamp" id="stamp" data-at="{e(now.isoformat())}">更新于 <b>{e(now.strftime("%Y-%m-%d %H:%M:%S"))}</b>（<span id="ago">刚刚</span>）'
             '<span id="old" hidden>　可能已过期，以 <code>workshop status</code> 为准</span></p>')
    live = [r for r in rows if r["state"] not in ENDED]
    card = (f'<section class="card top"><p class="total">{e(total)}</p>' + stamp +
            '<div class="tabs"><input type="radio" name="by" id="by-target" checked><label for="by-target">按项目</label>'
            '<input type="radio" name="by" id="by-flow"><label for="by-flow">按流程</label>'
            f'<div class="pane p-target">{chart(live, lambda r: r["target"])}</div>'
            f'<div class="pane p-flow">{chart(live, lambda r: r["flow"])}</div></div></section>') if live else f'<p class="total">{e(total)}</p>' + stamp
    overall = chart(rows)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>车间状态</title>
<style>{STYLE}</style></head>
<body><main>
<h1>车间状态</h1>
{card}
<p class="sub">{e(scope)}{(" · " + e(hidden)) if hidden else ""}</p>
{section("卡住", stuck, asks, now, badge=False, warn=True)}
{section("等你", waiting, asks, now, badge=False)}
{section("在动", moving, asks, now)}
{f'<details class="fold"><summary><h2>按流程分布</h2></summary>{overall}</details>' if overall else ""}
{section("已结束", ended, asks, now, folded=True)}
{f'<details class="fold"><summary><h2>消息通道有问题（项目经理处理，你不用动）</h2></summary><div class="card"><p class="note">车间给你发卡片、读你的回答走的是飞书；这里列的是「发出去不确定到没到」「你答了读不到」两类。</p>{channel}</div></details>' if channel else ""}
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
  var old=ms>=stale*60000;s.classList.toggle("old",old);document.getElementById("old").hidden=!old}
 document.querySelectorAll(".since").forEach(function(el){
  var b=Date.parse(el.dataset.since);if(!isNaN(b)){el.textContent=span(Date.now()-b)}})}
tick();setInterval(tick,30000)})();
""" % STALE_MINUTES


STYLE = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a64;--line:#e3e1d8;--run:#2f6fde;--wait:#c07a00;--timer:#8a8fa0;
--pause:#7a5bb5;--stuck:#d2462f;--done:#3f9a5b;--drop:#bdbab0;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#17171a;--card:#212125;--ink:#ecebe6;--mute:#9d9b94;--line:#33333a;--timer:#6d7286;--drop:#55534d;color-scheme:dark}}
body{background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;margin:0}
main{max-width:960px;margin:0 auto;padding:28px 18px 60px}
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
details.item{border-top:1px solid var(--line)}
details.item>summary,.cols{display:grid;grid-template-columns:minmax(120px,1.2fr) minmax(0,1.6fr) minmax(0,1.6fr) 84px;gap:4px 12px;align-items:baseline}
details.item>summary{padding:7px 0;cursor:pointer;list-style:none}
.cols{color:var(--mute);font-size:12px;padding-bottom:4px}
.since{text-align:right;font-variant-numeric:tabular-nums;color:var(--mute)}
details>summary::-webkit-details-marker{display:none}
details.item>summary::before{content:"▸";position:absolute;margin-left:-14px;color:var(--mute);font-size:11px}
details.item[open]>summary::before{content:"▾"}
.name{font-weight:600;overflow-wrap:anywhere}.ref{margin-left:8px;font-weight:400;font-size:12px}.ref a{color:var(--run)}
.quote{display:block;font-weight:400;font-size:12px;color:var(--mute)}.why{color:var(--mute)}.todo{overflow-wrap:anywhere}
.pill{color:#fff;font-size:11px;border-radius:4px;padding:0 6px;margin-left:6px;font-weight:400}
.body{padding:2px 0 10px}.body p,.card p{margin:4px 0}
.body b,.card b{display:inline-block;min-width:52px;color:var(--mute);font-weight:500;margin-right:8px}
code{font:12px ui-monospace,Menlo,monospace;overflow-wrap:anywhere;margin-right:8px}
details.fold>summary{cursor:pointer;color:var(--mute);font-size:12px;list-style:none}
details.fold>summary{color:var(--ink);font-size:inherit}details.fold>summary::before{content:"▸ ";color:var(--mute)}
details.fold[open]>summary::before{content:"▾ "}
@media (max-width:600px){details.item>summary{grid-template-columns:1fr}.cols{display:none}.since{text-align:left}.lane{grid-template-columns:80px 1fr 28px}}
"""
