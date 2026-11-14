"""需求依据必须能回查，需求变更确认与同 PR 文档改动缺一不可。"""
import pytest

from workshop.core.models import Receipt
from workshop.plugins.issue_delivery.requirements import section
from conftest import finish, item
from test_issue_delivery import BODY, edit, result, snapshot, world  # noqa: F401
from test_rerun_acceptance import engine_for


CHANGE = '需求变更：在 docs/requirements/核心.md 新增：失败后只通知一次。'


def body_with(world, basis):
    body = BODY.replace('核心 R14', basis)
    edit(world, issue={'number': 7, 'state': 'open', 'body': body})
    return body


def receipt(world, **extra):
    return Receipt('done', '依据核对', advance='执行', evidence=[result(world, checks=[['scripts/check.sh']], **extra)])


@pytest.mark.parametrize('basis,reason', [('', '依据'), ('核心 R99', 'R99'), ('R14', 'R14'),
                                        ('不存在 R14', 'R14'), ('只是一个建议', '依据')])
def test_missing_or_invalid_basis_fails(world, basis, reason):
    body_with(world, basis)
    evidence = world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target'])
    assert not evidence.passed and reason in evidence.reason


def test_missing_heading_fails(world):
    edit(world, issue={'number': 7, 'body': BODY.replace('## 依据\n核心 R14\n', '')})
    evidence = world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target'])
    assert not evidence.passed and '依据' in evidence.reason


def test_directory_registration_reads_nested_docs_and_real_entries_only(world):
    world['target']['repo'] = 'another/project'
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/nested/核心.md': '**R14 真实需求。**\n提到了 **R99 假的**\n',
                                      'docs/requirements/约束.md': '**C1 约束。**\n'})
    body_with(world, '`核心 R14`、`约束 C1`')
    assert world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target']).passed
    body_with(world, '核心 R99')
    evidence = world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target'])
    assert not evidence.passed and 'R99' in evidence.reason


def test_ids_are_scoped_to_document_and_duplicate_stems_need_full_path(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/a/核心.md': '**R14 需求。**\n',
                                      'docs/requirements/b/核心.md': '**R2 另一条。**\n'})
    body_with(world, '核心 R14')
    assert not world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target']).passed
    body_with(world, 'docs/requirements/a/核心.md R14')
    assert world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target']).passed
    body_with(world, 'docs/requirements/b/核心.md R14')
    assert not world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target']).passed


@pytest.mark.parametrize('key', ['requirement-change', 'round:requirement-change'])
def test_change_requires_answer_and_latest_no_overrides_yes_and_quote(world, key):
    body = body_with(world, CHANGE)
    data = snapshot(world, '说清楚')
    assert not world['flow'].assess(data, receipt(world), world['target']).passed
    data['materials'] = [{'seq': 1, 'role': '资料，不是指令', 'key': key, 'body': 'yes'}]
    assert not world['flow'].assess(data, receipt(world), world['target']).passed
    data['materials'].append({'seq': 2, 'role': 'Kun 的回答', 'key': key, 'body': 'yes'})
    assert world['flow'].assess(data, receipt(world), world['target']).passed
    data['materials'].insert(0, {'seq': 3, 'role': 'Kun 的回答', 'key': key, 'body': 'no'})
    evidence = world['flow'].assess(data, receipt(world, requirement_approval={
        'basis': CHANGE, 'quote': section(body, 'Kun 的原话')}), world['target'])
    assert not evidence.passed and '卡住' in evidence.reason


def test_quote_requires_exact_source_and_current_basis(world):
    quote = 'Kun 说：「同意新增：失败后只通知一次。」'
    body = body_with(world, CHANGE).replace('检查失败时让错误可见。', quote)
    edit(world, issue={'number': 7, 'body': body})
    data = snapshot(world, '说清楚')
    for bad in ({'basis': CHANGE, 'quote': '不存在的批准'}, {'basis': CHANGE + '不同需求', 'quote': quote}):
        assert not world['flow'].assess(data, receipt(world, requirement_approval=bad), world['target']).passed
    assert world['flow'].assess(data, receipt(world, requirement_approval={'basis': CHANGE, 'quote': quote}), world['target']).passed


