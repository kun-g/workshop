"""私密离线报告；反馈只序列化成 JSON，不解释命令或链接。"""
from datetime import datetime
from html import escape
import json


def h(value):
    return escape(str(value), quote=True)


def line(value):
    return ' '.join(str(value).split())


def blind_text(value):
    if isinstance(value, dict) and value.get('kind') == 'manual':
        places = '、'.join(f"{p['host']}/{p['service']}" for p in value['instances'])
        return f"{value['software']} · {places}：人工核对上游；{value['reason']}"
    if isinstance(value, dict) and value.get('version') == '版本不明':
        return f"{value['software']} · {value['host']}/{value['service']}：版本不明；{value['reason']}"
    return str(value)


def summary(chosen, versions, weekly, blind):
    rows = [('盯更新周报' if weekly else '盯更新日报') + f'：本次 {len(chosen)} 项更新。']
    if not any(j['verdict'] == '该升' for k in chosen for j in versions[k]['judgment']['instances']):
        rows.append('已完成判断的部署中没有该升的项。')
    for key in chosen:
        value = versions[key]
        judgments = {j['id']: j for j in value['judgment']['instances']}
        for place in value['software']['instances']:
            judge = judgments[place['id']]
            installed = '版本不明' if place['version'] == 'unknown' else place['version']
            rows.append(line(f"{value['software']['name']} · {place['host']}/{place['service']} · "
                             f"{installed} → {value['releases'][-1]['tag_name']} · "
                             f"{judge['verdict']}（{judge['priority']}）：{judge['reason']}"))
    if blind:
        rows.append('盲区：')
        rows.extend(line(blind_text(b)) for b in blind)
    rows.append('发布说明与逐处反馈见私密附件。')
    return '\n'.join(rows)


def render(report, versions, needs, now):
    cards = []
    count = 0
    for key in report['versions']:
        value = versions[key]
        soft = value['software']
        judged = {p['id']: p for p in value['judgment']['instances']}
        days = max(0, (now - datetime.fromisoformat(value.get('first_reported', now.isoformat()))).days)
        rows = []
        for place in soft['instances']:
            judge = judged[place['id']]
            decided = value.get('feedback', {}).get(place['id'], {})
            features = ''.join(f'<label>{h(f["summary"])}（在做的事：{h(f["direction"])}）'
                               f'<select data-feature="{h(f["id"])}"><option value="">未反馈</option>'
                               '<option>想用</option><option>用不上</option></select></label>'
                               for f in judge.get('features', []))
            links = ' '.join(f'<a href="{h(link)}" rel="noreferrer">发布说明原文</a>'
                             for link in judge['links'] if link.startswith('https://'))
            rows.append(f'''<section data-key="{h(key)}" data-instance="{h(place['id'])}">
<h3>{h(place['host'])} · {h(place['service'])}</h3>
<p>{h('版本不明' if place['version'] == 'unknown' else place['version'])} → {h(value['releases'][-1]['tag_name'])} · {h(judge['verdict'])} · {h(judge['priority'])}</p>
<p>{h(judge['reason'])}</p><p class="summary">{h(judge['summary'])}</p>
<p>依据：{h(', '.join(judge.get('references', [])))} · 版本读取：{h(place['read_at'])}</p>{links}
<p>上次反馈：{h(decided.get('decision', '未反馈'))}</p>
<label>本次反馈 <select data-decision><option value="">保留，未反馈</option><option>没问题</option><option>判错了</option><option>不用盯</option><option>升</option></select></label>
<label>补充一句 <input data-note maxlength="4000"></label>
<h4>可能用得上的新功能</h4>{features or '<p>本项没有</p>'}</section>''')
        content = f'<article><h2>{h(soft["name"])} · 已等 {days} 天</h2>{"".join(rows)}</article>'
        if all(r['verdict'] == '不用升' for r in judged.values()):
            content = '<details><summary>' + h(soft['name']) + '：不用升，展开明细及反馈</summary>' + content + '</details>'
            count += 1
        cards.append(content)
    need_cards = ''.join(f'''<section data-need="{h(n['id'])}"><h3>{h(n['statement'])}</h3>
<p>{h(n['owner'])} · 出处：{h(n['source'])}</p><select><option value="">未反馈，下周继续</option><option>确认</option><option>不要了</option></select></section>'''
                        for n in needs if n['id'] in report.get('needs', []))
    payload = json.dumps(report['id']).replace('<', '\\u003c')
    changes = '\n'.join(str(x) for x in report.get('changes', [])) or '本周没有清单改动'
    blind = '\n'.join(blind_text(x) for x in report.get('blind', [])) or '本次已登记的来源均已查成'
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>盯更新报告 · {h(report['id'])}</title><style>
body{{font:17px/1.6 system-ui;background:#f3f1eb;color:#202b31;max-width:960px;margin:40px auto;padding:0 20px}}
h1{{font-size:32px}}h2{{font-size:24px}}h3{{font-size:19px}}article,details{{background:white;padding:24px;margin:20px 0;border:1px solid #cfd6cf;border-radius:8px}}
section{{padding:14px 0;border-top:1px solid #ddd}}label{{display:block;margin:12px 0}}input,select,button,textarea{{font:inherit;padding:8px;max-width:100%}}input{{width:90%}}button{{background:#245b48;color:white;border:0;border-radius:4px;cursor:pointer}}
pre,.summary{{white-space:pre-wrap;overflow-wrap:anywhere}}footer{{position:sticky;bottom:0;background:#f3f1eb;padding:16px 0}}textarea{{width:95%;min-height:170px}}a{{color:#245b48}}
</style><h1>盯更新 · {'每周报告' if report['kind']=='weekly' else '紧急与重要更新'}</h1>
<p>{h(report['id'])} · 仅供 Kun 私密查看</p><p class="summary">{h(report['summary'])}</p>
<p>不用升：{count} 项。未反馈的排在前面；未选择的会带到下一份周报。</p>
{''.join(cards)}<h2>需要台账的新条目</h2>{need_cards or '<p>没有新条目</p>'}
<h2>清单变化</h2><pre>{h(changes)}</pre><h2>盲区</h2><pre>{h(blind)}</pre>
<footer><button id="copy">复制反馈</button><p id="status">点选后复制，在飞书里粘贴给 Ash。</p><textarea id="output" hidden readonly></textarea></footer>
<script>
document.getElementById('copy').onclick=async()=>{{
 document.querySelector('footer').style.position='static';
 const feedback={{schema:'watch-feedback/v1',report:{payload},items:[],needs:[]}};
 document.querySelectorAll('[data-key]').forEach(el=>{{
  const decision=el.querySelector('[data-decision]').value,note=el.querySelector('[data-note]').value;
  const features=Array.from(el.querySelectorAll('[data-feature]')).filter(x=>x.value).map(x=>({{id:x.dataset.feature,choice:x.value}}));
  if(decision||note||features.length)feedback.items.push({{key:el.dataset.key,instance:el.dataset.instance,decision,note,features}});
 }});
 document.querySelectorAll('[data-need]').forEach(el=>{{const choice=el.querySelector('select').value;if(choice)feedback.needs.push({{id:el.dataset.need,choice}})}});
 const output=document.getElementById('output');output.value=JSON.stringify(feedback,null,2);output.hidden=false;
 try{{await navigator.clipboard.writeText(output.value);document.getElementById('status').textContent='已复制，粘贴给 Ash 即可。'}}
 catch(e){{output.focus();output.select();document.getElementById('status').textContent='文字已选中，请手动复制后粘贴给 Ash。'}}
}};
</script></html>'''
