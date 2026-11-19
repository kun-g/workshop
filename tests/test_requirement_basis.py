"""接单核对登记（核心 R2、R3，issue交付 R2、R10）：约束节写法，需求变更在背景里认、有原话；交付时同一个 PR 改了需求文档。"""
import pytest

from workshop.core.models import Receipt
from workshop.plugins.issue_delivery import dod
from conftest import item
from test_issue_delivery import BODY, PR_BODY, calls, edit, result, snapshot, world  # noqa: F401
from test_issue_end_to_end import engine_for, start


CHANGE = '需求变更：在 docs/requirements/核心.md 新增：失败后只通知一次。'
PROGRAM = Receipt('done', '程序完成', advance='执行')


def with_change(change, body=BODY):
    """在「背景和目标」里另起一段写需求变更（新写法）。"""
    return body.replace('（核心 R14）。\n', '（核心 R14）。\n\n' + change + '\n')


def legacy(basis, body=BODY):
    """兼容：旧写法的「依据」节。"""
    return body + '## 依据\n' + basis + '\n'


def body_with(world, change, write=with_change):
    body = write(change)
    edit(world, issue={'number': 7, 'state': 'open', 'body': body})
    return body


def intake(world):
    return world['flow'].assess(snapshot(world, '接单'), PROGRAM, world['target'])


def deliver(world):
    world['io'].save(snapshot(world, '交付'), world['target'], {'effects': {}, 'issue': 7})
    return world['flow'].assess(snapshot(world, '交付'), Receipt('done', '程序完成', advance='收尾'), world['target'])


def test_complete_registration_passes_with_checks(world):
    evidence = intake(world)
    assert evidence.passed and evidence.checks == [['scripts/check.sh']]


@pytest.mark.parametrize('basis', ['核心 R99', 'R14', '不存在 R14', '只是一个建议'])
def test_listed_requirement_numbers_are_not_checked(world, basis):
    """只列已有需求编号的不再核对编号存不存在；旧「依据」节写什么都不拦（兼容）。"""
    body_with(world, basis, legacy)
    assert intake(world).passed


def test_old_sections_are_not_required(world):
    """没有「依据」「能跑的检查」「不可逆或对外的动作」三节照样接单。"""
    assert all(f'## {name}' not in BODY for name in ('依据', '能跑的检查', '不可逆或对外的动作'))
    evidence = intake(world)
    assert evidence.passed and evidence.checks == [['scripts/check.sh']]


def test_directory_registration_reads_nested_docs_and_real_entries_only(world):
    world['target']['repo'] = 'another/project'
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/nested/核心.md': '**R14 真实需求。**\n',
                                      'docs/requirements/约束.md': '**C1 约束。**\n'})
    body_with(world, '需求变更：在 `核心` 新增：失败后只通知一次。')
    assert intake(world).passed
    edit(world, files=[{'filename': 'docs/requirements/核心.md', 'status': 'modified'}])
    assert not deliver(world).passed
    edit(world, files=[{'filename': 'docs/requirements/nested/核心.md', 'status': 'modified'}])
    assert deliver(world).passed


def test_duplicate_stems_need_full_path(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/a/核心.md': '**R14 需求。**\n',
                                      'docs/requirements/b/核心.md': '**R2 另一条。**\n'})
    body_with(world, '需求变更：在 核心 新增：失败后只通知一次。')
    evidence = intake(world)
    assert not evidence.passed and '不唯一' in evidence.reason
    body_with(world, '需求变更：在 docs/requirements/a/核心.md 新增：失败后只通知一次。')
    assert intake(world).passed


def test_change_needs_kun_quote_not_only_runtime_source(world):
    body = body_with(world, CHANGE)
    assert intake(world).passed
    edit(world, issue={'number': 7, 'body': body.replace('检查失败时让错误可见。', '来源：运行现象，见 workshop why ws#7')})
    evidence = intake(world)
    assert not evidence.passed and evidence.awaits_update and '原话' in evidence.reason
    edit(world, issue={'number': 7, 'body': body.replace('## Kun 的原话\n检查失败时让错误可见。\n', '')})
    assert not intake(world).passed


def test_change_without_text_fails(world):
    body_with(world, '需求变更：')
    evidence = intake(world)
    assert not evidence.passed and '需求原文' in evidence.reason


