"""未标记的开放登记：回源核对、每版正文一次、符合接单核对才用 App 补标签。"""
import hashlib
import re
from workshop.core.models import Classification
from . import dod
from .requirements import section


def trusted(issue, target):
    author = (issue.get('user') or {}).get('login')
    if author == target.get('kun_github', 'kun-g'):
        return True
    quote = section(issue.get('body') or '', 'Kun 的原话')
    return (author in target.get('intake_app_logins', []) and
            bool(re.search(r'^\s*>\s*\S', quote, re.M)) and
            bool(re.search(r'会话[^\n]*[0-9a-f]{8}-[0-9a-f-]{27,}', quote)))


def inspect(io, event, item, target):
    number = str(event.body.get('number', ''))
    if not number.isdigit():
        return Classification(discard=True, summary='来源缺登记编号')
    issue = io.api(f'repos/{target["repo"]}/issues/{number}')
    if issue.get('number') != int(number) or 'pull_request' in issue:
        return Classification(discard=True, summary='回源对象不匹配')
    if item is not None:
        return Classification()  # 已有活仍收到正文更新；接单与关系端口回查内容。
    if issue.get('state') != 'open' or not trusted(issue, target):
        return Classification(discard=True, summary='未开放或开单来源未核实')
    label = target.get('label', 'Ready for Agent')
    body = issue.get('body') or ''
    digest = hashlib.sha256(body.encode()).hexdigest()
    identity = {'id': event.item_id}
    if any(v['name'] == label for v in issue.get('labels', [])):
        with io.locked(identity, target) as state:
            previous = state.get('intake_versions', {}).get(digest)
            if previous and previous.get('eligible') and not previous.get('labeled'):
                previous['labeled'] = True
                io.save(identity, target, state)
        # 标签的权限仍由标签事件触发器判；正文不能替标签授予 ready。
        return Classification(discard=True, summary='已带标签，交标签事件接单')
    with io.locked(identity, target) as state:
        previous = state.get('intake_versions', {}).get(digest)
        if previous and previous.get('labeled'):
            return Classification(discard=True, summary='同版正文已补过标签；移除标签后不自动重打')
        if previous is not None and not previous.get('eligible'):
            return Classification(discard=True, summary='同版正文已核对：' + '；'.join(previous['problems']))
        if previous is None:
            problems, _ = dod.registration(body, target, io.requirement_documents(target))
            state.setdefault('intake_versions', {})[digest] = {'eligible': not problems, 'problems': problems}
            io.save(identity, target, state)
            if problems:
                return Classification(discard=True, summary='未写全：' + '；'.join(problems))
        if target.get('github_identity', {}).get('mode') != 'app':
            raise ValueError('自动打标签只允许 App 身份；请修正本交付对象配置')
        # POST labels 是集合追加，响应丢失重试前上面的 GET 已回查标签，不重复副作用。
        io.api(f'repos/{target["repo"]}/issues/{number}/labels', data={'labels': [label]})
        state['intake_versions'][digest]['labeled'] = True
        io.save(identity, target, state)
    return Classification('ready', summary='回源核对写全，App 已补 Ready for Agent')
