"""开单来源核对（issue交付 R11）：认会话号的写法和 intake_require_session 开关。"""
import pytest
import yaml

from workshop.plugins.issue_delivery.intake import origin, trusted
from workshop.registry import load_rules
from conftest import configuration

APP = 'lambdatheory-agent[bot]'
TARGET = {'name': 'alpha', 'intake_app_logins': [APP]}

# 原话节原样取自 kun-g/workshop #242（「会话 <号>」）和 #248（「协调对话 <号>」）。
WROTE_SESSION = '''## Kun 的原话

2026-11-18，会话 `a96e1480-23b8-4fec-ad92-e3a16e3502ba`：

> 现在发版的周期是写死在代码里面的，还是可以配置的？

> 提个issue改吧

## 依据
定时发版 R1
'''
WROTE_COORDINATION = '''## Kun 的原话

2026-11-18，协调对话 `2ba67f64-4e2b-47b0-9213-00c871c6e28c`。协调对话查清 15:37 主检出被推进是派执行轮时 Orca 开工作区快进本地 main 造成的，建议改成用提交号开工作区。Kun：

> 开
'''
NO_QUOTE = WROTE_COORDINATION.replace('> 开\n', '开\n')
NO_SESSION = WROTE_COORDINATION.replace('`2ba67f64-4e2b-47b0-9213-00c871c6e28c`', '`<会话号>`')
# 会话号只出现在引用行里，不算写明（引用行是 Kun 的原话，不是开单会话的出处）。
SESSION_ONLY_QUOTED = NO_SESSION.replace('> 开', '> 开 2ba67f64-4e2b-47b0-9213-00c871c6e28c')
# 会话号写在原话节以外不算。
SESSION_ELSEWHERE = NO_SESSION + '\n## 交接材料\n会话 2ba67f64-4e2b-47b0-9213-00c871c6e28c\n'


def issue(body, author=APP):
    return {'user': {'login': author}, 'body': body}


@pytest.mark.parametrize('body', [WROTE_SESSION, WROTE_COORDINATION])
def test_both_documented_session_forms_are_trusted(body):
    assert origin(issue(body), TARGET) == []
    assert trusted(issue(body), TARGET)


@pytest.mark.parametrize('body,reason', [
    (NO_QUOTE, '没有 > 引用的原话'),
    (NO_SESSION, '没写会话号'),
    (SESSION_ONLY_QUOTED, '没写会话号'),
    (SESSION_ELSEWHERE, '没写会话号'),
    ('', '没有 > 引用的原话'),
])
def test_missing_quote_or_session_is_untrusted(body, reason):
    problems = origin(issue(body), TARGET)
    assert any(reason in p for p in problems), problems
    assert not trusted(issue(body), TARGET)


@pytest.mark.parametrize('require,body,expected', [
    (True, NO_SESSION, False),
    (False, NO_SESSION, True),
    (True, WROTE_COORDINATION, True),
    (False, WROTE_COORDINATION, True),
    (False, NO_QUOTE, False),  # 关掉开关仍要原话
])
def test_switch_only_drops_session_requirement(require, body, expected):
    assert trusted(issue(body), {**TARGET, 'intake_require_session': require}) is expected


@pytest.mark.parametrize('require', [True, False])
def test_switch_does_not_change_author_check(require):
    target = {**TARGET, 'intake_require_session': require}
    assert trusted(issue('', 'kun-g'), target)  # kun-g 本人开的不看原话节
    for author in ('stranger', None):
        problems = origin(issue(WROTE_COORDINATION, author), target)
        assert problems and '不在 intake_app_logins' in problems[0]


@pytest.mark.parametrize('value', ['false', 0, 1, None, []])
def test_rules_reject_non_boolean_switch(tmp_path, value):
    config = configuration()
    config['targets']['alpha'] = {'plugin': 'issue_delivery', 'repo': 'kun-g/workshop', 'sources': [],
                                   'intake_require_session': value}
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    with pytest.raises(ValueError, match='intake_require_session 必须是布尔值'):
        load_rules(path)


@pytest.mark.parametrize('value', [True, False])
def test_rules_accept_boolean_switch(tmp_path, value):
    config = configuration()
    config['targets']['alpha'] = {'plugin': 'issue_delivery', 'repo': 'kun-g/workshop', 'sources': [],
                                   'intake_require_session': value}
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    assert load_rules(path).target('alpha')['intake_require_session'] is value
