"""GitHub 关系翻译为通用快照；同一交付对象合批读，分页完成才发布。"""
import json
import re
import time

from workshop.core.models import Relations
from workshop.core.ports import Retryable
from .io import run

FIELDS = 'number title state url repository{nameWithOwner}'
GROUPS = {'blockedBy': 'prerequisites', 'subIssues': 'members', 'relatesTo': 'related'}


def node(value, target):
    if not isinstance(value, dict):
        raise ValueError('关系包含不可见的对象，不能当成已完成')
    repo = value.get('repository', {}).get('nameWithOwner')
    number = value.get('number')
    if (not isinstance(repo, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', repo)
            or type(number) is not int or number < 1 or value.get('state') not in ('OPEN', 'CLOSED')):
        raise ValueError('关系对象缺少仓库、编号或关闭状态')
    identity = f'{repo}#{number}'
    name = f'{target.get("short_name", target["name"])}#{number}' if repo == target['repo'] else identity
    return dict(id=identity, name=name, complete=value['state'] == 'CLOSED',
                title=value.get('title', ''), url=value.get('url', ''), text=value.get('body', ''))


def query(io, text, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Retryable('关系读取超过本批时间预算；未发布不完整快照')
    result = run([io.gh, 'api', 'graphql', '-f', 'query=' + text], check=False, timeout=min(15, remaining))
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise Retryable('关系接口未返回 JSON；未发布空关系') from None
    if not isinstance(data, dict) or (result.returncode and not data.get('errors')):
        raise Retryable('关系接口请求失败；未发布空关系')
    repository = (data.get('data') or {}).get('repository')
    if not isinstance(repository, dict):
        raise Retryable('关系接口未返回仓库；未发布空关系')
    errors = {}
    for error in data.get('errors', []):
        path = error.get('path') or []
        if len(path) < 2 or path[0] != 'repository':
            raise Retryable('关系接口失败：' + error.get('message', '未知错误'))
        errors[str(path[1])] = error.get('message', '关系读取失败')
    repository['__errors__'] = errors
    return repository


def connection(field, after=None):
    args = 'first:100' + (',after:' + json.dumps(after) if after else '')
    fields = FIELDS + (' body' if field == 'relatesTo' else '')
    return f'{field}({args}){{totalCount nodes{{{fields}}} pageInfo{{hasNextPage endCursor}}}}'


def read(io, objects, target):
    owner, repo = target['repo'].split('/')
    prefix = f'query{{repository(owner:{json.dumps(owner)},name:{json.dumps(repo)}){{'
    numbers, results = {}, {}
    for obj in objects:
        number = io.read(obj, target).get('issue') or (int(obj['object_key']) if obj['object_key'].isdigit() else None)
        if number is None:
            # 尚未登记的入口先接单；有来源对象后下一拍必须查关系。
            results[obj['id']] = Relations()
        else:
            numbers[obj['id']] = int(number)
    entries = list(numbers.items())
    deadline = time.monotonic() + 20
    for start in range(0, len(entries), 20):
        batch = entries[start:start + 20]
        fields = ('number issueDependenciesSummary{totalBlockedBy} subIssuesSummary{total} parent{'
                  + FIELDS + '} ' + ' '.join(connection(k) for k in GROUPS))
        text = prefix + ' '.join(f'i{n}:issue(number:{n}){{{fields}}}' for _, n in batch) + '}}'
        try:
            values = query(io, text, deadline)
        except (Retryable, ValueError) as exc:
            results.update({identity: Relations(error=str(exc)) for identity, _ in batch})
            continue
        for identity, number in batch:
            try:
                if values.get('__errors__', {}).get(f'i{number}'):
                    raise ValueError(values['__errors__'][f'i{number}'])
                value = values[f'i{number}']
                if not isinstance(value, dict) or value.get('number') != number:
                    raise ValueError('关系查询未返回指定 issue')
                result = dict(converge='收尾', collections=[node(value['parent'], target)] if value.get('parent') else [])
                expected = {'blockedBy': value['issueDependenciesSummary']['totalBlockedBy'],
                            'subIssues': value['subIssuesSummary']['total']}
                for field, name in GROUPS.items():
                    part, nodes, cursors, total = value[field], [], set(), None
                    while True:
                        count, rows, info = part['totalCount'], part['nodes'], part['pageInfo']
                        if type(count) is not int or not isinstance(rows, list) or type(info['hasNextPage']) is not bool:
                            raise ValueError('关系分页格式无效')
                        if total is not None and count != total:
                            raise ValueError('关系在分页期间变化，下次重新读完整快照')
                        total = count
                        nodes.extend(node(v, target) for v in rows)
                        if not info['hasNextPage']:
                            break
                        cursor = info['endCursor']
                        if not isinstance(cursor, str) or not cursor or cursor in cursors:
                            raise ValueError('关系分页游标无效或重复')
                        cursors.add(cursor)
                        text = prefix + f'i{number}:issue(number:{number}){{{connection(field, cursor)}}}' + '}}'
                        page = query(io, text, deadline)
                        if page.get('__errors__', {}).get(f'i{number}'):
                            raise ValueError(page['__errors__'][f'i{number}'])
                        part = page[f'i{number}'][field]
                    if len(nodes) != total or len({n['id'] for n in nodes}) != total:
                        raise ValueError('关系数量不一致或包含不可见对象；不能放行')
                    if field in expected and (type(expected[field]) is not int or expected[field] != total):
                        raise ValueError('关系摘要与可见对象数不同，不能放行')
                    result[name] = sorted(nodes, key=lambda n: n['id'])
                snapshot = Relations(**result)
                snapshot.validate()
                results[identity] = snapshot
            except (Retryable, ValueError, KeyError, TypeError) as exc:
                results[identity] = Relations(error=str(exc))
    return results

