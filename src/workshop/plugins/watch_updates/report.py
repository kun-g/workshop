"""按认可样例输出私密离线报告：结论、要点、部署反馈。"""
from datetime import datetime
from html import escape
from pathlib import Path

from .model import UPGRADE_ORDER, point_link
from .inventory import blind_records
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
        rows.extend(blind_records(view['software']))
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


def short(text):
    """标签里只留名字：去掉括号里的补充说明。"""
    import re
    return re.split(r'\s*[（(]', str(text))[0].strip() or str(text)


SOURCES = {'local': '本机', 'servers': '服务器', 'clusters': '集群', 'needs': '需要来源'}


def blind_groups(blind, versions):
    """盲区按类归并成「标题 · 一句话 · 名字标签」，原始错误留在对账记录里，不上页面。"""
    pending, unknown, sources, manual, other = [], {}, [], [], []
    retained, conflicts = [], []
    for value in blind:
        if not isinstance(value, dict):
            other.append(str(value))
        elif value.get('judgment_pending'):
            key = value['judgment_pending']
            pending.append(short(versions.get(key, {}).get('software', {}).get('name', key.rsplit('-', 1)[0])))
        elif value.get('version') == '版本不明':
            unknown.setdefault(short(value['software']), []).append(short(value['host']))
        elif value.get('kind') in ('retained', 'conflict'):
            (retained if value['kind'] == 'retained' else conflicts).append(blind_text(value, versions))
        elif value.get('kind') == 'manual':
            manual.append(short(value['software']))
        elif value.get('id') in SOURCES or 'ok' in value:
            sources.append(SOURCES.get(value.get('id'), value.get('id', '')))
        else:
            other.append(blind_text(value, versions))
    groups = []
    if pending:
        groups.append((f'{len(pending)} 个软件没判成 · 下周补', '有新版，但发布说明没读成或上游只有 tag 没有说明。', list(dict.fromkeys(pending))))
    if unknown:
        count = sum(len(v) for v in unknown.values())
        note = ('对账没连上：' + '、'.join(sources) + '，这些部署沿用上次读数或读不到版本。') if sources else '这些部署读不到版本号。'
        groups.append((f'{count} 处版本不明', note,
                       [f'{name} ×{len(hosts)}' if len(hosts) > 1 else f'{name} · {hosts[0]}' for name, hosts in unknown.items()]))
    elif sources:
        groups.append((f'{len(sources)} 类来源没查全', '原因见本周对账记录。', sources))
    if retained:
        groups.append((f'{len(retained)} 处沿用上次版本', '本轮没有新读数，保留原版本、日期与证据。', retained))
    if conflicts:
        groups.append((f'{len(conflicts)} 处版本来源有冲突', '声明值与其他来源不同，尚未确认运行版本。', conflicts))
    if manual:
        groups.append((f'{len(manual)} 个软件没有版本接口', '上游不提供可查的发布接口，需要人工看。', manual))
    if other:
        groups.append((f'{len(other)} 项其他', '', other))
    return groups


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

    up, wait = [], []
    for view in views:
        lead = view['lead']
        started = [datetime.fromisoformat(value['first_reported']) for _, value in view['entries']
                   if value.get('first_reported') and any(p['id'] not in value.get('feedback', {})
                                                        for p in value['software']['instances'])]
        days = max((now - min(started)).days, 0) if started else 0
        waited = f'<span class="count">已等 {days} 天</span>' if days else ''
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
                    continue
            if target['role'] == 'server' and view['judged'].get(target['place']['id'], {}).get('verdict') != lead['verdict']:
                label += ' <small>' + h(view['judged'].get(target['place']['id'], {}).get('verdict', '尚未判断')) + '</small>'
            instance_rows.append(row(identity, target, label))
        points = ''
        if is_up and lead.get('points'):
            points = '<ul>' + ''.join(point(p, view['releases']) for p in lead['points'][:3]) + '</ul>'
        body = f'''<section class="card {'upgrade' if is_up else 'waiting'}" data-software="{h(view['id'])}"><div class="{'head' if is_up else 'mini'}">
<span class="name">{h(view['name'])}</span><span class="ver"><b>{h(view['installed'])}</b> → <b>{h(view['latest'])}</b></span>
<span class="count">{h(count_instances(view))}</span><span class="tag {'up' if is_up else 'wait'}">{h(lead['verdict'])}</span>{waited}</div>
<p class="why">{h(lead['headline'])}</p>{points}{'<div class="inst">'+''.join(instance_rows)+'</div>' if instance_rows else ''}</section>'''
        if lead['verdict'] == '版本不明':
            continue
        elif is_up:
            up.append(body)
        else:
            wait.append(body)
    sections = ('<h2>该升</h2>' + ''.join(up) if up else '') + ('<h2>可以等</h2>' + ''.join(wait) if wait else '')
    # 新功能和新需要要 Kun 作答，但不是本周重点，默认折起。
    features = [row(k, t) for k, t in registry.items() if t['kind'] == 'feature']
    if features:
        sections += f'<details><summary>可能用得上的新功能 <span>{len(features)} 项</span></summary>' + ''.join(features) + '</details>'
    new_needs = [row(k, t) for k, t in registry.items() if t['kind'] == 'need']
    if new_needs:
        sections += f'<details><summary>需要确认 <span>{len(new_needs)} 项</span></summary>' + ''.join(new_needs) + '</details>'
    blind = blind_spots(report['versions'], report.get('snapshot', versions), report.get('blind', []))
    groups = blind_groups(blind, versions)
    # 版本号变化不算清单变化，只列新加和移出的软件。
    changes = [c for c in report.get('changes', []) if c.get('action') != '更新']
    if groups or changes:
        sections += '<h2>这周没看全的</h2>'
    for title, note, chips in groups:
        sections += f'<details><summary>{h(title)}</summary>' + (f'<p>{h(note)}</p>' if note else '')
        sections += '<div class="chips">' + ''.join(f'<span>{h(c)}</span>' for c in chips) + '</div></details>'
    if changes:
        sections += '<details><summary>清单变化 ' + str(len(changes)) + ' 项</summary><div class="chips">'
        sections += ''.join(f'<span>{h(c.get("name") or c["id"])} · {h(c["action"])}</span>' for c in changes) + '</div></details>'
    stats = []
    for count, text in ((len(up), '个软件该升'), (len(wait), '个可以等'), (len(changes), '项清单变化')):
        if count:
            stats.append(f'<b>{count}</b> {text}')
    stats += [h(title.split(' · ')[0]) for title, _, _ in groups]
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