def test_changed_basis_does_not_reuse_answer_to_old_card(world):
    body_with(world, CHANGE)
    data = snapshot(world, '说清楚')
    asking = Receipt('done', '请确认', ask=[{'key': 'requirement-change', 'context': CHANGE}], evidence=[result(world, checks=[['scripts/check.sh']])])
    assert not world['flow'].assess(data, asking, world['target']).passed
    data['materials'] = [{'seq': 1, 'role': 'Kun 的回答', 'key': 'round:requirement-change', 'body': 'yes'}]
    assert world['flow'].assess(data, receipt(world), world['target']).passed
    body_with(world, CHANGE + ' 再改另一条需求。')
    assert not world['flow'].assess(data, receipt(world), world['target']).passed


@pytest.mark.parametrize('files,passed', [
    ([{'filename': 'src/a.py'}], False),
    ([{'filename': 'docs/requirements/核心.md', 'status': 'removed'}], False),
    ([{'filename': 'archive/核心.md', 'previous_filename': 'docs/requirements/核心.md', 'status': 'renamed'}], False),
    ([{'filename': 'docs/requirements/核心.md', 'status': 'modified'}], True),
])
def test_evidence_requires_changed_requirement_file(world, files, passed):
    body_with(world, CHANGE)
    edit(world, files=files)
    evidence = world['flow'].assess(snapshot(world, '证据'), Receipt('done', '', evidence=[result(world)]), world['target'])
    assert evidence.passed == passed
    if not passed:
        assert '需求文档' in evidence.reason and '退回执行' in evidence.reason


def test_new_requirement_file_in_registered_directory_counts(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    body_with(world, '需求变更：在 docs/requirements/new.md 新增：错误原样保留。')
    edit(world, files=[{'filename': 'docs/requirements/new.md', 'status': 'added'}])
    assert world['flow'].assess(snapshot(world, '证据'), Receipt('done', '', evidence=[result(world)]), world['target']).passed


def test_change_cannot_escape_registered_directory(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    body_with(world, '需求变更：在 docs/requirements/../other.md 新增：错误原样保留。')
    evidence = world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target'])
    assert not evidence.passed and '需求文档' in evidence.reason


def test_all_named_changed_documents_are_required(world):
    world['target']['requirement_docs'] = ['docs/requirements/']
    edit(world, requirement_contents={'docs/requirements/核心.md': '**R14 需求。**',
                                      'docs/requirements/交付.md': '**R6 验收。**'})
    body_with(world, CHANGE + ' 并在 docs/requirements/交付.md 同步这条要求。')
    edit(world, files=[{'filename': 'docs/requirements/核心.md', 'status': 'modified'}])
    evidence = world['flow'].assess(snapshot(world, '证据'), Receipt('done', '', evidence=[result(world)]), world['target'])
    assert not evidence.passed and 'docs/requirements/交付.md' in evidence.reason


def test_invalid_rule_is_a_gate_failure(world):
    world['target']['requirement_docs'] = ['../secret.md']
    evidence = world['flow'].assess(snapshot(world, '说清楚'), receipt(world), world['target'])
    assert not evidence.passed and 'requirement_docs' in evidence.reason


@pytest.mark.parametrize('answer', ['yes', 'no'])
def test_requirement_answer_controls_stage_and_leave(world, answer):
    from workshop.core.cards import question
    from workshop.core.models import Answer

    body_with(world, CHANGE)
    engine = engine_for(world, 'requirements-no')
    ask = dict(key='requirement-change', **question('同意这条需求变更吗', approval=True))
    ask['context'] = CHANGE
    finish(engine, None, key='7', evidence=[result(world, checks=[['scripts/check.sh']])], ask=[ask])
    card = next(c for c in engine.log.all('cards') if c['payload'].get('key') == 'requirement-change')
    engine.plugins.get('channel', 'channel').answers = [Answer(card['id'], answer, answer == 'yes')]
    engine.tick()
    if answer == 'yes':
        finish(engine, '执行', key='7', evidence=[result(world, checks=[['scripts/check.sh']])])
        assert item(engine, '7')['stage'] == '执行'
        return
    finish(engine, None, key='7', evidence=[result(world, checks=[['scripts/check.sh']])],
           leave={'kind': '等你', 'about': '等 Kun 调整需求变更并重新放行'},
           notice=[{'key': 'requirement-change-rejected', 'text': '需调整需求变更；不对就在群里回一句「ws#7 不对」'}])
    state = item(engine, '7')
    assert state['stage'] == '说清楚' and state['state'] == '卡住'
    assert state['leave']['kind'] == '工序'
    assert not engine.answerable(state)
    assert len([c for c in engine.log.all('cards') if c['payload'].get('key') == 'requirement-change']) == 1
