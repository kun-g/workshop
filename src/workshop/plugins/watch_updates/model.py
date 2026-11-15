"""盯更新的数据约定；原始资料与可信的配置分开。"""
import hashlib
import json
import re


SCHEMA = 'watch-updates/v1'
VERDICTS = ('该升', '可以等', '不用升')
PRIORITIES = ('紧急', '重要', '普通')
HEADLINE_LIMIT = 40
POINT_LIMIT = 45
POINTS_LIMIT = 3
FEATURE_LIMIT = 50
PROFILE_REFERENCE = 'profile'

UPGRADE_ORDER = '先升服务端，agent 跟着升；agent 不能比服务端新。'
# 给人看的字段里不许出现写给校验程序的辩护（Kun 11-15：报告里的理由「是给人看的？重点是什么？」）。
HEDGE = re.compile(r'profile|needs?|directions?|资料|证实|核实|不能(认定|据此|按|假定|当作|确定)|不标紧急|判紧急|个容器|[\r\n]', re.I)


def judgment_sources(releases, needs, directions):
    """说明与校验共享本轮可引用值，不从外部正文解释出新规则。"""
    return {
        'links': {r['html_url'] for r in releases},
        'references': {n['id'] for n in needs} | {d['id'] for d in directions} | {PROFILE_REFERENCE},
        'directions': {d['id'] for d in directions},
    }


def judgment_spec(releases, needs, directions):
    sources = judgment_sources(releases, needs, directions)
    return f'''verdict 只能填 {'、'.join(VERDICTS)}；priority 只能填 {'、'.join(PRIORITIES)}（中文原词）。
instances 必须逐处覆盖全部已知版本的 server，id 来自 software.instances，不重复、不添加部署；agent 和 unknown 的结论由程序派生。
每项 id、headline 必填且为非空字符串；headline 先说原因，不超过 {HEADLINE_LIMIT} 字。
points 必须是列表，最多 {POINTS_LIMIT} 条；每条 {{version,text}} 都是非空字符串，text 不超过 {POINT_LIMIT} 字。
points.version 必须对应本轮 releases.tag_name（可省略 v 前缀），且该发布的 html_url 是 HTTPS 链接。
links 必填非空列表，原样取 releases.html_url：{json.dumps(sorted(sources['links']), ensure_ascii=False)}。
references 引用 needs.id、directions.id 或 {PROFILE_REFERENCE}，本轮可用：{json.dumps(sorted(sources['references']), ensure_ascii=False)}；该升至少填一个且全部合法。
紧急、重要须为该升；重要的 references 至少含一个 needs.id 或 directions.id，不能仅有 {PROFILE_REFERENCE}。
紧急须 signals.high_risk_vulnerability 为真且 software.profile.exposed 为真，或 signals.connection_failure 为真；必须有影响在用版本的漏洞或断连证据。
features 可省略；每条 id、summary、direction 都为非空字符串；summary 说明功能和对应的事，不超过 {FEATURE_LIMIT} 字。
features.direction 只能取 directions.id：{json.dumps(sorted(sources['directions']), ensure_ascii=False)}。
headline、points.text、features.summary 不写校验辩护，不含换行或以下模式：{HEDGE.pattern}；features.summary 还不能含「不构成」。
程序补的升级顺序要点「{UPGRADE_ORDER}」不受 text 长度和上述模式限制。'''



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


