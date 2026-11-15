"""按认可样例输出私密离线报告：结论、要点、部署反馈。"""
from html import escape
from pathlib import Path

from .model import UPGRADE_ORDER, point_link
from .presentation import software_views, targets


def h(value):
    return escape(str(value), quote=True)


def line(value):
    return ' '.join(str(value).split())


def count_instances(view):
    servers, agents = len(view['servers']), len(view['agents'])
    return (f'{servers} 个实例' if servers else '') + ((' · ' if servers else '') + f'agent {agents} 处' if agents else '')


def blind_spots(chosen, versions, blind):
    """把版本缺失补进报告；同一部署只计一次，日报和周报共用。"""
    rows = list(blind)
    for view in software_views({'versions': chosen}, versions):
        for place in view['servers'] + view['agents']:
            if place['version'] == 'unknown':
                rows.append({'software': view['name'], 'host': place['host'], 'service': place['service'],
                             'version': '版本不明', 'reason': place['error']})
        if view['agents'] and not view['servers']:
            rows.append({'name': view['name'], 'reason': '服务端尚未核实，agent 暂不能判断'})
    result, seen = [], set()
    for value in rows:
        identity = (value.get('software'), value.get('host'), value.get('service')) if value.get('version') == '版本不明' else blind_text(value, versions)
        if identity not in seen:
            result.append(value)
            seen.add(identity)
    return result


def summary(chosen, versions, weekly, blind):
    views = software_views({'versions': chosen}, versions)
    rows = ['盯更新周报' if weekly else '盯更新日报']
    for view in views:
        rows.append(line(f"{view['name']} · {view['installed']}→{view['latest']} · {view['lead']['verdict']} · {count_instances(view)}"))
    if not any(v['lead']['verdict'] == '该升' for v in views):
        rows.append('本周没有该升的项。' if weekly else '没有该升的项。')
    rows.append(f'盲区 {len(blind_spots(chosen, versions, blind))} 项')
    return '\n'.join(rows)


def blind_text(value, versions):
    if not isinstance(value, dict):
        return str(value)
    if value.get('judgment_pending'):
        key = value['judgment_pending']
        return versions.get(key, {}).get('software', {}).get('name', key.rsplit('-', 1)[0]) + '：判断未完成'
    if value.get('kind') == 'manual':
        return value['software'] + '：' + value['reason']
    if value.get('version') == '版本不明':
        return f"{value['software']} · {value['host']} · {value['service']}：版本不明，{value['reason']}"
    return str(value.get('name') or value.get('id') or '') + '：' + str(value.get('reason') or value.get('evidence') or '尚未查全')


