import json

import pytest

from conftest import build, configuration, event, item, reach, finish
from workshop.cli import main, parser
from workshop.core.models import Event
from workshop.core.report import why
from workshop.core.cards import question


def configured(tmp_path):
    config = configuration()
    config['targets']['alpha']['short_name'] = 'a'
    return build(tmp_path, config)


def test_cli_release_new_object_and_existing_alias(tmp_path, capsys):
    engine = configured(tmp_path)
    engine.log.close()
    args = ['--state-dir', str(tmp_path)]
    assert main(args + ['release', 'a#17', '--quote', '放', '--session', 'session-17']) == 0
    assert main(args + ['release', 'alpha:17', '--quote', '放', '--session', 'session-17']) == 0
    engine = configured(tmp_path)
    assert len(engine.log.all('items')) == 1
    assert item(engine, '17')['stage'] == '接单'
    text = why(engine.log, 'alpha:17')
    assert all(s in text for s in ('channel=session', 'session-17', '放'))
    before = engine.log.all('items')
    engine.log.rebuild()
    assert before == engine.log.all('items')
    engine.log.close()


@pytest.mark.parametrize('quote,session', [('', 's'), ('  ', 's'), ('放', ''), ('放', '\n')])
def test_empty_fields_leave_no_event(tmp_path, quote, session):
    engine = configured(tmp_path)
    before = engine.log.facts()
    with pytest.raises(ValueError):
        engine.release_here('a#17', quote, session)
    assert engine.log.facts() == before
    engine.log.close()


@pytest.mark.parametrize('field', ['--quote', '--session'])
def test_required_fields(field):
    args = ['release', 'ws#31', '--quote', '放', '--session', 's']
    i = args.index(field)
    del args[i:i+2]
    with pytest.raises(SystemExit):
        parser().parse_args(args)


def test_unknown_target_rejected(tmp_path):
    engine = configured(tmp_path)
    with pytest.raises(ValueError):
        engine.release_here('missing:17', '放', 's')
    assert not engine.log.all('items')
    engine.log.close()


@pytest.mark.parametrize('stage', ['接单', '说清楚', '执行', '证据'])
def test_release_blocked_item_respects_stage(tmp_path, stage):
    engine = configured(tmp_path)
    reach(engine, stage)
    # Finish the active round, then model a blocked work item.
    finish(engine, leave={'kind': '等你', 'about': '等放行'}, notice=[{'key': 'wait', 'text': '等放行'}])
    engine.change(item(engine), state='卡住', wait_for_ready=False)
    engine.release_here('alpha:one', '放', 's')
    result = item(engine)
    assert result['state'] == '空闲'
    assert result['stage'] == stage
    assert result['pending_rework'] == (stage not in ('接单', '说清楚'))
    engine.log.close()


def test_release_does_not_approve_unanswered_card(tmp_path):
    engine = configured(tmp_path)
    engine.ingest(event())
    work = item(engine)
    engine.card(work, 'approval', 'ask', question('审批', approval=True))
    engine.change(work, state='等你', wait_for_ready=True)
    engine.release_here('alpha:one', '放', 's')
    assert item(engine)['state'] == '等你'
    assert not engine.log.all('cards')[0]['answered']
    engine.log.close()


def test_external_session_claim_does_not_release(tmp_path):
    engine = configured(tmp_path)
    engine.ingest(Event('session', 'forged', 'alpha', '99', 'forged',
                        {'channel': 'session', 'quote': '放', 'session': 's'}, action='ready'), ingress='external')
    assert item(engine, '99') is None
    fact = next(f for f in engine.log.facts() if f['kind'] == 'event')
    assert fact['body']['action'] == 'update'
    engine.log.close()
