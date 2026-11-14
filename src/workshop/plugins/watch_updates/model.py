"""盯更新的数据约定；原始资料与可信的配置分开。"""
import hashlib
import json
import re


SCHEMA = 'watch-updates/v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def version(text):
    match = re.fullmatch(r'(?:v|[A-Za-z0-9_-]+@)?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?', text)
    if not match:
        raise ValueError(f'版本不明：{text}')
    suffix = match[4]
    parts = tuple((0, int(x)) if x.isdigit() else (1, x) for x in (suffix or '').split('.'))
    return (*map(int, match.group(1, 2, 3)), suffix is None, parts)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', value):
        raise ValueError('标识须是字母、数字、点、短横线或下划线')
    return value


def required(row, fields):
    if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in fields):
        raise ValueError('缺少文字字段：' + ', '.join(fields))


def validate_software(row):
    if not isinstance(row, dict):
        raise ValueError('软件记录须为对象')
    identifier(row.get('id'))
    required(row, ('name', 'evidence', 'source', 'tag_pattern'))
    if row.get('kind') not in ('infra', 'agent') or type(row.get('prerelease')) is not bool:
        raise ValueError('kind 须为 infra/agent，prerelease 须为布尔值')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', row.get('upstream', '')):
        raise ValueError('upstream 须是 GitHub owner/repo')
    re.compile(row['tag_pattern'])
    profile = row.get('profile', {})
    if type(profile.get('exposed')) is not bool or any(k not in profile for k in ('data', 'users', 'paired')):
        raise ValueError('软件画像须写 exposed/data/users/paired')
    instances = row.get('instances')
    if not isinstance(instances, list) or not instances:
        raise ValueError('盯的软件至少要有一处部署')
    seen = set()
    for place in instances:
        identifier(place.get('id'))
        required(place, ('host', 'service', 'version', 'read_at', 'evidence'))
        if place['id'] in seen:
            raise ValueError('部署标识重复')
        seen.add(place['id'])
        if place['version'] != 'unknown':
            version(place['version'])
        elif not place.get('error'):
            raise ValueError('版本不明须注明缺什么')
    return row


def eligible(row):
    return (row.get('operated_by_us') is True and not any(row.get(k) for k in
            ('auto_updates', 'business_image', 'lock_dependency', 'bundled_with')))


def reconcile(previous, proposal, now):
    """未查成的来源保留旧记录；明确卸载才移出，冷门不等于卸载。"""
    sources = proposal.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('对账须列出每个感知来源的结果')
    outcomes = {}
    for row in sources:
        required(row, ('id', 'evidence'))
        if type(row.get('ok')) is not bool or row['id'] in outcomes:
            raise ValueError('来源结果无效或重复')
        outcomes[row['id']] = row['ok']
    old = {r['id']: r for r in previous.get('software', [])}
    excluded = dict(previous.get('excluded', {}))
    changes, present = [], set()
    for row in proposal.get('software', []):
        validate_software(row)
        if row['id'] in present or row['source'] not in outcomes:
            raise ValueError('软件重复或来源未登记')
        present.add(row['id'])
        if not outcomes[row['source']]:
            continue
        if row['id'] in excluded or not eligible(row):
            if row['id'] in old:
                del old[row['id']]
                changes.append({'id': row['id'], 'action': '移出范围', 'evidence': row['evidence']})
            continue
        prior = old.get(row['id'])
        row = {**row, 'notes': list(dict.fromkeys((prior or {}).get('notes', []) + row.get('notes', [])))}
        if prior != row:
            changes.append({'id': row['id'], 'action': '更新' if prior else '加入', 'evidence': row['evidence']})
        old[row['id']] = row
    for removal in proposal.get('removed', []):
        required(removal, ('id', 'source', 'evidence'))
        if outcomes.get(removal['source']) is not True or removal.get('uninstalled') is not True:
            raise ValueError('移出必须有成功来源和明确卸载证据')
        prior = old.get(removal['id'])
        if prior and prior['source'] != removal['source']:
            raise ValueError('卸载证据来源与清单不匹配')
        if prior:
            del old[removal['id']]
            changes.append({'id': removal['id'], 'action': '移出', 'evidence': removal['evidence']})
    return {'schema': SCHEMA, 'software': list(old.values()), 'excluded': excluded}, changes


def release_range(software, releases):
    installed = [version(p['version']) for p in software['instances']]
    found = []
    for row in releases:
        tag = row.get('tag_name', '')
        if row.get('draft') or not re.fullmatch(software['tag_pattern'], tag):
            continue
        try:
            parsed = version(tag)
        except ValueError:
            continue
        if (row.get('prerelease') or not parsed[3]) and not software['prerelease']:
            continue
        if parsed > min(installed):
            required(row, ('html_url',))
            found.append(row)
    return sorted(found, key=lambda r: version(r['tag_name']))


def validate_judgment(value, software, releases, needs, directions):
    rows = value.get('instances')
    places = {p['id'] for p in software['instances']}
    if not isinstance(rows, list) or {r.get('id') for r in rows} != places or len(rows) != len(places):
        raise ValueError('判断必须逐处覆盖全部部署')
    links = {r['html_url'] for r in releases}
    references = {n['id'] for n in needs} | {d['id'] for d in directions} | {'profile'}
    for row in rows:
        required(row, ('id', 'reason', 'summary'))
        if row.get('verdict') not in ('该升', '可以等', '不用升') or row.get('priority') not in ('紧急', '重要', '普通'):
            raise ValueError('结论或紧急程度无效')
        if not row.get('links') or not set(row['links']).issubset(links):
            raise ValueError('判断须引用本次发布说明')
        if row['verdict'] == '该升' and (not row.get('references') or not set(row['references']).issubset(references)):
            raise ValueError('该升须引用需要、在做的事或软件画像')
        if row['priority'] in ('紧急', '重要') and row['verdict'] != '该升':
            raise ValueError('紧急、重要须是该升的项')
        if row['priority'] == '重要' and not set(row.get('references', [])).intersection(references - {'profile'}):
            raise ValueError('重要须引用碰到的问题或现在要用的功能')
        if row['priority'] == '紧急':
            reasons = row.get('signals', {})
            if not ((reasons.get('high_risk_vulnerability') and software['profile']['exposed']) or reasons.get('connection_failure')):
                raise ValueError('紧急须有对外暴露的高危漏洞或断连证据')
        for feature in row.get('features', []):
            required(feature, ('id', 'summary', 'direction'))
            if feature['direction'] not in {d['id'] for d in directions}:
                raise ValueError('新功能须对上在做的事')
    return value