def test_change_cannot_escape_registered_directory(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    body_with(world, '需求变更：在 docs/requirements/../other.md 新增：错误原样保留。')
    evidence = intake(world)
    assert not evidence.passed and '需求文档' in evidence.reason


def test_invalid_rule_is_a_gate_failure(world):
    world['target']['requirement_docs'] = ['../secret.md']
    evidence = intake(world)
    assert not evidence.passed and 'requirement_docs' in evidence.reason


def test_change_without_quote_stays_at_intake_without_cards(world):
    """需求变更没有原话，车间不接单、不发卡（介入点 R3）；停在接单等写 issue 的会话补原话。"""
    body = body_with(world, CHANGE).replace('检查失败时让错误可见。', '来源：运行现象')
    edit(world, issue={'number': 7, 'body': body})
    engine = engine_for(world, 'requirements-quote')
    start(engine)
    waiting = item(engine, '7')
    assert waiting['stage'] == '接单' and waiting['state'] == '等资料' and '原话' in waiting['next']
    assert not engine.log.all('cards') and not engine.log.all('rounds')
    engine.log.close()


@pytest.mark.parametrize('files,passed', [
    ([{'filename': 'src/a.py'}], False),
    ([{'filename': 'docs/requirements/核心.md', 'status': 'removed'}], False),
    ([{'filename': 'archive/核心.md', 'previous_filename': 'docs/requirements/核心.md', 'status': 'renamed'}], False),
    ([{'filename': 'docs/requirements/核心.md', 'status': 'modified'}], True),
])
def test_delivery_requires_changed_requirement_file(world, files, passed):
    body_with(world, CHANGE)
    edit(world, files=files)
    evidence = deliver(world)
    assert evidence.passed == passed
    if not passed:
        assert '需求文档' in evidence.reason and '退回执行' in evidence.reason and evidence.handoff == '执行'


def test_new_requirement_file_in_registered_directory_counts(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    body_with(world, '需求变更：在 docs/requirements/new.md 新增：错误原样保留。')
    edit(world, files=[{'filename': 'docs/requirements/new.md', 'status': 'added'}])
    assert deliver(world).passed


def test_all_named_changed_documents_are_required(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/核心.md': '**R14 需求。**',
                                      'docs/requirements/交付.md': '**R6 验收。**'})
    body_with(world, CHANGE + ' 并在 docs/requirements/交付.md 同步这条要求。')
    edit(world, files=[{'filename': 'docs/requirements/核心.md', 'status': 'modified'}])
    evidence = deliver(world)
    assert not evidence.passed and 'docs/requirements/交付.md' in evidence.reason


def test_documents_only_cited_before_change_are_not_required(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/核心.md': '**R14 需求。**',
                                      'docs/requirements/交付.md': '**R6 验收。**'})
    # 兼容：旧「依据」节里段落中间的需求变更照旧认，变更前引用的文档不要求改。
    body_with(world, '已有需求：`docs/requirements/交付.md` R6；交付 R6。' + CHANGE, legacy)
    edit(world, files=[{'filename': 'docs/requirements/交付.md', 'status': 'modified'}])
    assert not deliver(world).passed
    edit(world, files=[{'filename': 'docs/requirements/核心.md', 'status': 'modified'}])
    assert deliver(world).passed


@pytest.mark.parametrize('separator', ['\n\n', '\n  \t\n', '\r\n\r\n'])
def test_documents_in_later_paragraph_are_not_required(world, separator):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/核心.md': '**R14 需求。**',
                                      'docs/requirements/交付.md': '**R6 验收。**'},
         files=[{'filename': 'docs/requirements/核心.md', 'status': 'modified'}])
    body_with(world, CHANGE + separator + '另外的说明（交付 R6）。')
    assert deliver(world).passed


def test_empty_change_does_not_consume_next_paragraph(world):
    body_with(world, '需求变更：\n\n核心.md R14')
    evidence = intake(world)
    assert not evidence.passed and '没写需求原文' in evidence.reason


def test_repair_keeps_merged_documents_but_requires_new_basis_document(world):
    """首个 PR 改文档并合入；执行交接清理合并状态后，修复 PR 沿用记录。"""
    target, flow, io = world['target'], world['flow'], world['io']
    target['requirement_docs'] = ['docs/requirements/']
    body_with(world, CHANGE)
    edit(world, files=[{'filename': 'docs/requirements/核心.md', 'status': 'modified'}])
    assert deliver(world).passed
    data = snapshot(world, '交付')
    assert not flow.after(data, '交付', 'first-merge', target).pending
    state = io.read(data, target)
    assert state['requirement_changes'] == {'docs/requirements/核心.md': state['merge']}
    assert state['merge'] == world['sha']

    edit(world, pr={'number': 12, 'body': PR_BODY, 'head': {'sha': world['sha']},
                    'state': 'open', 'merged': False}, files=[{'filename': 'src/a.py', 'status': 'modified'}])
    execution = snapshot(world, '执行')
    assert not flow.after(execution, '执行', 'repair-handoff', target).pending
    state = io.read(data, target)
    assert 'merge' not in state and state['requirement_changes'] == {'docs/requirements/核心.md': world['sha']}
    assert flow.assess(data, PROGRAM, target).passed

    body_with(world, CHANGE + '\n并在 docs/requirements/新增.md 新增：保留失败原文。')
    evidence = flow.assess(data, PROGRAM, target)
    assert not evidence.passed and evidence.handoff == '执行'
    assert '需求变更没改登记的需求文档：docs/requirements/新增.md' in evidence.reason
    # 另一件活不能借用这件活的合并记录。
    other = {**data, 'id': 'alpha:8', 'object_key': '8'}
    assert 'requirement_changes' not in io.read(other, target)


