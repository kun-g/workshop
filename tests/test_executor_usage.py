from dataclasses import asdict
from pathlib import Path
import shutil

import pytest
from workshop.plugins.executors.local.usage import collect
from workshop.plugins.executors.local.drivers.claude import claude_entries
from workshop.plugins.executors.local.drivers.codex import codex_entries

FIXTURES = Path(__file__).parent/'fixtures'/'executors'
SINCE = '2026-11-13T01:00:00+00:00'


def fixture(name, to):
    to.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES/name, to)


def test_claude_round_dedup_ttl_and_children(tmp_path):
    root = tmp_path/'.claude/projects/-repo'
    fixture('claude.jsonl',root/'main.jsonl')
    fixture('claude-child.jsonl',root/'main/subagents/agent-one.jsonl')
    fixture('claude-child.jsonl',root/'main/subagents/agent-two.jsonl')
    result = collect('claude',tmp_path,'main','/repo',SINCE,'subscription')
    assert result.main.model_actual == 'claude-opus-5-5'
    assert (result.main.input,result.main.cache_read,result.main.cache_write,result.main.output) == (100,200,50,20)
    assert result.main.cost_usd == pytest.approx((100*4+200*.2+30*5+20*8+20*20)/1e6)
    assert result.children_reported
    assert len(result.children) == 1
    child = result.children[0]
    assert child.model_actual == 'claude-sonnet-4-6'
    assert (child.input,child.cache_read,child.cache_write,child.output) == (40,60,0,20)
    assert child.cost_usd == pytest.approx(2*(20*3+30*.3+10*15)/1e6)


def test_codex_round_dedup_inclusive_cache_and_children(tmp_path):
    root = tmp_path/'.codex/sessions/2026/11/13'
    fixture('codex.jsonl',root/'rollout-main.jsonl')
    fixture('codex-child.jsonl',root/'rollout-child.jsonl')
    result = collect('codex',tmp_path,'codex-main','/repo',SINCE,'subscription')
    assert result.main.model_actual == 'gpt-6-astra'
    assert (result.main.input,result.main.cache_read,result.main.cache_write,result.main.output) == (100,200,0,20)
    assert result.main.cost_usd == pytest.approx((100*10+200+20*50)/1e6)
    assert result.children_reported
    child = result.children[0]
    assert child.model_actual == 'gpt-6.1-sol'
    assert (child.input,child.cache_read,child.cache_write,child.output) == (20,30,0,10)


@pytest.mark.parametrize('kind',['codex','claude'])
def test_missing_is_unknown(tmp_path,kind):
    result = collect(kind,tmp_path,'missing','/repo',SINCE,'subscription')
    assert all(value is None for key,value in asdict(result.main).items() if key != 'billing')
    assert result.main.billing == 'subscription'
    assert not result.children_reported


def test_unknown_cache_ttl_is_unknown_cost():
    entries = claude_entries([{'type':'assistant','timestamp':SINCE,'message':{'id':'m','model':'claude-opus-5-5',
        'usage':{'input_tokens':1,'cache_read_input_tokens':2,'cache_creation_input_tokens':3,'output_tokens':4}}}],SINCE,'subscription')
    assert entries[0].cache_write == 3
    assert entries[0].cost_usd is None


def test_missing_codex_cache_field_not_zero():
    records = [{'type':'turn_context','payload':{'model':'gpt-6-astra'}},
        {'type':'event_msg','timestamp':SINCE,'payload':{'type':'token_count','info':{
            'total_token_usage':{'input_tokens':100,'output_tokens':10},
            'last_token_usage':{'input_tokens':100,'output_tokens':10}}}}]
    entry = codex_entries(records,SINCE,'subscription')[0]
    assert entry.cache_read is None and entry.cache_write is None
    assert entry.input is None and entry.cost_usd is None
    assert entry.output == 10


def test_unreadable_child_marks_incomplete(tmp_path):
    root = tmp_path/'.claude/projects/-repo'
    fixture('claude.jsonl',root/'main.jsonl')
    child = root/'main/subagents/agent-bad.jsonl'
    child.parent.mkdir(parents=True)
    child.write_text('broken json\n')
    assert not collect('claude',tmp_path,'main','/repo',SINCE,'subscription').children_reported


