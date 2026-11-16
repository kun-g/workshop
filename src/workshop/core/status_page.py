"""把总览（report.overview）排成一页自包含的网页：纯规则排版，不调模型，离线能开。"""
from collections import Counter, defaultdict
from datetime import datetime
from html import escape as e

ENDED = ("完成", "放弃")
# 状态在页面里的先后和颜色类；没列到的状态（例如作业字样「等检查」）按在跑算。
ORDER = ["卡住", "等你", "跑着", "排队", "定时", "暂停", "完成", "放弃"]
TONE = {"卡住": "stuck", "等你": "wait", "跑着": "run", "排队": "timer", "定时": "timer", "暂停": "pause",
        "完成": "done", "放弃": "drop"}
# 下一步相同的活够这么多件，就并成一组，只列名字。
SAME_NEXT = 3


def tone(state):
    return TONE.get(state, "run")


def lane(row):
    return f'{row["target"]} · {row["flow"]}' if row["flow"] else row["target"]


def item(row, *, badge=True):
    pill = f'<span class="pill {tone(row["shown"])}">{e(row["shown"])}</span>' if badge else ""
    reason = row["reason"] if row["state"] == "卡住" else ""
    blocker = "" if row["blocker"] in ("—", reason) else row["blocker"]
    extra = "".join(f"<p><b>{k}</b>{e(v)}</p>" for k, v in (("原因", reason), ("卡在谁手里", blocker)) if v)
    return (f'<details class="item"><summary><span class="name">{e(row["title"])}{pill}</span>'
            f'<span class="meta">{e(row["stage"])} · {e(row["who"])}</span><span class="next">{e(row["next"])}</span></summary>'
            f'<div class="body"><p><b>活</b><code>{e(row["id"])}</code></p><p><b>下一步</b>{e(row["next"])}</p>{extra}</div></details>')


def block(rows, *, badge=True):
    """一个流程里的活：下一步相同的并成一组，其余逐条列。"""
    same = Counter(r["next"] for r in rows)
    parts, done = [], set()
    for r in rows:
        text = r["next"]
        if same[text] >= SAME_NEXT and text not in done:
            done.add(text)
            members = [x for x in rows if x["next"] == text]
            chips = "".join(f'<span class="chip">{e(x["title"])}</span>' for x in members)
            parts.append(f'<div class="same"><p><b>{len(members)} 件下一步相同</b>{e(text)}</p><div class="chips">{chips}</div>'
                         f'<details class="more"><summary>逐件展开</summary><div class="list">'
                         + "".join(item(x, badge=badge) for x in members) + "</div></details></div>")
        elif same[text] < SAME_NEXT:
            parts.append(item(r, badge=badge))
    return "".join(parts)


def section(title, rows, *, note="", badge=True, folded=False, warn=False):
    if not rows:
        return ""
    groups = defaultdict(list)
    for r in rows:
        groups[lane(r)].append(r)
    body = "".join(f'<div class="group{" warn" if warn else ""}"><h3>{e(name)}<em>{len(rs)}</em></h3><div class="list">{block(rs, badge=badge)}</div></div>'
                   for name, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])))
    head = f"{e(title)} {len(rows)} 件"
    if folded:
        return f'<details class="fold"><summary><h2>{head}</h2></summary>{body}</details>'
    return f'<h2>{head}</h2>' + (f'<p class="note">{e(note)}</p>' if note else "") + body


def chart(rows):
    lanes = defaultdict(Counter)
    for r in rows:
        lanes[lane(r)][r["shown"]] += 1
    if not lanes:
        return ""
    widest = max(sum(c.values()) for c in lanes.values())
    seen = sorted({s for c in lanes.values() for s in c}, key=lambda s: ORDER.index(s) if s in ORDER else 2.5)
    out = ['<div class="chart">']
    for name, c in sorted(lanes.items(), key=lambda kv: -sum(kv[1].values())):
        total = sum(c.values())
        segs = "".join(f'<span class="seg {tone(s)}" style="flex:{c[s]}" title="{e(s)} {c[s]}">{c[s] if c[s] / widest >= 0.04 else ""}</span>'
                       for s in seen if c[s])
        out.append(f'<div class="lane"><div class="lane-name">{e(name)}</div><div class="track"><div class="bar" style="width:{total / widest * 100:.1f}%">{segs}</div></div><div class="lane-total">{total}</div></div>')
    out.append('<div class="legend">' + "".join(f'<span><i class="seg {tone(s)}"></i>{e(s)}</span>' for s in seen) + "</div></div>")
    return "".join(out)