def test_merge_records_only_registered_added_or_modified_documents(world):
    target, flow, io = world['target'], world['flow'], world['io']
    target['requirement_docs'] = ['docs/requirements/']
    files = [
        {'filename': 'docs/requirements/新增.md', 'status': 'added'},
        {'filename': 'docs/requirements/核心.md', 'status': 'modified'},
        {'filename': 'docs/requirements/删除.md', 'status': 'removed'},
        {'filename': 'archive/旧.md', 'previous_filename': 'docs/requirements/旧.md', 'status': 'renamed'},
        {'filename': 'docs/requirements/纯移动.md', 'previous_filename': 'docs/requirements/旧名.md',
         'status': 'renamed', 'changes': 0},
        {'filename': 'docs/requirements/改名并修改.md', 'previous_filename': 'docs/requirements/之前.md',
         'status': 'renamed', 'changes': 2},
        {'filename': 'src/a.py', 'status': 'modified'},
    ]
    edit(world, files=files)
    assert deliver(world).passed
    data = snapshot(world, '交付')
    assert 'requirement_changes' not in io.read(data, target)
    assert not flow.after(data, '交付', 'merge', target).pending
    expected = {p: world['sha'] for p in ('docs/requirements/新增.md', 'docs/requirements/核心.md',
                                        'docs/requirements/改名并修改.md')}
    assert io.read(data, target)['requirement_changes'] == expected
    # 合并成功后本地保存丢失：回查已合并 PR，补记而不重新合并。
    state = io.read(data, target)
    for key in ('merge', 'requirement_changes'):
        state.pop(key)
    io.save(data, target, state)
    flow.after(data, '交付', 'retry-merge', target)
    assert io.read(data, target)['requirement_changes'] == expected
    assert len([c for c in calls(world) if c[1:3] == ['pr', 'merge']]) == 1


# dod.registration / dod.evidence_problems：只看文字结构。
TARGET = {'name': 'alpha'}


@pytest.mark.parametrize('change,reason', [
    (('- 错误输出和非零退出码保留：`scripts/check.sh` 通过。', '- 待定'), '待定'),
    (('## 要不要亲眼看\n否', '## 要不要亲眼看\n再说'), '亲眼看'),
    (('## 约束\n无\n', ''), dod.CONSTRAINT),
])
def test_registration_names_problems(change, reason):
    problems, _ = dod.registration(BODY.replace(*change), TARGET)
    assert any(reason in p for p in problems), problems


@pytest.mark.parametrize('constraint,ok', [
    (None, False),                                  # 缺约束
    ('无', True),                                   # 写「无」
    ('\n无\n', True),                               # 去掉空行后第一行单独是「无」
    ('- 不改 `src/workshop/core/`（核心 R22）\n- 允许在交付后打 tag', True),  # 列了条目
    ('1. 只改插件', True),
    ('写这张单特有的限制和允许的动作', False),          # 只有占位文字
    ('待补充：没有就写「无」', False),
    ('无，但不许改核心', False),                       # 「无」后面还跟着别的字
    ('无。', False),
    ('```\n- 代码块里的不算\n```', False),
])
def test_constraint_section(constraint, ok):
    body = BODY.replace('## 约束\n无\n', '' if constraint is None else f'## 约束\n{constraint}\n')
    problems, _ = dod.registration(body, TARGET)
    assert (dod.CONSTRAINT in problems) is (not ok), problems
    assert (problems == []) is ok


def test_constraint_problem_sentence():
    assert dod.CONSTRAINT == '「约束」节须单写「无」或至少列一条'


def test_old_runnable_section_is_ignored_and_checks_come_from_rules():
    """兼容：issue 里留着旧「能跑的检查」节不报错也不生效，多写的、写坏的都被忽略。"""
    old = ('## 能跑的检查\n```workshop-checks\n[["scripts/check.sh"], ["echo", "extra"]]\n```\n'
           '## 不可逆或对外的动作\n无\n## 依据\n核心 R99\n')
    assert dod.registration(BODY + old, TARGET) == ([], [['scripts/check.sh']])
    broken = '## 能跑的检查\n```workshop-checks\n[[]]\n```\n'
    assert dod.registration(BODY + broken, TARGET) == ([], [['scripts/check.sh']])
    rule = {**TARGET, 'required_checks': [['project-check', '--all'], ['lint']]}
    assert dod.registration(BODY + old, rule) == ([], [['project-check', '--all'], ['lint']])