def test_missing_referenced_child_not_hidden_by_old_child(tmp_path):
    import json
    root = tmp_path/'.claude/projects/-repo'
    fixture('claude.jsonl',root/'main.jsonl')
    fixture('claude-child.jsonl',root/'main/subagents/agent-old.jsonl')
    with (root/'main.jsonl').open('a') as stream:
        stream.write(json.dumps({'type':'user','timestamp':SINCE,'toolUseResult':{'agentId':'missing'}})+'\n')
    assert not collect('claude',tmp_path,'main','/repo',SINCE,'subscription').children_reported


def test_real_synthetic_record_does_not_poison_actual_usage(tmp_path):
    root = tmp_path/'.claude/projects/-repo'
    fixture('claude.jsonl', root/'main.jsonl')
    before = collect('claude', tmp_path, 'main', '/repo', SINCE, 'subscription')
    with (root/'main.jsonl').open('a') as stream:
        stream.write((FIXTURES/'claude-synthetic.jsonl').read_text())
    after = collect('claude', tmp_path, 'main', '/repo', SINCE, 'subscription')
    assert after.main == before.main
    assert after.main.model_actual == 'claude-opus-5-5'
    assert after.main.cost_usd is not None


def test_missing_model_zero_placeholder_is_skipped():
    import json
    record = json.loads((FIXTURES/'claude-synthetic.jsonl').read_text())
    record['message'].pop('model')
    assert claude_entries([record], SINCE, 'subscription') == []


def test_haiku_tier_is_per_message_and_counts_both_caches():
    records = []
    for i, ordinary in enumerate((20_000, 20_001)):
        records.append({'type': 'assistant', 'timestamp': SINCE, 'message': {
            'id': str(i), 'model': 'claude-haiku-5-5', 'usage': {
                'input_tokens': ordinary, 'cache_read_input_tokens': 70_000,
                'cache_creation_input_tokens': 10_000, 'output_tokens': 100,
                'cache_creation': {'ephemeral_5m_input_tokens': 6_000, 'ephemeral_1h_input_tokens': 4_000}}}})
    entries = claude_entries(records, SINCE, 'subscription')
    assert entries[0].cost_usd == pytest.approx((20_000*.1+70_000*.01+6_000*.125+4_000*.2+100*.5)/1e6)
    assert entries[1].cost_usd == pytest.approx((20_001*.5+70_000*.05+6_000*.625+4_000*1+100*2.5)/1e6)
    from workshop.plugins.executors.local.usage import aggregate
    assert aggregate(entries, 'subscription').cost_usd == pytest.approx(sum(e.cost_usd for e in entries))


@pytest.mark.parametrize('model,rates', [
    ('claude-opus-5', (5, .5, 6.25, 10, 25)),
    ('claude-sonnet-5', (2, .2, 2.5, 4, 10)),
    ('claude-fable-5', (10, 1, 12.5, 20, 50)),
    ('claude-fable-5-1', (10, .25, 12.5, 20, 50)),
    ('claude-fable-5-2', (8, .2, 10, 16, 40)),
    ('gpt-6-astra', (10, 1, 12.5, 25, 50)),
    ('gpt-6.1-sol', (2, .1, 2.5, 5, 10)),
    ('gpt-6.1-terra', (1, .05, 1.25, 2.5, 5)),
    ('gpt-6-luna', (.1, .01, .125, .25, .5)),
    ('gpt-6.2-astra', (8, .4, 10, 20, 40)),
    ('gpt-6.2-sol', (2, .1, 2.5, 5, 10)),
])
def test_dated_current_price_rows(model, rates):
    from workshop.plugins.executors.local.pricing import PRICES, cost, ANTHROPIC, OPENAI
    assert cost(model, 11, 13, 17, 19, 23) == pytest.approx(sum(a*b for a,b in zip((11,13,17,19,23), rates))/1e6)
    for row in PRICES.values():
        assert row.checked in ('2026-11-13', '2026-11-19')
        assert row.source in (ANTHROPIC, OPENAI)
    assert cost('unknown-model', 1, 2, 3, 4, 5) is None
