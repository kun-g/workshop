"""接单核对登记（核心 R2、R3，issue交付 R2、R10）：依据能回查，需求变更有原话；交付时同一个 PR 改了需求文档。"""
import pytest

from workshop.core.models import Receipt
from workshop.plugins.issue_delivery import dod
from conftest import item
from test_issue_delivery import BODY, PR_BODY, calls, edit, result, snapshot, world  # noqa: F401
from test_issue_end_to_end import engine_for, start


CHANGE = '需求变更：在 docs/requirements/核心.md 新增：失败后只通知一次。'
PROGRAM = Receipt('done', '程序完成', advance='执行')


def body_with(world, basis):
    body = BODY.replace('核心 R14', basis)
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


@pytest.mark.parametrize('basis,reason', [('', '依据'), ('核心 R99', 'R99'), ('R14', 'R14'),
                                        ('不存在 R14', 'R14'), ('只是一个建议', '依据')])
def test_missing_or_invalid_basis_waits_for_update(world, basis, reason):
    body_with(world, basis)
    evidence = intake(world)
    evidence.validate()
    assert not evidence.passed and evidence.awaits_update and reason in evidence.reason


def test_missing_heading_fails(world):
    edit(world, issue={'number': 7, 'body': BODY.replace('## 依据\n核心 R14\n', '')})
    evidence = intake(world)
    assert not evidence.passed and evidence.awaits_update and '依据' in evidence.reason


def test_directory_registration_reads_nested_docs_and_real_entries_only(world):
    world['target']['repo'] = 'another/project'
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/nested/核心.md': '**R14 真实需求。**\n提到了 **R99 假的**\n',
                                      'docs/requirements/约束.md': '**C1 约束。**\n'})
    body_with(world, '`核心 R14`、`约束 C1`')
    assert intake(world).passed
    body_with(world, '核心 R99')
    evidence = intake(world)
    assert not evidence.passed and 'R99' in evidence.reason


def test_ids_are_scoped_to_document_and_duplicate_stems_need_full_path(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/a/核心.md': '**R14 需求。**\n',
                                      'docs/requirements/b/核心.md': '**R2 另一条。**\n'})
    body_with(world, '核心 R14')
    assert not intake(world).passed
    body_with(world, 'docs/requirements/a/核心.md R14')
    assert intake(world).passed
    body_with(world, 'docs/requirements/b/核心.md R14')
    assert not intake(world).passed


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
    body_with(world, '已有需求：`docs/requirements/交付.md` R6；交付 R6。' + CHANGE)
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
    (('[["scripts/check.sh"]]', '[["echo", "pass"]]'), 'scripts/check.sh'),
    (('[["scripts/check.sh"]]', '[["scripts/check.sh"], []]'), '格式'),
    (('## 要不要亲眼看\n否', '## 要不要亲眼看\n再说'), '亲眼看'),
    (('## 不可逆或对外的动作\n无\n', ''), '不可逆或对外的动作'),
])
def test_registration_names_problems(change, reason):
    problems, _ = dod.registration(BODY.replace(*change), TARGET)
    assert any(reason in p for p in problems), problems


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
    # 能跑的检查只取自己那一节，不混进上线后的检查。
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
