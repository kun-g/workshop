"""issue 写法改版（ws#286）：已过接单的旧写法活不受影响，检查按交付对象规则取，旧核对结论不沿用，派活说明讲约束。"""
import hashlib
import json

from workshop.core.models import Event, Receipt
from workshop.plugins.issue_delivery import dod
from conftest import item
from test_issue_delivery import BODY, edit, calls, snapshot, world  # noqa: F401
from test_issue_end_to_end import engine_for, start, set_body
from test_github_identity_flow import configure

# 兼容：改版前的旧写法正文——有依据、能跑的检查（多写了一条）、不可逆或对外的动作，没有约束节。
OLD = '''## Kun 的原话
检查失败时让错误可见。
## 依据
核心 R14
## 合并前 DoD
- 错误输出和非零退出码保留：`scripts/check.sh` 通过。
## 能跑的检查
```workshop-checks
[["scripts/check.sh"], ["echo", "from-issue"]]
```
## 不可逆或对外的动作
无
## 要不要亲眼看
否
'''


def old_issue(world):
    edit(world, issue={'number': 7, 'state': 'open', 'body': OLD})


def test_execution_with_old_issue_is_not_sent_back(world):
    old_issue(world)
    evidence = world['flow'].assess({**snapshot(world, '执行'), 'checks': [['echo', 'from-issue']]},
                                    Receipt('done', '做完'), world['target'])
    assert evidence.passed and evidence.handoff == '交付'
    assert evidence.checks == [['scripts/check.sh']]


def test_delivery_with_old_issue_runs_rule_checks_not_stored_ones(world):
    old_issue(world)
    data = {**snapshot(world, '交付'), 'checks': [['echo', 'from-issue']]}
    world['io'].save(data, world['target'], {'effects': {}, 'issue': 7})
    evidence = world['flow'].assess(data, Receipt('done', '程序完成', advance='收尾'), world['target'])
    assert evidence.passed, evidence.reason
    assert evidence.checks == [['scripts/check.sh']]
    assert [run['argv'] for run in json.loads(evidence.reason)['runs'] if run.get('kind') != 'prepare'] == [['scripts/check.sh']]


def test_delivery_uses_registered_required_checks(world):
    target = {**world['target'], 'required_checks': [['/bin/sh', '-c', 'echo project']]}
    world['io'].save(snapshot(world, '交付'), target, {'effects': {}, 'issue': 7})
    evidence = world['flow'].assess(snapshot(world, '交付'), Receipt('done', '程序完成', advance='收尾'), target)
    # PR 正文附的是 scripts/check.sh 这个名字，不是登记的检查：退回执行，但跑的是登记的那条。
    assert evidence.checks == [['/bin/sh', '-c', 'echo project']]
    pending = world['io'].read(snapshot(world, '交付'), target)['pending_return']
    assert pending['output'].startswith('$ /bin/sh -c echo project\nexit 0')


def test_item_still_at_intake_is_checked_by_new_rules(world):
    set_body(world, OLD)
    engine = engine_for(world, 'old-intake')
    start(engine)
    waiting = item(engine, '7')
    assert waiting['stage'] == '接单' and waiting['state'] == '等资料'
    assert waiting['next'].endswith(dod.CONSTRAINT)
    set_body(world, OLD + '## 约束\n无\n')
    engine.ingest(Event('github', 'issue-edited', 'alpha', '7', 'test', {'kind': 'issue', 'number': 7}))
    engine.tick()
    assert item(engine, '7')['stage'] == '执行'
    assert item(engine, '7')['checks'] == [['scripts/check.sh']]
    engine.log.close()


def auto_intake(world, monkeypatch, recorded):
    configure(world, monkeypatch)
    target = {**world['target'], 'auto_intake': True, 'intake_app_logins': ['lambdatheory-agent[bot]']}
    edit(world, issue={'number': 7, 'state': 'open', 'labels': [], 'user': {'login': 'kun-g'}, 'body': BODY})
    digest = hashlib.sha256(BODY.encode()).hexdigest()
    world['io'].save({'id': 'alpha:7'}, target, {'intake_versions': {digest: recorded}})
    event = Event('github', 'one', 'alpha', '7', '登记', {'kind': 'issue', 'number': '7'})
    return world['flow'].classify(event, None, target, None), target


def test_old_intake_conclusion_is_rechecked_once_under_new_rules(world, monkeypatch):
    """升级前同一版正文记下「缺能跑的检查」：不沿用，按新规则再核对一次，过了就补标签。"""
    value, target = auto_intake(world, monkeypatch, {'eligible': False, 'problems': ['缺「能跑的检查」节或其中的 workshop-checks 代码块']})
    assert value.action == 'ready' and not value.discard
    assert len([c for c in calls(world) if '/issues/7/labels' in str(c)]) == 1
    state = world['io'].read({'id': 'alpha:7'}, target)
    assert next(iter(state['intake_versions'].values()))['rules'] == 2


def test_new_intake_conclusion_is_kept(world, monkeypatch):
    value, _ = auto_intake(world, monkeypatch, {'eligible': False, 'problems': ['某项'], 'rules': 2})
    assert value.discard and '同版正文已核对' in value.summary
    assert not any('/labels' in str(c) for c in calls(world))


def test_brief_lists_rule_checks_and_constraint_rules(world):
    target = {**world['target'], 'required_checks': [['scripts/check.sh'], ['uv', 'run', 'lint-imports', '--config', 'a b.toml']]}
    value = world['flow'].brief({**snapshot(world, '执行'), 'allowed_advances': ['交付']}, '执行', target)
    text = value if isinstance(value, str) else value.instructions
    assert "- scripts/check.sh\n- uv run lint-imports --config 'a b.toml'\n" in text
    assert ("「约束」节是这件活必须遵守的限制；只做「约束」节里列出的不可逆或对外的动作，没列就是没有" in text)
    assert "发现约束和 DoD 做不到一起时，登记带推荐的取舍等待（decision），不自行放宽约束，不自行改 issue 正文。" in text
    assert "申请授权的流程照上面不变" in text and "需要授权时在中心申请一次" in text
    assert "只做 issue「不可逆或对外的动作」节列出的动作" not in text
