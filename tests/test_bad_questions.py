"""gc#109 原始问卡回归：格式错误不能替换机器结论或耗掉重验机会。"""
import json
from pathlib import Path
import re

import pytest

from conftest import current, item, reach
from workshop.core.cards import question, validate_question
from workshop.core.models import Receipt, utcnow
from workshop.plugins.executors.local import runner
from test_issue_delivery import world, snapshot, result, edit  # noqa: F401
from test_issue_end_to_end import engine_for, ensure_round, hand_over, remote, start


BAD = json.loads((Path(__file__).parent / 'fixtures/bad_questions.json').read_text())


def samples(text):
    return [json.loads(s) for s in re.findall(r'```json\n(.*?)\n```', text, re.S)]


@pytest.mark.parametrize('stage,key', [('执行', 'question')])
def test_actual_brief_examples_pass_both_validators(world, stage, key):
    value = world['flow'].brief({**snapshot(world, stage), 'allowed_advances': ['执行']}, stage, world['target'])
    brief = value if isinstance(value, str) else value.instructions
    cards = [v['ask'][0] for v in samples(brief) if 'ask' in v]
    assert {'question', key} <= {c['key'] for c in cards}
    for card in cards:
        validate_question(card)
        Receipt.parse({'outcome': 'nothing', 'summary': '等待', 'ask': [card]}, utcnow())
    assert 'visual-acceptance' not in brief
    for text in ('subject 为非空字符串且≤30', 'decision 为非空字符串且≤42',
                 'context 为非空字符串（无长度上限）', 'reason 为非空字符串且≤60',
                 'key 为非空字符串且≤18', 'label 为非空字符串且≤18',
                 'effect 为非空字符串且≤60', 'approval/reversible', '2–3', 'Unicode',
                 '仅在本轮允许提问', '示例不授予提问或批准权限'):
        assert text in brief


@pytest.mark.parametrize('first,second', [(0, 0), (1, 1), (0, 1)])
def test_wrapper_two_bad_cards_blocks_without_failed_or_rerun(world, monkeypatch, first, second):
    engine = engine_for(world, 'bad-cards')
    start(engine)
    ensure_round(engine)
    assert ensure_round(engine)['stage'] == '执行'
    sha = world['sha']
    token = current(engine, '7')['token']
    evidence = [result(world, acceptance={'passed': True, 'head': sha, 'output': 'machine checks passed'})]
    def receipt(index):
        return {'outcome': 'done', 'summary': '机器通过，待亲眼看',
                'ask': BAD[index], 'evidence': evidence, 'leave': {'kind': '等你', 'about': '亲眼看'}}
    output = world['root'] / 'output.log'
    def emit(data):
        with output.open('a') as stream:
            stream.write(json.dumps({'type': 'result', 'result': json.dumps(data)}) + '\n')
    submissions = []
    def submit(directory, spec, env, name, data):
        submissions.append(data)
        try:
            engine.submit(token, data)
            return {'code': 0, 'stdout': '', 'stderr': ''}
        except ValueError as exc:
            return {'code': 1, 'stdout': '', 'stderr': str(exc)}
    def run(argv, prompt):
        text = prompt.read_text()
        assert '完整问卡要求' in text and '≤30' in text and 'approval/reversible' in text
        engine.log.rebuild()  # 第一次拒绝的计数和原文不能只留在进程内。
        emit(receipt(second))
        return 0
    monkeypatch.setattr(runner, 'submit', submit)
    emit(receipt(first))
    _, submitted = runner.proxy_receipt(world['root'], {'kind': 'claude', 'vendor_id': 'fake', 'retry_argv': ['fake']}, {}, output, 0, run)
    assert submitted['code'] == 0 and len(submissions) == 2
    assert all(r['outcome'] == 'done' for r in submissions)  # 包装器没有代交 failed。
    assert engine.submit(token, receipt(second)) is False
    engine.tick()
    stopped = item(engine, '7')
    assert (stopped['stage'], stopped['state'], stopped['blocked_by']) == ('执行', '卡住', '问卡格式不对')
    assert '修正问卡格式' in stopped['next']
    assert engine.log.all('cards') == []
    row = engine.log.get('rounds', token)
    assert row['outcome'] == 'nothing' and row['passed'] is None
    assert row['receipt']['evidence'] == evidence and len(row['question_rejections']) == 2
    assert row['question_rejections'][0]['receipt'] == receipt(first)
    assert row['question_rejections'][1]['receipt'] == receipt(second)
    engine.log.rebuild()
    assert engine.submit(token, receipt(second)) is False
    before = len(engine.log.all('rounds'))
    engine.tick()
    assert len(engine.log.all('rounds')) == before
    assert remote(world)['issue']['state'] == 'open'
    engine.log.close()


@pytest.mark.parametrize('bad', BAD)
def test_fix_in_same_round_keeps_normal_waiting(engine, bad):
    reach(engine, '验收')
    token = current(engine)['token']
    data = {'outcome': 'done', 'summary': '检查通过', 'ask': bad}
    with pytest.raises(ValueError, match='完整问卡要求'):
        engine.submit(token, data)
    data['ask'] = [{'key': 'visual-acceptance', **question('请亲眼验收', approval=True)}]
    assert engine.submit(token, data)
    engine.tick()
    assert item(engine)['state'] == '等你'
    assert len(engine.log.all('cards')) == 1


@pytest.mark.parametrize('extra', [dict(outcome='invalid'), dict(evidence={}), dict(advance='说清楚'),
                                 dict(leave={'kind': 'bad', 'about': 'x'}),
                                 dict(events=[{'source':'x','ext_id':'x','target':'unknown','object_key':'x','title':'x','body':{}}])])
def test_mixed_errors_never_downgrade(engine, extra):
    reach(engine, '验收')
    token = current(engine)['token']
    data = {'outcome': 'done', 'summary': '检查通过', 'ask': BAD[0], **extra}
    for _ in range(2):
        with pytest.raises(ValueError):
            engine.submit(token, data)
    row = engine.log.get('rounds', token)
    assert row['receipt'] is None and not row.get('question_blocked')


def test_non_card_rejection_resets_consecutive_count(engine):
    reach(engine, '验收')
    token = current(engine)['token']
    data = {'outcome': 'done', 'summary': '通过', 'ask': BAD[0]}
    for submission in [data, {**data, 'outcome': 'invalid'}, data]:
        with pytest.raises(ValueError):
            engine.submit(token, submission)
    assert engine.log.get('rounds', token)['receipt'] is None
