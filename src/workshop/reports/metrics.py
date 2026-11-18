"""按事实区间计算供给、流动、负载；不经过会写库的日志初始化。"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
from workshop.core.log import Log


STATES = ('在跑', '定时', '卡住', '排队', '等你', '等前置', '等子单', '等资料', '其他')
TERMINAL = ('完成', '放弃', '移除')
FIELDS = ('target', 'flow', 'title', 'action', 'release_gate', 'pending_classification',
          'unmatched', 'state', 'wait_for_ready', 'token', 'executor', 'stopped',
          'status', 'outcome', 'event', 'plugin_blocked', 'wait_id',
          'id', 'item', 'mode', 'answered', 'cancelled', 'rejected')
KINDS = ('event', 'event_classified', 'item_changed', 'gate_blocked', 'gate_passed',
         'round_finished', 'work_blocked', 'round_reserved', 'round_changed',
         'item_dropped', 'reset', 'wait_bound', 'wait_changed', 'wait_resumed',
         'card_created', 'card_changed')


def instant(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('时间必须含时区偏移')
    return result.astimezone(timezone.utc)


def window(since=None, start=None, end=None, now=None):
    now = now or datetime.now(timezone.utc)
    if start is not None or end is not None:
        if since is not None or start is None or end is None:
            raise ValueError('--from 和 --to 必须一起使用，不能同时给 --since')
        first, last = instant(start), instant(end)
    else:
        match = re.fullmatch(r'([1-9][0-9]*)([smhd])', since if since is not None else '24h')
        if not match:
            raise ValueError('--since 要用正整数加 s/m/h/d，例如 24h')
        seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
        first, last = now - timedelta(seconds=seconds), now
    if last <= first:
        raise ValueError('窗口终点必须晚于起点')
    return first, last


def read_snapshot(path):
    """短只读事务保证各表是同一快照；不建表、不迁移、不取写锁。"""
    log = Log.readonly(path)
    try:
        with log.read_transaction():
            rules = (log.get('controls', '@rules') or {}).get('data', {})
            meta = {row['id']: {k: row[k] for k in ('target', 'flow', 'title')}
                    for row in log.all('items', fields=('id', 'target', 'flow', 'title'))}
            facts = [(row['seq'], row['kind'], row['item'], instant(row['at']), row['body'])
                     for row in log.selected_facts(KINDS, FIELDS)]
        return facts, meta, rules
    finally:
        log.close()


def histories(facts, metadata, rules, end):
    """保留窗口前的状态与轮次端点，用于裁剪和当前等待年龄。"""
    items, rounds, cards, waits = {}, {}, {}, {}

    def ensure(key, body):
        if key not in items:
            meta = metadata.get(key, {})
            target = body.get('target') or meta.get('target') or ''
            if not isinstance(target, str):
                target = target.get('name', '')
            config = rules.get('targets', {}).get(target, {})
            items[key] = dict(id=key, title=body.get('title') or meta.get('title') or key,
                              target=target, flow=body.get('flow') or meta.get('flow') or
                              config.get('default_flow') or config.get('plugin') or '未登记',
                              state='空闲', waiting=False, plugin_blocked=False, wait_id=None, timeline=[])
        return items[key]

    def record(item, at):
        state = item['state']
        if state in TERMINAL:
            label = '结束'
        elif item['waiting']:
            label = '未接单'
        else:
            label = {'空闲': '排队', '跑着': '在跑'}.get(state, state)
            if label not in STATES:
                label = '其他'
        timeline = item['timeline']
        if not timeline or timeline[-1][1] != label:
            timeline.append((at, label))

    def release(item):
        reference_pending = waits.get(item.get('wait_id'), {}).get('state') == 'waiting'
        pending = reference_pending or any(card.get('item') == item['id'] and card.get('mode') == 'ask'
                                          and not any(card.get(k) for k in ('answered', 'cancelled', 'rejected'))
                                          for card in cards.values())
        if item['state'] in ('卡住', '等你') and not item['plugin_blocked'] and not pending:
            item['state'] = '空闲'
        # 尚未建活的未接单与已有活的拒收等待分别沿用原口径。
        if item['waiting']:
            item['waiting'] = False

    for _, kind, key, at, body in facts:
        if kind == 'round_reserved':
            rounds[body['token']] = dict(item=key, start=at, free=None, end=None,
                                          executor=body.get('executor'), outcome=None)
        elif kind == 'round_changed':
            row = rounds.get(body.get('token'))
            if row:
                if row['free'] is None and (body.get('stopped') or body.get('status') == 'ended'):
                    row['free'] = at
                if body.get('status') == 'ended':
                    row['end'] = at
                if 'outcome' in body:
                    row['outcome'] = body['outcome']
        # 轮次的最终结果可在窗口之后出现；状态快照只能用终点以前的事实。
        if at >= end:
            continue
        if kind in ('card_created', 'card_changed'):
            cards.setdefault(body['id'], {}).update(body)
            continue
        if kind in ('wait_bound', 'wait_changed', 'wait_resumed'):
            waits.setdefault(body['id'], {}).update(body)
            if kind == 'wait_bound':
                ensure(key, body)['wait_id'] = body['id']
            elif kind == 'wait_resumed':
                waits[body['id']]['state'] = 'resumed'
                item = ensure(key, body)
                item.update(wait_id=None, state='空闲')
                record(item, at)
            continue
        if kind == 'reset':
            cards.clear()
            waits.clear()
            for item in items.values():
                item['state'] = '移除'
                record(item, at)
            continue
        if kind == 'event_classified':
            if body.get('event'):
                outer_action = body.get('action')
                body = {**body['event']}
                if outer_action == 'ready':
                    body['action'] = 'ready'
            elif body.get('action') != 'ready' or key not in items:
                continue
            kind = 'event'
        if kind == 'event':
            if not key or body.get('pending_classification') or body.get('unmatched'):
                continue
            new = key not in items or items[key]['state'] == '移除'
            item = ensure(key, body)
            if body.get('flow'):
                item['flow'] = body['flow']
            if new:
                item.update(state='空闲', plugin_blocked=False, wait_id=None,
                            waiting=bool(body.get('release_gate') and body.get('action') != 'ready'))
            else:
                if item['state'] == '等资料' and not item['plugin_blocked']:
                    item['state'] = '空闲'  # 同视图：等资料的活收到任何新事件就放回空闲。
                if body.get('action') == 'ready':
                    release(item)
            record(item, at)
        elif key and kind == 'item_dropped':
            item = ensure(key, body)
            item['state'] = '移除'
            record(item, at)
        elif key and any(field in body for field in ('state', 'wait_for_ready', 'plugin_blocked', 'wait_id')):
            item = ensure(key, body)
            if 'state' in body:
                item['state'] = body['state']
            for field in ('plugin_blocked', 'wait_id'):
                if field in body:
                    item[field] = body[field]
            if 'wait_for_ready' in body:
                item['waiting'] = bool(body['wait_for_ready'])
            record(item, at)
    return items, list(rounds.values())


def segments(item, rounds, end):
    """状态与 agent 在跑区间做端点扫描，重叠轮不重复计活的时间。"""
    events = []
    for at, state in item['timeline']:
        events.append((at, 0, state))
    for row in rounds:
        if row['executor'] and row['item'] == item['id'] and row['start'] < end:
            events.append((row['start'], 1, 1))
            events.append((row['end'] or end, 1, -1))
    events.sort(key=lambda event: (event[0], event[1]))
    result, state, running, previous = [], '结束', 0, None
    for at, kind, value in events + [(end, 2, None)]:
        at = min(at, end)
        if previous is not None and at > previous:
            label = '在跑' if running and state not in ('未接单', '结束') else state
            if result and result[-1][2] == label and result[-1][1] == previous:
                result[-1] = (result[-1][0], at, label)
            else:
                result.append((previous, at, label))
        if kind == 0:
            state = value
        elif kind == 1:
            running += value
        previous = at
    return result


def overlap(start, end, first, last):
    return max(0.0, (min(end, last) - max(start, first)).total_seconds())


def summarize(items, rounds, first, last, capacity):
    result = dict(in_flight=0, pending=0, oldest_pending=None, longest={},
                  seconds={state: 0.0 for state in STATES}, failed=0, rounds=0,
                  occupied_seconds=0.0, capacity_seconds=(last-first).total_seconds()*capacity)
    for item in items:
        for start, end, state in item['segments']:
            if state in STATES:
                result['seconds'][state] += overlap(start, end, first, last)
            # 半开窗口结束前的快照；等待年龄不在窗口起点截断。
            if start < last <= end:
                entry = dict(id=item['id'], title=item['title'], seconds=(last-start).total_seconds())
                if state == '未接单':
                    result['pending'] += 1
                    old = result['oldest_pending']
                    if old is None or entry['seconds'] > old['seconds']:
                        result['oldest_pending'] = entry
                elif state in STATES:
                    result['in_flight'] += 1
                    if state in ('卡住', '等你', '排队'):
                        old = result['longest'].get(state)
                        if old is None or entry['seconds'] > old['seconds']:
                            result['longest'][state] = entry
    for row in rounds:
        if not row['executor']:
            continue
        if first <= row['start'] < last:
            result['rounds'] += 1
            result['failed'] += bool(row['end'] and row['outcome'] == 'failed')
        result['occupied_seconds'] += overlap(row['start'], row['free'] or last, first, last)
    result['load'] = result['occupied_seconds'] / result['capacity_seconds']
    total = sum(result['seconds'].values())
    result['efficiency'] = result['seconds']['在跑'] / total if total else 0.0
    return result


def calculate(facts, metadata, rules, first, last):
    capacity = rules.get('limits', {}).get('concurrent')
    if type(capacity) is not int or capacity <= 0:
        raise ValueError('状态库规则快照缺少有效 limits.concurrent')
    items, rounds = histories(facts, metadata, rules, last)
    for item in items.values():
        item['segments'] = segments(item, rounds, last)
    groups = defaultdict(list)
    for item in items.values():
        groups[(item['target'], item['flow'])].append(item)
    report = dict(start=first.isoformat(), end=last.isoformat(), capacity=capacity,
                  summary=summarize(items.values(), rounds, first, last, capacity), groups=[], hours=[], items=[])
    for (target, flow), members in sorted(groups.items()):
        keys = {i['id'] for i in members}
        selected = [r for r in rounds if r['item'] in keys]
        report['groups'].append(dict(target=target, flow=flow,
                                     **summarize(members, selected, first, last, capacity)))
    at = first
    while at < last:
        until = min(last, at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        report['hours'].append(dict(start=at.isoformat(), end=until.isoformat(),
                                   **summarize(items.values(), rounds, at, until, capacity)))
        at = until
    for item in sorted(items.values(), key=lambda value: value['id']):
        data = summarize([item], [r for r in rounds if r['item'] == item['id']], first, last, capacity)
        if sum(data['seconds'].values()) or data['in_flight'] or data['pending']:
            report['items'].append(dict(id=item['id'], title=item['title'], target=item['target'],
                                       flow=item['flow'], **data))
    return report


def render(report):
    def clean(value):
        return ' '.join(str(value).split())

    def age(entry):
        return f"{clean(entry['id'])} {clean(entry['title'])} {entry['seconds']:.3f}s" if entry else '无 0s'

    def row(data):
        total = sum(data['seconds'].values())
        times = ' '.join(f'{state}={seconds:.3f}s({seconds/total:.1%})' if total else f'{state}=0s(0%)'
                         for state, seconds in data['seconds'].items())
        ratio = data['failed']/data['rounds'] if data['rounds'] else 0
        return (f"供给 在途={data['in_flight']} 未接单={data['pending']} 最老={age(data['oldest_pending'])} | "
                f"流动 {times} 效率={data['efficiency']:.2%} 失败轮={data['failed']}/{data['rounds']}({ratio:.2%}) | "
                f"负载={data['load']:.2%} 占用={data['occupied_seconds']/60:.3f}位置分钟")

    lines = [f"窗口 [{report['start']}, {report['end']})；UTC 整点分桶；容量={report['capacity']}（读取时规则快照）",
             '供给/最长等待为各窗口末快照；流动按区间累计。失败轮按开始时间分桶、结果截至读取时。',
             '空闲计排队，程序在跑计流动、不计 agent 轮及位置；分组负载共用全局容量分母。',
             f"汇总 {row(report['summary'])}",
             f"容量 {report['summary']['capacity_seconds']/60:.3f}位置分钟"]
    for state in ('卡住', '等你', '排队'):
        lines.append(f"当前最长{state}：{age(report['summary']['longest'].get(state))}")
    lines.append('按交付对象 / 流程')
    for group in report['groups']:
        lines.append(f"{clean(group['target'])} / {clean(group['flow'])} {row(group)}")
        for state in ('卡住', '等你', '排队'):
            lines.append(f"  最长{state}：{age(group['longest'].get(state))}")
    lines.append('按小时')
    for hour in report['hours']:
        lines.append(f"{hour['start']} → {hour['end']} {row(hour)}")
    lines.append('每件活（窗口内）')
    for item in report['items']:
        lines.append(f"{clean(item['id'])} {clean(item['title'])} {row(item)}")
    return '\n'.join(lines)


def metrics(path, since=None, start=None, end=None):
    first, last = window(since, start, end)
    return render(calculate(*read_snapshot(path), first, last))