def upstream(software):
    """兼容首版清单；新清单显式记录查询接口或人工盲区。"""
    value = software.get('upstream')
    if isinstance(value, str):
        value = {'kind': 'github_releases', 'repo': value}
    if not isinstance(value, dict):
        raise ValueError('upstream 须为带 kind 的对象')
    kind = value.get('kind')
    if kind == 'manual':
        required(value, ('reason',))
    elif kind in ('github_releases', 'github_tags'):
        if not isinstance(value.get('repo'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value['repo']):
            raise ValueError('GitHub upstream.repo 须是 owner/repo')
    elif kind in ('gitlab_releases', 'gitlab_tags'):
        project = value.get('project')
        if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+|[0-9]+', project):
            raise ValueError('GitLab upstream.project 须是项目路径或数字 ID（gitlab.com）')
    else:
        raise ValueError('不支持的 upstream.kind')
    return value


def validate_software(row, *, roles=False):
    if not isinstance(row, dict):
        raise ValueError('软件记录须为对象')
    identifier(row.get('id'))
    required(row, ('name', 'evidence', 'source'))
    manual = upstream(row)['kind'] == 'manual'
    if row.get('kind') not in ('infra', 'agent'):
        raise ValueError('kind 须为 infra/agent')
    if not manual:
        required(row, ('tag_pattern',))
        if type(row.get('prerelease')) is not bool:
            raise ValueError('prerelease 须为布尔值')
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
        if (roles or 'role' in place) and place.get('role') not in ('server', 'agent'):
            raise ValueError('部署 role 须为 server/agent；客户端也算 agent')
        required(place, ('host', 'service', 'version', 'read_at', 'evidence'))
        if place['id'] in seen:
            raise ValueError('部署标识重复')
        seen.add(place['id'])
        if place['version'] != 'unknown' and not manual:
            version(place['version'])
        elif place['version'] == 'unknown' and not place.get('error'):
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
        validate_software(row, roles=True)
        if row['id'] in present or row['source'] not in outcomes:
            raise ValueError('软件重复或来源未登记')
        present.add(row['id'])
        if not outcomes[row['source']]:
            continue
        if row['id'] in excluded or not eligible(row):
            if row['id'] in old:
                del old[row['id']]
                changes.append({'id': row['id'], 'name': row['name'], 'action': '移出范围', 'evidence': row['evidence']})
            continue
        prior = old.get(row['id'])
        row = {**row, 'notes': list(dict.fromkeys((prior or {}).get('notes', []) + row.get('notes', [])))}
        if prior != row:
            changes.append({'id': row['id'], 'name': row['name'], 'action': '更新' if prior else '加入', 'evidence': row['evidence']})
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


def tag_version(software, tag):
    """tag_pattern 可用命名组 version 指出版本号部分，适配 docker-v29.9.3、v19.5.3-ee 这类前后缀。"""
    match = re.fullmatch(software['tag_pattern'], tag)
    if not match:
        return None
    try:
        return version(match['version'] if 'version' in match.re.groupindex else tag)
    except ValueError:
        return None


def release_range(software, releases):
    installed = [version(p['version']) for p in software['instances'] if p['version'] != 'unknown']
    if not installed:
        reasons = '；'.join(f"{p.get('host', p.get('id', ''))}/{p.get('service', '')}：{p.get('error', '未取得版本')}"
                           for p in software['instances'])
        raise ValueError('所有部署版本不明：' + reasons)
    found = []
    for row in releases:
        tag = row.get('tag_name', '')
        parsed = None if row.get('draft') else tag_version(software, tag)
        if parsed is None:
            continue
        if (row.get('prerelease') or not parsed[3]) and not software['prerelease']:
            continue
        if parsed > min(installed):
            required(row, ('html_url',))
            found.append(row)
    return sorted(found, key=lambda r: tag_version(software, r['tag_name']))


def validate_judgment(value, software, releases, needs, directions):
    rows = value.get('instances')
    places = {p['id'] for p in software['instances']}
    validate_software(software, roles=True)
    servers = [p for p in software['instances'] if p['role'] == 'server']
    agents = [p for p in software['instances'] if p['role'] == 'agent']
    known = {p['id'] for p in servers if p['version'] != 'unknown'}
    if (not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows)
            or not known.issubset({r.get('id') for r in rows})
            or not {r.get('id') for r in rows}.issubset(places)
            or len({r.get('id') for r in rows}) != len(rows)):
        raise ValueError('判断必须逐处覆盖全部已知版本的部署，不得重复或添加部署')
    rows = [r for r in rows if r['id'] in known]
    sources = judgment_sources(releases, needs, directions)
    links, references = sources['links'], sources['references']
    for row in rows:
        required(row, ('id', 'headline'))
        if len(row['headline']) > HEADLINE_LIMIT or HEDGE.search(row['headline']):
            raise ValueError(f'headline 须不超过 {HEADLINE_LIMIT} 字，只写给人看的原因，不写资料缺口、未证实、为何不标紧急等给校验的话')
        points = row.get('points')
        if not isinstance(points, list) or len(points) > POINTS_LIMIT:
            raise ValueError(f'points 须为最多 {POINTS_LIMIT} 条要点')
        for point in points:
            required(point, ('version', 'text'))
            if point_link(point['version'], releases) is None:
                raise ValueError('要点版本须对应本次发布记录')
            if point['text'] != UPGRADE_ORDER and (len(point['text']) > POINT_LIMIT or HEDGE.search(point['text'])):
                raise ValueError(f'每条要点只写一个变化，不超过 {POINT_LIMIT} 字（版本号另列，不算在内），不写资料缺口、未证实、为何不标紧急等给校验的话')
        if row.get('verdict') not in VERDICTS or row.get('priority') not in PRIORITIES:
            raise ValueError(f'verdict 只能是 {"/".join(VERDICTS)}，priority 只能是 {"/".join(PRIORITIES)}（中文原词）')
        if not row.get('links') or not set(row['links']).issubset(links):
            raise ValueError('links 须原样取自本次 releases 的 html_url')
        if row['verdict'] == '该升' and (not row.get('references') or not set(row['references']).issubset(references)):
            raise ValueError('该升的 references 只能取 ' + '、'.join(sorted(references)) + '，至少一个')
        if row['priority'] in ('紧急', '重要') and row['verdict'] != '该升':
            raise ValueError('紧急、重要须是该升的项')
        if row['priority'] == '重要' and not set(row.get('references', [])).intersection(references - {PROFILE_REFERENCE}):
            raise ValueError('重要的 references 须含 ' + '、'.join(sorted(references - {PROFILE_REFERENCE})) + ' 之一')
        if row['priority'] == '紧急':
            reasons = row.get('signals', {})
            if not ((reasons.get('high_risk_vulnerability') and software['profile']['exposed']) or reasons.get('connection_failure')):
                raise ValueError('紧急须有对外暴露的高危漏洞或断连证据')
        for feature in row.get('features', []):
            required(feature, ('id', 'summary', 'direction'))
            if len(feature['summary']) > FEATURE_LIMIT or HEDGE.search(feature['summary']) or '不构成' in feature['summary']:
                raise ValueError(f'新功能 summary 一句话写清功能和对应的事，不超过 {FEATURE_LIMIT} 字，不写为何不提高优先级等给校验的话')
            if feature['direction'] not in sources['directions']:
                raise ValueError('features.direction 只能取 ' + '、'.join(sorted(sources['directions'])))
    unknown = [
        {'id': p['id'], 'verdict': '版本不明', 'priority': '普通', 'headline': '在用版本未确认，先补齐版本',
         'reason': p['error'], 'points': [], 'links': [], 'references': [], 'features': []}
        for p in servers if p['version'] == 'unknown']
    if agents:
        order = {'不用升': 0, '可以等': 1, '该升': 2}
        priorities = {'普通': 0, '重要': 1, '紧急': 2}
        leader = max(rows, key=lambda r: (order[r['verdict']], priorities[r['priority']])) if rows and not unknown else None
        if leader:
            for row in rows:
                row['points'] = [p for p in row['points'] if p['text'] != UPGRADE_ORDER][:2] + [
                    {'version': releases[-1]['tag_name'], 'text': UPGRADE_ORDER}]
            derived = [{**leader, 'id': p['id'], 'headline': '跟服务端走，先升服务端', 'features': [],
                        'inherited': True} for p in agents]
        else:
            derived = [{'id': p['id'], 'verdict': '版本不明', 'priority': '普通',
                        'headline': '服务端版本未确认，先核对服务端', 'points': [], 'links': [],
                        'references': [], 'features': [], 'inherited': True} for p in agents]
    else:
        derived = []
    value['instances'] = rows + unknown + derived
    value['format'] = 'watch-judgment/v2'
    return value


def point_link(tag, releases):
    """只把实际读到的版本链接给人看，不从模型给的文字构造地址。"""
    for release in releases:
        if tag == release['tag_name'] or tag.removeprefix('v') == release['tag_name'].removeprefix('v'):
            url = release.get('html_url', '')
            return url if url.startswith('https://') else None
    return None


def current_judgment(value):
    return bool(value.get('judgment', {}).get('format') == 'watch-judgment/v2'
                and all(p.get('role') in ('server', 'agent') for p in value['software']['instances']))
