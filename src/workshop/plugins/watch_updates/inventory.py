"""逐实例对账：保留历史、标明来源、应用范围排除；不执行采集命令。"""
from copy import deepcopy
from datetime import datetime
import re

from .model import SCHEMA, eligible, required, validate_software

VERSION_KINDS = {'local', 'deployment_repo', 'dokploy', 'upgrade_record', 'history'}


def scopes(value):
    if not isinstance(value, list):
        raise ValueError('excluded_scopes 须是列表')
    seen = set()
    for row in value:
        required(row, ('kind', 'value', 'reason'))
        if row['kind'] not in ('cluster', 'host') or (row['kind'], row['value']) in seen:
            raise ValueError('排除范围须为不重复的 cluster/host')
        seen.add((row['kind'], row['value']))
    return value


def cluster(place):
    if place.get('cluster'):
        return place['cluster']
    # 旧清单的位置约定；只读 host 的明确集群前缀，不从 notes 猜范围。
    match = re.match(r'^k8s\s+([\w.-]+)(?=$|\s|[（(/])', place['host'])
    return match[1] if match else None


def excluded(place, rules):
    return any((cluster(place) == r['value'] if r['kind'] == 'cluster'
                else place['host'] == r['value']) for r in rules)


def with_scopes(watchlist, configured=()):
    value = deepcopy(watchlist)
    merged = {(r['kind'], r['value']): r for r in scopes(value.get('excluded_scopes', []))}
    for row in scopes(list(configured)):
        merged[row['kind'], row['value']] = row
    value['excluded_scopes'] = list(merged.values())
    return value


def filter_scopes(watchlist):
    value = with_scopes(watchlist)
    kept = []
    for software in value['software']:
        if software['id'] in value.get('excluded', {}):
            continue
        software['instances'] = [p for p in software['instances'] if not excluded(p, value['excluded_scopes'])]
        if software['instances']:
            kept.append(software)
    value['software'] = kept
    return value


def provenance(place, *, strict=False):
    source = place.get('version_source')
    if source is None:
        if strict and place['version'] != 'unknown':
            raise ValueError('已知版本须标明 version_source.kind/reference')
        return {'kind': 'history', 'reference': place['evidence']}
    required(source, ('kind', 'reference'))
    if source['kind'] not in VERSION_KINDS:
        raise ValueError('版本来源只允许本机、部署仓库、Dokploy、升级记录或历史')
    return deepcopy(source)


def observation(place):
    return {k: deepcopy(place[k]) for k in ('version', 'read_at', 'evidence', 'version_source')}


def stale(place, now, reason):
    value = deepcopy(place)
    value['version_source'] = provenance(value)
    value['last_attempt'] = {'at': now, 'ok': False, 'error': reason}
    if value['version'] == 'unknown':
        value['error'] = reason
    return value


def retain(prior, incoming, now, reason):
    # 只保留版本证据；本轮已确认的部署角色等资料仍须更新。
    protected = {'version', 'read_at', 'evidence', 'version_source', 'error', 'last_attempt', 'conflicts'}
    value = {**deepcopy(prior), **{k: deepcopy(v) for k, v in incoming.items() if k not in protected}}
    return stale(value, now, reason)


def merge_place(prior, incoming, now, strict):
    place = deepcopy(incoming)
    if prior and prior.get('cluster') and not place.get('cluster'):
        place['cluster'] = prior['cluster']
    place['version_source'] = provenance(place, strict=strict)
    conflicts = place.get('conflicts', [])
    if not isinstance(conflicts, list):
        raise ValueError('conflicts 须为版本证据列表')
    for conflict in conflicts:
        required(conflict, ('version', 'read_at', 'evidence'))
        provenance(conflict, strict=True)
    if place['version'] == 'unknown':
        if prior and prior['version'] != 'unknown':
            return retain(prior, place, now, place['error'])
        return stale(place, now, place['error'])
    if place['version_source']['kind'] == 'history':
        # 历史只能重申已有读数，不能把旧日期刷新或替另一版本背书。
        if prior and (place['version'], place['read_at']) != (prior['version'], prior['read_at']):
            raise ValueError('历史版本不能修改已知版本或读取日期')
        if prior:
            return retain(prior, place, now, '本轮仅有历史记录，未取得新读数')
    place.pop('error', None)
    place.pop('last_attempt', None)
    if place['version_source']['kind'] == 'history':
        place['last_attempt'] = {'at': now, 'ok': False, 'error': '仅有历史记录，未取得本轮读数'}
    else:
        place['last_attempt'] = {'at': now, 'ok': True}
    # 声明值与实测或升级记录不相同，保留两份证据供报告和后续核对。
    if prior and prior['version'] != 'unknown' and prior['version'] != place['version']:
        before = {**prior, 'version_source': provenance(prior)}
        kinds = {before['version_source']['kind'], place['version_source']['kind']}
        if 'deployment_repo' in kinds and len(kinds) > 1:
            place['conflicts'] = [observation(before), observation(place)]
    return place


