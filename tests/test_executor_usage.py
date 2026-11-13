from dataclasses import asdict
from pathlib import Path
import shutil

import pytest
from workshop.plugins.executors.local.usage import collect, claude_entries, codex_entries

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