def render(report, versions, needs, now):
    views = software_views(report, versions)
    registry = report.get('feedback_targets') or targets(report, versions, needs)

    def row(identity, target, label=None):
        buttons = ''.join(f'<button type="button" aria-pressed="false">{h(c)}</button>' for c in target['choices'])
        note = '<input class="note" hidden maxlength="4000" placeholder="想说的话（可不填）" aria-label="补充说明">' if target['kind'] == 'instances' else ''
        return f'<div class="row" data-feedback="{h(identity)}" data-label="{h(target["label"])}"><span class="where">{label or h(target["label"])}</span><span class="opts">{buttons}</span>{note}</div>'

    def point(value, releases):
        # 升级顺序是程序补的操作约束，不伪装成某版发布说明的摘录。
        if value['text'] == UPGRADE_ORDER:
            return '<li>' + h(value['text']) + '</li>'
        return f'<li><a href="{h(point_link(value["version"], releases))}" rel="noreferrer">{h(value["version"])}</a> {h(value["text"])}</li>'

    up, wait, unseen = [], [], []
    rendered_unknown = set()
    for view in views:
        lead = view['lead']
        is_up = lead['verdict'] == '该升'
        instance_rows = []
        for identity, target in registry.items():
            if target.get('software') != view['id'] or target['kind'] != 'instances':
                continue
            if target['role'] == 'agent':
                hosts = '、'.join(dict.fromkeys(p['host'] for p in target['places']))
                label = f'agent <small>{h(hosts)} · 跟服务端走</small>'
            else:
                place = target['place']
                label = ('服务端 ' if view['agents'] else '') + h(place['host']) + ' · ' + h(place['service'])
                label += f' <small>{h(place["version"])}</small>' if len(view['servers']) > 1 else ''
                if place['version'] == 'unknown':
                    unseen.append(row(identity, target, h(target['label']) + '：版本不明，' + h(place['error'])))
                    rendered_unknown.add((view['name'], place['host'], place['service']))
                    continue
            if target['role'] == 'server' and view['judged'].get(target['place']['id'], {}).get('verdict') != lead['verdict']:
                label += ' <small>' + h(view['judged'].get(target['place']['id'], {}).get('verdict', '尚未判断')) + '</small>'
            instance_rows.append(row(identity, target, label))
        points = ''
        if is_up and lead.get('points'):
            points = '<ul>' + ''.join(point(p, view['releases']) for p in lead['points'][:3]) + '</ul>'
        body = f'''<section class="card {'upgrade' if is_up else 'waiting'}" data-software="{h(view['id'])}"><div class="{'head' if is_up else 'mini'}">
<span class="name">{h(view['name'])}</span><span class="ver"><b>{h(view['installed'])}</b> → <b>{h(view['latest'])}</b></span>
<span class="count">{h(count_instances(view))}</span><span class="tag {'up' if is_up else 'wait'}">{h(lead['verdict'])}</span></div>
<p class="why">{h(lead['headline'])}</p>{points}{'<div class="inst">'+''.join(instance_rows)+'</div>' if instance_rows else ''}</section>'''
        if lead['verdict'] == '版本不明':

            if instance_rows:
                unseen.append(body)
        elif is_up:
            up.append(body)
        else:
            wait.append(body)
    sections = ('<h2>该升</h2>' + ''.join(up) if up else '') + ('<h2>可以等</h2>' + ''.join(wait) if wait else '')
    features = [row(k, t) for k, t in registry.items() if t['kind'] == 'feature']
    if features:
        sections += '<h2>可能用得上的新功能</h2><section class="card">' + ''.join(features) + '</section>'
    new_needs = [row(k, t) for k, t in registry.items() if t['kind'] == 'need']
    if new_needs:
        sections += '<h2>需要确认</h2><section class="card">' + ''.join(new_needs) + '</section>'
    blind = blind_spots(report['versions'], report.get('snapshot', versions), report.get('blind', []))
    changes = report.get('changes', [])
    if blind or unseen or changes:
        sections += '<h2>这周没看全的</h2>'
    if blind or unseen:
        content = ''.join(f'<p>{h(blind_text(b, versions))}</p>' for b in blind
                          if (b.get('software'), b.get('host'), b.get('service')) not in rendered_unknown) + ''.join(unseen)
        sections += f'<details><summary>{len(blind)} 项待补齐</summary>{content}</details>'
    if changes:
        sections += '<details><summary>清单变化 ' + str(len(changes)) + ' 项</summary><div class="chips">'
        sections += ''.join(f'<span>{h(c.get("name") or c["id"])} · {h(c["action"])}</span>' for c in changes) + '</div></details>'
    stats = []
    for count, text in ((len(up), '个软件该升'), (len(wait), '个可以等'), (len(blind), '项没看全'), (len(changes), '项清单变化')):
        if count:
            stats.append(f'<b>{count}</b> {text}')
    top = ' · '.join(stats)
    if not up:
        top = '没有该升的项' + (' · ' + top if top else '')
    assets = Path(__file__).parent
    css = (assets / 'report.css').read_text()
    js = (assets / 'report.js').read_text()
    title = '盯更新周报' if report['kind'] == 'weekly' else '盯更新日报'
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · {h(report['id'])}</title><style>{css}</style></head><body data-report="{h(report['id'])}"><main>
<h1>{title} · {h(report.get('day', now.date().isoformat()))}</h1><div class="top">{top}</div>{sections}
<div id="out"><textarea id="outText" aria-label="复制反馈内容" readonly></textarea><div id="msg" role="status"></div></div></main>
<div class="bar"><span id="count">还没选</span><button id="copy" type="button">复制反馈</button></div><script>{js}</script></body></html>'''
