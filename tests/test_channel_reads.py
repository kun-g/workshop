"""逐卡读取故障：完成次数、缓存重放、混合结果与恢复。"""
from conftest import event, item
from workshop.core.cards import question
from workshop.core.engine import Engine
from workshop.core.jobs import Pending, encode, decode
from workshop.core.models import Answer, ChannelPoll
from workshop.core.report import status, why


def setup_cards(engine):
    ids = []
    for name in ('one', 'two', 'three'):
        engine.ingest(event(ext_id=name, key=name))
        current = item(engine, name)
        cid = engine.card(current, 'ask', 'ask', question('批准操作', approval=True))
        ids.append(cid)
        engine.change(current, state='等你')
    engine.channels()
    for n, cid in enumerate(ids, 1):
        engine.fact('card_changed', {'id': cid, 'external': f'Q{n}'}, engine.log.get('cards', cid)['item'])
    return ids


def test_mixed_reads_count_completed_attempts_survive_rebuild_and_recover(engine):
    first, second, third = setup_cards(engine)
    channel = engine.plugins.get('channel', 'channel')
    response = ChannelPoll([Answer(second, 'no')], [first, second, third], {first: 'HTTP 403'})
    channel.poll = lambda cards: response
    engine.channels()
    assert engine.log.get('cards', second)['answer'] == 'no'
    assert not engine.log.get('cards', second)['approved']
    for _ in range(12):
        # 模拟后台结果的序列化和新鲜期内重放，不能重复累计、重复消费回答。
        response = decode(encode(response))
        engine.channels()
    assert engine.log.get('cards', first)['read_failures'] == 1
    assert engine.log.get('cards', third)['read_failures'] == 0
    assert len([f for f in engine.log.facts() if f['kind'] == 'answer']) == 1
    for n in range(2, 10):
        response = ChannelPoll(checked=[first, third], errors={first: 'HTTP 403'})
        engine.channels()
        assert engine.log.get('cards', first)['read_failures'] == n
    assert '连续 9 次读取失败' not in status(engine.log)
    response = ChannelPoll(checked=[first, third], errors={first: 'HTTP 403 latest'})
    engine.channels()
    view = status(engine.log)
    assert 'Q1 连续 10 次读取失败：HTTP 403 latest' in view
    assert '卡住 1' in view and '负责会话核对通道读取权限和状态' in view
    assert not [c for c in engine.log.all('cards') if c['mode'] == 'notice']
    assert 'HTTP 403' in why(engine.log, 'alpha:one')
    engine.log.rebuild()
    assert status(engine.log) == view
    revived = Engine(engine.log, engine.rules, engine.plugins, engine.clock)
    response = ChannelPoll([Answer(first, 'yes', True)], [first, third])
    revived.channels()
    assert item(revived)['state'] == '空闲'
    assert revived.log.get('cards', first)['read_failures'] == 0
    assert '连续 10 次读取失败' not in status(revived.log)
    assert item(revived, 'three')['state'] == '等你'
    assert not revived.control(f'delivery:read:{first}').get('failed')
    revived.channels()
    assert len([f for f in revived.log.facts() if f['kind'] == 'answer']) == 2


def test_successful_waiting_read_resets_streak_and_preserves_other_block(engine):
    first, second, third = setup_cards(engine)
    channel = engine.plugins.get('channel', 'channel')
    response = None
    channel.poll = lambda cards: response
    for _ in range(10):
        response = ChannelPoll(checked=[first], errors={first: 'unavailable'})
        engine.channels()
    engine.change(item(engine), state='卡住', blocked_by='独立故障', next='修复独立故障')
    response = ChannelPoll(checked=[first])  # 成功读取 waiting，不是回答。
    engine.channels()
    assert engine.log.get('cards', first)['read_failures'] == 0
    assert not engine.log.get('cards', first)['answered']
    for _ in range(9):
        response = ChannelPoll(checked=[first], errors={first: 'unavailable'})
        engine.channels()
    assert 'Q1 连续' not in status(engine.log)
    response = ChannelPoll([Answer(first, 'yes', True)], [first])
    engine.channels()
    assert item(engine)['state'] == '卡住' and item(engine)['blocked_by'] == '独立故障'


def test_pending_is_not_a_failure_and_batch_errors_are_per_card(engine):
    first, second, third = setup_cards(engine)
    channel = engine.plugins.get('channel', 'channel')
    def pending(cards):
        raise Pending('background')
    channel.poll = pending
    for _ in range(12):
        engine.channels()
    assert not engine.log.get('cards', first).get('read_failures')
    def fail(cards):
        raise OSError('connection failed')
    channel.poll = fail
    for _ in range(10):
        engine.channels()
    assert all(engine.log.get('cards', cid)['read_failures'] == 10 for cid in (first, second, third))
    assert '卡住 3' in status(engine.log)
    # 稍后的成功空读解除通道级错误；逐卡恢复必须有对应 checked，不能清其他卡。
    channel.poll = lambda cards: ChannelPoll(checked=[second])
    engine.channels()
    assert engine.log.get('cards', first)['read_failures'] == 10
    assert engine.log.get('cards', second)['read_failures'] == 0
    assert not engine.control('delivery:channel:channel').get('failed')