def page(view, now=None):
    rows = view["rows"]
    now = now or datetime.now().astimezone()
    stuck = [r for r in rows if r["stuck"]]
    waiting = [r for r in rows if r["shown"] == "等你" and not r["stuck"]]
    ended = [r for r in rows if r["state"] in ENDED]
    moving = [r for r in rows if r not in stuck and r not in waiting and r not in ended]
    counts = Counter("卡住" if r["stuck"] else r["shown"] for r in rows)
    filters = "；".join(f"{k} {'、'.join(v)}" for k, v in (("状态", view["filters"]["states"]), ("交付对象", view["filters"]["targets"]),
                                                          ("流程", view["filters"]["flows"])) if v)
    scope = ("全部活" if view["everything"] else "当前的活") + (f"，筛选：{filters}" if filters else "")
    hidden = (f"另有 {view['hidden']} 件没列出（{'筛掉的' if view['filtered'] else '已结束'}）" if view["hidden"] else "")
    kpis = "".join(f'<div class="kpi {tone(s)}"><b>{counts[s]}</b><span>{e(s)}</span></div>'
                   for s in sorted(counts, key=lambda s: ORDER.index(s) if s in ORDER else 2.5))
    lead = (f"要处理的是 <b>{len(stuck)} 件卡住</b>" if stuck else "没有卡住的活") + (f"、<b>{len(waiting)} 件等你</b>" if waiting else "") + "。"
    channel = "".join(
        [f'<p><b>读取失败</b><code>{e(c["item"])}</code>{e(c["reason"])}</p>' for c in view["unreadable"]]
        + [f'<p><b>等回答</b><code>{e(c["item"])}</code>{e(c["card"])} · {e(c["decision"])}</p>' for c in view["asks"]]
        + [f'<p><b>待对账</b><code>{e(c["item"])}</code>连续 3 次不能确定是否已发送，需检查通道<br><span class="note">卡号 {e(c["card"])}</span></p>'
           for c in view["uncertain"]])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>车间状态</title>
<style>{STYLE}</style></head>
<body><main>
<h1>车间状态</h1>
<p class="sub">{e(now.strftime("%Y-%m-%d %H:%M"))} · {e(scope)} · 列出 {len(rows)} 件{(" · " + e(hidden)) if hidden else ""}</p>
<p class="lead">{lead}</p>
<div class="kpis">{kpis}</div>
{chart(rows)}
{section("卡住", stuck, note="每件点开看原因和建议动作；下一步相同的并成一组。", badge=False, warn=True)}
{f'<h2>通道</h2><div class="card">{channel}</div>' if channel else ""}
{section("等你", waiting, badge=False)}
{section("在动", moving)}
{section("已结束", ended, folded=True)}
<p class="note foot">由 <code>workshop status --html</code> 生成，排版是固定规则，不经模型。</p>
</main></body></html>
"""


STYLE = """
:root{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--mute:#6b6a64;--line:#e3e1d8;--run:#2f6fde;--wait:#c07a00;--timer:#8a8fa0;
--pause:#7a5bb5;--stuck:#d2462f;--done:#3f9a5b;--drop:#bdbab0;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#17171a;--card:#212125;--ink:#ecebe6;--mute:#9d9b94;--line:#33333a;--timer:#6d7286;--drop:#55534d;color-scheme:dark}}
body{background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"PingFang SC","Helvetica Neue",sans-serif;margin:0}
main{max-width:960px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 8px;display:inline-block}
h3{margin:0 0 4px;font-size:14px}h3 em{font-style:normal;color:var(--mute);margin-left:6px;font-weight:400}
.sub,.note{color:var(--mute)}.sub{margin:0 0 16px}.note{font-size:12px;margin:0 0 8px}.foot{margin-top:40px}
.lead{font-size:16px;margin:0 0 16px}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 14px;min-width:76px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi span{color:var(--mute);font-size:12px}
.kpi.stuck b{color:var(--stuck)}.kpi.wait b{color:var(--wait)}.kpi.run b{color:var(--run)}
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
details.item>summary{display:grid;grid-template-columns:minmax(140px,260px) auto minmax(0,1fr);gap:4px 12px;align-items:baseline;padding:7px 0;cursor:pointer;list-style:none}
details>summary::-webkit-details-marker{display:none}
details.item>summary::before{content:"▸";position:absolute;margin-left:-14px;color:var(--mute);font-size:11px}
details.item[open]>summary::before{content:"▾"}
.name{font-weight:600;overflow-wrap:anywhere}.meta{color:var(--mute);font-size:12px;white-space:nowrap}
.next{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--mute)}
.pill{color:#fff;font-size:11px;border-radius:4px;padding:0 6px;margin-left:6px;font-weight:400}
.body{padding:2px 0 10px}.body p,.card p{margin:4px 0}
.body b,.card b,.same b{display:inline-block;min-width:52px;color:var(--mute);font-weight:500;margin-right:8px}
code{font:12px ui-monospace,Menlo,monospace;overflow-wrap:anywhere;margin-right:8px}
.same{border-top:1px solid var(--line);padding:7px 0}.same p{margin:0}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 2px}
.chip{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:1px 9px;background:var(--bg)}
details.more>summary,details.fold>summary{cursor:pointer;color:var(--mute);font-size:12px;list-style:none}
details.fold>summary{color:var(--ink);font-size:inherit}details.fold>summary::before{content:"▸ ";color:var(--mute)}
details.fold[open]>summary::before{content:"▾ "}
@media (max-width:600px){details.item>summary{grid-template-columns:1fr}.next{white-space:normal}.lane{grid-template-columns:80px 1fr 28px}}
"""
