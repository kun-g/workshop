"""报告中的软件、部署组和反馈目标，统一来自生成时的快照。"""
from .model import digest, tag_version
from .inventory import cluster, machine


def software_views(report, versions):
    grouped = {}
    snapshot = report.get('snapshot', versions)
    for key in report['versions']:
        value = snapshot[key]
        grouped.setdefault(value['software']['id'], []).append((key, value))
    views = []
    for software_id, entries in grouped.items():
        _, latest = max(entries, key=lambda pair: tag_version(pair[1]['software'], pair[1]['releases'][-1]['tag_name']))
        soft = latest['software']
        judged = {r['id']: r for r in latest['judgment']['instances']}
        places = {p['id']: p for _, value in entries for p in value['software']['instances']}
        places.update({p['id']: p for p in soft['instances']})
        servers = [p for p in places.values() if p.get('role') == 'server']
        agents = [p for p in places.values() if p.get('role') == 'agent']
        rank = {'版本不明': -1, '不用升': 0, '可以等': 1, '该升': 2}
        judgments = [judged[p['id']] for p in servers if p['id'] in judged]
        lead = max(judgments or list(judged.values()), key=lambda j: (rank[j['verdict']],
                   {'普通': 0, '重要': 1, '紧急': 2}[j['priority']]))
        installed = list(dict.fromkeys(p['version'] for p in servers if p['version'] != 'unknown'))
        views.append({'id': software_id, 'name': soft['name'], 'software': soft, 'entries': entries,
                      'servers': servers, 'agents': agents, 'judged': judged, 'lead': lead,
                      'installed': ' / '.join(installed) or '版本不明',
                      'latest': latest['releases'][-1]['tag_name'], 'releases': latest['releases']})
    return views


def drop_scopes(view, places):
    """「不用盯」可选的范围，只列这一行实际涉及的；第一项是默认的「只这一处」。"""
    options = [{'id': 'here', 'kind': 'instance', 'label': '只这一处', 'software': view['id'],
                'instances': [p['id'] for p in places]},
               {'id': 'software', 'kind': 'software', 'label': '整个 ' + view['name'], 'software': view['id']}]
    for name in dict.fromkeys(machine(p) for p in places if machine(p)):
        options.append({'id': 'host:' + name, 'kind': 'host', 'value': name, 'label': '机器 ' + name})
    for name in dict.fromkeys(cluster(p) for p in places if cluster(p)):
        options.append({'id': 'cluster:' + name, 'kind': 'cluster', 'value': name, 'label': '集群 ' + name})
    return options


def targets(report, versions, needs):
    """id 对应的成员固定入报告；反馈时不按最新清单重新组队。"""
    result = {}
    views = software_views(report, versions)
    ids = [p['id'] for view in views for p in view['servers']]

    def add(identity, value):
        if identity in result:
            identity = 'target-' + digest(value)
        if identity in result:
            raise ValueError('报告反馈目标重复')
        result[identity] = value
        return identity

    for view in views:
        def members(place_ids):
            return [{'key': key, 'instance': p['id']} for key, value in view['entries']
                    for p in value['software']['instances'] if p['id'] in place_ids]
        for place in view['servers']:
            identity = place['id'] if ids.count(place['id']) == 1 else view['id'] + ':' + place['id']
            label = view['name'] + ' · ' + place['host'] + ' · ' + place['service']
            add(identity, {'kind': 'instances', 'software': view['id'], 'label': label,
                           'members': members({place['id']}), 'role': 'server', 'place': place,
                           'choices': ['升', '没问题', '判错了', '不用盯'], 'scopes': drop_scopes(view, [place])})
        if view['agents']:
            add(view['id'] + '-agents', {'kind': 'instances', 'software': view['id'], 'role': 'agent',
                'label': view['name'] + ' · agent（全部）', 'places': view['agents'],
                'members': members({p['id'] for p in view['agents']}),
                'choices': ['升', '没问题', '判错了', '不用盯'], 'scopes': drop_scopes(view, view['agents'])})
        for key, value in view['entries']:
            shown = set()
            for judge in value['judgment']['instances']:
                for feature in judge.get('features', []):
                    # 同一版本的同一功能，多处部署只问一次。
                    if (feature['id'], feature['summary']) in shown:
                        continue
                    shown.add((feature['id'], feature['summary']))
                    add('feature-' + digest([key, judge['id'], feature['id']]),
                        {'kind': 'feature', 'software': view['id'], 'label': feature['summary'],
                         'key': key, 'instance': judge['id'], 'feature': feature['id'],
                         'direction': feature['direction'], 'choices': ['想用', '用不上']})
    for need in needs:
        if need['id'] in report.get('needs', []):
            add('need-' + need['id'], {'kind': 'need', 'need': need['id'], 'label': need['statement'],
                                      'owner': need['owner'], 'choices': ['确认', '不要了']})
    return result
