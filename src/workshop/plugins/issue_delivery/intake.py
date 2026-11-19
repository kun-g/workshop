"""未标记的开放登记：回源核对、每版正文一次、符合接单核对才用 App 补标签。"""
import hashlib
import re
from workshop.core.models import Classification
from . import dod
from .requirements import section


DRAFT = '【Draft】'
SESSION = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


def validate(target):
    """规则装配时调用：开关只收布尔值，写错不当成开或关。"""
    if type(target.get('intake_require_session', True)) is not bool:
        raise ValueError(f'{target.get("name", "交付对象")} 的 intake_require_session 必须是布尔值')


def origin(issue, target):
    """开单来源核对（issue交付 R11）；返回认不出的原因，空列表是可信。"""
    author = (issue.get('user') or {}).get('login')
    kun = target.get('kun_github', 'kun-g')
    if author == kun:
        return []
    if author not in target.get('intake_app_logins', []):
        return [f'开单来源：作者 {author or "（无）"} 不是 {kun}，也不在 intake_app_logins']
    validate(target)
    lines = section(issue.get('body') or '', 'Kun 的原话').splitlines()
    problems = []
    if not any(re.match(r'\s*>\s*\S', line) for line in lines):
        problems.append('开单来源：「Kun 的原话」节没有 > 引用的原话')
    if target.get('intake_require_session', True) and not any(
            SESSION.search(line) for line in lines if not re.match(r'\s*>', line)):
        problems.append('开单来源：「Kun 的原话」节引用行以外没写会话号（8-4-4-4-12 位 UUID）')
    return problems


def drafted(issue):
    """介入点 R3、R4：标题原样含【Draft】（全角括号、D 大写）的 issue 不核对、不接单、不开工；正文里的不算。"""
    return DRAFT in (issue.get('title') or '')


def trusted(issue, target):
    return not origin(issue, target)


def inspect(io, event, item, target):
    number = str(event.body.get('number', ''))
    if not number.isdigit():
        return Classification(discard=True, summary='来源缺登记编号')
    issue = io.api(f'repos/{target["repo"]}/issues/{number}')
    if issue.get('number') != int(number) or 'pull_request' in issue:
        return Classification(discard=True, summary='回源对象不匹配')
    if item is not None:
        return Classification()  # 已有活仍收到正文更新；接单与关系端口回查内容。
    if drafted(issue):
        # 不记核对版本：去掉标记后同一版正文仍要核对一次。
        return Classification(discard=True, summary=f'标题带{DRAFT}，不核对、不接单')
    problems = origin(issue, target)
    if issue.get('state') != 'open' or problems:
        return Classification(discard=True, summary='未开放或开单来源未核实' + ('：' + '；'.join(problems) if problems else ''))
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