def upgrade_records(value):
    if not isinstance(value, list):
        raise ValueError('升级完成记录须为列表')
    for row in value:
        required(row, ('software', 'instance', 'version', 'completed_at', 'evidence'))
        if row['version'] == 'unknown':
            raise ValueError('升级完成记录须含已知版本')
        instant(row['completed_at'])
    return value


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('升级完成时间须有时区')
    return parsed


def reconcile(previous, proposal, now, *, strict=False, upgrades=()):
    previous = with_scopes(previous)
    sources = proposal.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('对账须列出每个感知来源的结果')
    outcomes = {}
    for row in sources:
        required(row, ('id', 'evidence'))
        if type(row.get('ok')) is not bool or row['id'] in outcomes:
            raise ValueError('来源结果无效或重复')
        outcomes[row['id']] = row
    original = {r['id']: deepcopy(r) for r in previous.get('software', [])}
    old = deepcopy(original)
    present = set()
    for row in proposal.get('software', []):
        validate_software(row, roles=True)
        if row['id'] in present or row['source'] not in outcomes:
            raise ValueError('软件重复或来源未登记')
        present.add(row['id'])
        if row['id'] in previous.get('excluded', {}) or not eligible(row):
            old.pop(row['id'], None)
            continue
        outcome = outcomes[row['source']]
        if not outcome['ok']:
            continue
        prior = old.get(row['id'], {})
        places = {p['id']: p for p in prior.get('instances', [])}
        observed = set()
        for place in row['instances']:
            observed.add(place['id'])
            if excluded(place, previous['excluded_scopes']):
                places.pop(place['id'], None)
                continue
            places[place['id']] = merge_place(places.get(place['id']), place, now, strict)
        for key in set(places) - observed:
            places[key] = stale(places[key], now, '本轮未提供该实例，缺项不作为卸载证据')
        old[row['id']] = {**deepcopy(row), 'instances': list(places.values()),
            'notes': list(dict.fromkeys(prior.get('notes', []) + row.get('notes', [])))}
    for key, row in old.items():
        outcome = outcomes.get(row['source'])
        if key not in present or not outcome or not outcome['ok']:
            reason = outcome['evidence'] if outcome and not outcome['ok'] else '本轮未提供该软件，保留上次读数'
            row['instances'] = [stale(p, now, reason) for p in row['instances']]
    # 程序读取固定路径的完成记录；提案中的同名字段不作为升级成功证明。
    for record in sorted(upgrade_records(list(upgrades)), key=lambda r: instant(r['completed_at'])):
        row = old.get(record['software'])
        if not row:
            continue
        for index, place in enumerate(row['instances']):
            if place['id'] != record['instance']:
                continue
            incoming = {**place, 'version': record['version'], 'read_at': record['completed_at'],
                'evidence': record['evidence'], 'version_source': {'kind': 'upgrade_record', 'reference': record['evidence']}}
            try:
                older = instant(record['completed_at']) < instant(place['read_at'])
            except ValueError:
                older = False  # 旧清单日期允许只有日期；升级完成记录已严格校验。
            if older and place['version'] != 'unknown':
                if (place['version_source']['kind'] == 'deployment_repo'
                        and place['version'] != incoming['version']):
                    place['conflicts'] = [observation(place), observation(incoming)]
                continue
            incoming.pop('conflicts', None)
            row['instances'][index] = merge_place(place, incoming, now, True)
            validate_software(row, roles=True)
    for removal in proposal.get('removed', []):
        required(removal, ('id', 'source', 'evidence'))
        if not outcomes.get(removal['source'], {}).get('ok') or removal.get('uninstalled') is not True:
            raise ValueError('移出必须有成功来源和明确卸载证据')
        if strict:
            required(removal, ('instance',))
        row = old.get(removal['id'])
        if row:
            if row['source'] != removal['source']:
                raise ValueError('卸载证据来源与清单不匹配')
            if removal.get('instance'):
                row['instances'] = [p for p in row['instances'] if p['id'] != removal['instance']]
            else:
                del old[row['id']]
    value = filter_scopes({**previous, 'schema': SCHEMA, 'software': list(old.values())})
    changes = []
    current = {s['id']: s for s in value['software']}
    for key in dict.fromkeys([*original, *current]):
        before, after = original.get(key), current.get(key)
        if before != after:
            changes.append({'id': key, 'name': (after or before)['name'],
                'action': '加入' if not before else '移出' if not after else '更新',
                'evidence': (after or before)['evidence']})
    return value, changes


def blind_records(software):
    rows = []
    for place in software['instances']:
        if place['version'] != 'unknown' and place.get('last_attempt', {}).get('ok') is False:
            rows.append({'kind': 'retained', 'name': software['name'] + ' · ' + place['host'],
                'reason': f"沿用 {place['version']}（{place['read_at']}）；本轮未读到：{place['last_attempt']['error']}"})
        if place.get('conflicts'):
            rows.append({'kind': 'conflict', 'name': software['name'] + ' · ' + place['host'],
                'reason': '版本来源有冲突：' + '；'.join(
                    f"{p['version']}（{p['version_source']['kind']}，{p['read_at']}，{p['version_source']['reference']}）"
                    for p in place['conflicts'])})
    return rows