@pytest.mark.parametrize('value', [[], [[]], [['']], 'scripts/check.sh', [['ok'], 'bad'], [[1]]])
def test_bad_required_checks_fail_at_rule_load(value):
    from workshop.plugins.issue_delivery import IssueDelivery
    with pytest.raises(ValueError, match='required_checks'):
        IssueDelivery.validate({**TARGET, 'required_checks': value})
    IssueDelivery.validate({**TARGET, 'required_checks': [['scripts/check.sh']]})
    IssueDelivery.validate(TARGET)


def test_checks_from_is_gone():
    import workshop.plugins.issue_delivery as plugin
    assert not hasattr(plugin, 'checks_from')


@pytest.mark.parametrize('body,expected', [
    # 背景段首的需求变更
    (with_change('需求变更：在 docs/requirements/核心.md 新增：只通知一次。'),
     '需求变更：在 docs/requirements/核心.md 新增：只通知一次。'),
    # 背景里只在句中提到「需求变更：」不算
    (with_change('这张单不是需求变更：只改实现。'), None),
    # 兼容：旧「依据」节里的需求变更
    (legacy('需求变更：在 docs/requirements/交付.md 新增：保留原文。'), '在 docs/requirements/交付.md 新增：保留原文。'),
    # 两处都有，以背景为准
    (legacy('需求变更：在 docs/requirements/交付.md 新增：保留原文。',
            with_change('需求变更：在 docs/requirements/核心.md 新增：只通知一次。')),
     '在 docs/requirements/核心.md 新增：只通知一次。'),
    # 都没有
    (legacy('核心 R14'), None),
])
def test_where_requirement_change_is_read(body, expected):
    from workshop.plugins.issue_delivery.requirements import changed_requirement, requirement_basis
    change = changed_requirement(requirement_basis(body))
    if expected is None:
        assert change is None
    else:
        assert change == expected.removeprefix('需求变更：')


def test_registration_accepts_ordered_items_and_missing_visual_section_means_no():
    body = BODY.replace('- 错误输出', '1. 错误输出').replace('## 要不要亲眼看\n否\n', '')
    assert dod.registration(body, TARGET) == ([], [['scripts/check.sh']])
    assert not dod.visual(body)


def test_post_release_section_needs_checks():
    problems, _ = dod.registration(BODY + '## 上线后 DoD\n- 线上看得到新版本\n', TARGET)
    assert any('上线后 DoD' in p for p in problems)
    body = BODY + '## 上线后 DoD\n- 线上：\n```workshop-checks\n[["scripts/post.sh"]]\n```\n'
    assert dod.registration(body, TARGET)[0] == []
    assert dod.post_checks(body) == [['scripts/post.sh']]
    # 要跑的检查只按规则，不混进上线后的检查。
    assert dod.registration(body, TARGET)[1] == [['scripts/check.sh']]


@pytest.mark.parametrize('pr,ok', [
    ('## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n  `scripts/check.sh`\n', True),
    ('## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n```\nraw check output\n```\n', True),
    ('## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n', False),
    ('## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。 `echo pass`\n', False),
    ('## 合并前 DoD\n- 别的项 `scripts/check.sh`\n', False),
    ('没有这一节', False),
])
def test_pr_evidence_matches_issue_items(pr, ok):
    assert (dod.evidence_problems(pr, BODY, [['scripts/check.sh']]) == []) is ok


def test_issue_without_merge_items_relies_on_rerun_checks():
    """在途旧写法 issue 没有合并前 DoD 项：只靠干净副本重跑检查，不要求 PR 这一节。"""
    old = BODY.replace('## 合并前 DoD\n- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n', '')
    assert dod.evidence_problems('没有这一节', old, [['scripts/check.sh']]) == []


def test_rule_load_rejects_bad_required_checks(tmp_path):
    import yaml
    from conftest import configuration
    from workshop.registry import load_rules
    config = configuration()
    config['targets']['alpha'] = {'plugin': 'issue_delivery', 'repo': 'kun-g/workshop', 'sources': [],
                                   'required_checks': ['scripts/check.sh']}
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    with pytest.raises(ValueError, match='required_checks 格式不对'):
        load_rules(path)
    config['targets']['alpha']['required_checks'] = [['scripts/check.sh', '--fast']]
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    assert load_rules(path).target('alpha')['required_checks'] == [['scripts/check.sh', '--fast']]
