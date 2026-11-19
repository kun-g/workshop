"""ws#287 核心 R25：Claude Code 的壳经本机网关跑别家的模型（orca-claude-gpt）。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading

import pytest
import yaml

from workshop.core.models import Brief
from workshop.plugins.executors.local import adapters
from workshop.plugins.executors.local.drivers.claude import Driver as Claude, claude_entries
from workshop.plugins.executors.local.isolation import session_environment
from workshop.plugins.selectors.experiment import ExperimentSelector, bucket
from workshop.plugins.selectors.quota import QuotaSelector
from workshop.registry import load_rules
from conftest import FakeExecutor, build, configuration, event
from test_executors import setup, wait_for  # noqa: F401  (pytest fixture)
from test_executor_profiles import RULE_PATH, config_with, profile

GATEWAY = {'base_url': 'http://127.0.0.1:3425', 'token': 'magpie'}
SINCE = '2026-11-19T01:00:00+00:00'


def gateway_profile(**extra):
    return profile('claude', gateway=dict(GATEWAY), family='codex', **extra)


def load(tmp_path, profiles):
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(config_with(profiles)))
    return load_rules(path)


def free_port():
    """本机一个没人监听的端口：绑上再关掉。"""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def fake_gateway(port=0):
    class Root(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', port), Root)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_address[1]}'
    finally:
        server.shutdown()
        server.server_close()


# 第一条 DoD：gateway 字段、环境变量、报错。

def test_gateway_becomes_base_url_and_auth_token_never_api_key():
    env = Claude().environment(gateway_profile())
    assert env == {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:3425', 'ANTHROPIC_AUTH_TOKEN': 'magpie'}


def test_subscription_session_keeps_profile_gateway_but_strips_process_secrets():
    base = {'HOME': '/h', 'PATH': '/bin', 'ANTHROPIC_API_KEY': 'metered', 'ANTHROPIC_AUTH_TOKEN': 'process-token',
            'OPENAI_API_KEY': 'metered', 'CURSOR_API_KEY': 'metered'}
    spec = {'readonly': False, 'billing': 'subscription',
            'env': {'WORKSHOP_ITEM': 'a:1', **Claude().environment(gateway_profile())}}
    env = session_environment(spec, base)
    assert env['ANTHROPIC_AUTH_TOKEN'] == 'magpie' and env['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:3425'
    for key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'CURSOR_API_KEY'):
        assert key not in env
    # 没写 gateway 的配置档：和以前一样，进程带来的四个密钥都清掉。
    plain = session_environment({'readonly': False, 'billing': 'subscription', 'env': {'WORKSHOP_ITEM': 'a:1'}}, base)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'OPENAI_API_KEY', 'CURSOR_API_KEY'):
        assert key not in plain
    assert plain == {'HOME': '/h', 'PATH': '/bin', 'WORKSHOP_ITEM': 'a:1'}


def test_interactive_launch_carries_gateway_into_cli_environment(setup, monkeypatch):
    root, make, item, target = setup
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'metered-key')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'process-token')
    directory = root / 'executor-dir'
    directory.mkdir()
    (directory / 'settings.json').write_text('{}')
    with fake_gateway() as url:
        p = {'driver': 'claude', 'transport': 'orca', 'billing': 'subscription', 'family': 'codex',
             'gateway': {'base_url': url, 'token': 'magpie'}, 'model': 'codex/gpt-6.2-astra',
             'executor_dir': str(directory)}
        executor = make('gpt', p)
        started = executor.launch('one', Brief('执行', [], workdir=item['workdir']), item, {}, target)
        wait_for(lambda: (root / 'agents').exists() and (root / 'agents').read_text().endswith('\n'))
    call = json.loads((root / 'agents').read_text())
    assert call['env']['ANTHROPIC_BASE_URL'] == url and call['env']['ANTHROPIC_AUTH_TOKEN'] == 'magpie'
    assert 'ANTHROPIC_API_KEY' not in call['env'] and 'CLAUDE_CONFIG_DIR' not in call['env']
    assert call['args'][call['args'].index('--model') + 1] == 'codex/gpt-6.2-astra'
    executor.stop('one', started)


@pytest.mark.parametrize('gateway,field', [
    ({'base_url': 'http://127.0.0.1:3425', 'tokn': 'magpie'}, 'gateway.tokn'),
    ({'base_url': 'http://127.0.0.1:3425'}, 'gateway.token'),
    ({'token': 'magpie'}, 'gateway.base_url'),
    ({'base_url': '127.0.0.1:3425', 'token': 'magpie'}, 'gateway.base_url'),
    ({'base_url': 'http://127.0.0.1:3425', 'token': ''}, 'gateway.token'),
    ('http://127.0.0.1:3425', 'gateway'),
])
def test_bad_gateway_names_profile_and_field_at_rule_load(tmp_path, gateway, field):
    p = gateway_profile()
    p['gateway'] = gateway
    with pytest.raises(ValueError, match='配置档 gpt 的 ' + field.replace('.', r'\.') + '：'):
        load(tmp_path, {'gpt': p})


def test_gateway_only_for_claude_and_not_with_subscription_login_check(tmp_path):
    with pytest.raises(ValueError, match='配置档 gpt 的 gateway：驱动不支持'):
        load(tmp_path, {'gpt': profile('codex', gateway=dict(GATEWAY), family='codex')})
    with pytest.raises(ValueError, match='配置档 gpt 的 require_subscription_login'):
        load(tmp_path, {'gpt': gateway_profile(require_subscription_login=True)})


# 第二条 DoD：哪一家。

def test_family_with_and_without_gateway(tmp_path):
    # 配了网关：必须写 family，按模型那一家算。
    no_family = gateway_profile()
    del no_family['family']
    with pytest.raises(ValueError, match='配置档 gpt 的 family：配了 gateway 必须写明'):
        load(tmp_path, {'gpt': no_family})
    rules = load(tmp_path, {'gpt': gateway_profile()})
    assert rules.data['executor_profiles']['gpt']['family'] == 'codex'
    # 没配网关：照旧由驱动定；不写也是驱动那一家，写成别家报错。
    rules = load(tmp_path, {'plain': profile('claude')})
    assert rules.data['executor_profiles']['plain']['family'] == 'claude'
    with pytest.raises(ValueError, match='配置档 plain 的 family：由驱动确定为 claude'):
        load(tmp_path, {'plain': profile('claude', family='codex')})


# 第三条 DoD：审查和验收照模型那一家回避。

def test_review_and_acceptance_avoid_codex_after_gpt_writer(tmp_path):
    rules = load_rules(RULE_PATH)
    writer = next(c for c in rules.candidates('执行', 'workshop') if c['use'] == 'orca-claude-gpt')
    assert writer['family'] == 'codex'
    for stage in ('审查', '验收'):
        candidates = rules.candidates(stage, 'workshop', writer['family'])
        assert candidates and all(c['family'] != 'codex' for c in candidates)
        assert 'claude' in [c['family'] for c in candidates]
        target = {'quota_snapshot': {'path': str(tmp_path / 'missing.db'), 'max_age_minutes': 45},
                  'selection': {'writer': writer['family']}}
        choice = QuotaSelector().select(target, stage, 1, [], [{'config': c, 'available': True} for c in candidates])
        assert choice.config['family'] == 'claude'


# 第四条 DoD：网关连不上就不可用，挑选跳过；恢复后照常可用。

def gateway_executor(tmp_path, url):
    p = {'driver': 'claude', 'transport': 'orca', 'billing': 'subscription', 'family': 'codex',
         'gateway': {'base_url': url, 'token': 'magpie'}}
    return adapters.Executor('gpt', state_dir=tmp_path / 'state', profile=p,
                             commands={'claude': '/cli/claude', 'orca': '/cli/orca'})


def test_unreachable_gateway_is_unavailable_logged_and_recovers(tmp_path):
    port = free_port()
    url = f'http://127.0.0.1:{port}'
    executor = gateway_executor(tmp_path, url)
    assert executor.available({}) is False
    assert url in (executor.root / 'driver.log').read_text()
    with fake_gateway(port):
        assert executor.available({}) is True
    # 没配网关的配置档不探测。
    plain = adapters.Executor('plain', state_dir=tmp_path / 'state',
                              profile={'driver': 'claude', 'transport': 'orca', 'billing': 'subscription'},
                              commands={'claude': '/cli/claude', 'orca': '/cli/orca'})
    assert plain.available({}) is True and not (plain.root / 'driver.log').exists()


class GatewayBacked(FakeExecutor):
    """可用性检查交给真的 Claude 驱动去探网关；启动等其余照替身。"""
    def __init__(self, name, real):
        super().__init__(name)
        self.real = real

    def available(self, config):
        return self.real.available({})


def test_selection_skips_unreachable_gateway_and_blocks_when_it_is_the_only_one(tmp_path):
    port = free_port()
    real = gateway_executor(tmp_path, f'http://127.0.0.1:{port}')
    config = configuration()
    config['executors']['writer'] = [{'use': 'writer', 'family': 'codex', 'billing': 'subscription'},
                                     {'use': 'acceptor', 'family': 'c', 'billing': 'subscription'}]
    engine = build(tmp_path / 'one', config)
    try:
        engine.plugins.register('executor', 'writer', GatewayBacked('writer', real))
        engine.ingest(event())
        engine.tick()
        rows = engine.active()
        assert [r['executor'] for r in rows] == ['acceptor']
    finally:
        engine.log.close()
    config['executors']['writer'] = [{'use': 'writer', 'family': 'codex', 'billing': 'subscription'}]
    engine = build(tmp_path / 'two', config)
    try:
        engine.plugins.register('executor', 'writer', GatewayBacked('writer', real))
        engine.ingest(event())
        engine.tick()
        assert not engine.active()
        state = engine.log.get('items', 'alpha:one')
        assert state['state'] == '卡住' and state['blocked_by'].startswith('执行者不可用')
    finally:
        engine.log.close()
    # 网关恢复后，下一次挑选照常选到它（在可用性检查里判，没有被记成最近不可用）。
    engine = build(tmp_path / 'three', {**config, 'executors': {
        **config['executors'], 'writer': [{'use': 'writer', 'family': 'codex', 'billing': 'subscription'},
                                          {'use': 'acceptor', 'family': 'c', 'billing': 'subscription'}]}})
    try:
        engine.plugins.register('executor', 'writer', GatewayBacked('writer', real))
        engine.ingest(event())
        with fake_gateway(port):
            engine.tick()
        assert [r['executor'] for r in engine.active()] == ['writer']
    finally:
        engine.log.close()


# 第五条 DoD：用量去掉来源前缀，花费按车间价格表；没价格记未知。

def transcript(model):
    return [{'type': 'assistant', 'timestamp': SINCE, 'message': {'id': 'm', 'model': model, 'usage': {
        'input_tokens': 1000, 'cache_read_input_tokens': 2000, 'cache_creation_input_tokens': 0,
        'output_tokens': 300, 'total_cost_usd': 99}}}]


def test_gateway_model_prefix_is_stripped_and_priced_by_workshop_table():
    [entry] = claude_entries(transcript('codex/gpt-6.2-astra'), SINCE, 'subscription')
    assert entry.model_actual == 'gpt-6.2-astra'
    assert entry.cost_usd == pytest.approx((1000 * 8 + 2000 * .4 + 300 * 40) / 1e6)
    [entry] = claude_entries(transcript('codex/gpt-9-unpriced'), SINCE, 'subscription')
    assert entry.model_actual == 'gpt-9-unpriced'
    assert entry.cost_usd is None
    assert (entry.input, entry.cache_read, entry.output) == (1000, 2000, 300)


# 第七条 DoD：规则里的配置档和执行者目录。

def test_rule_file_has_gpt_profile_last_among_writers_with_matching_settings():
    rules = load_rules(RULE_PATH)
    assert [c['use'] for c in rules.candidates('执行', 'workshop')][-1] == 'orca-claude-gpt'
    p = rules.data['executor_profiles']['orca-claude-gpt']
    repo = RULE_PATH.parents[1]
    assert p['gateway'] == GATEWAY and p['family'] == 'codex' and p['model'] == 'codex/gpt-6.2-astra'
    assert p['quota'] == rules.data['executor_profiles']['orca-codex']['quota']
    assert p['executor_dir'] == str(repo / 'rules/executors/orca-claude-gpt')
    assert (repo / 'rules/executors/orca-claude-gpt/rules.md').read_text() == ''
    gpt = json.loads((repo / 'rules/executors/orca-claude-gpt/settings.json').read_text())
    claude = json.loads((repo / 'rules/executors/orca-claude/settings.json').read_text())
    assert gpt['permissions'] == claude['permissions'] and gpt['hooks'] == claude['hooks']
    assert gpt['env'] == claude['env']


# 第八条 DoD：候选从 2 个变 3 个，在途的活不换人。

def test_items_with_execution_rounds_keep_their_executor_when_candidates_grow():
    two, three = ['orca-codex', 'orca-claude'], ['orca-codex', 'orca-claude', 'orca-claude-gpt']
    configs = [{'use': n, 'family': n, 'billing': 'subscription'} for n in three]

    def choose(work, rule, history=()):
        target = {'selection': {'item': work, 'rule_candidates': rule, 'excluded': [], 'retry_executor': None}}
        return ExperimentSelector().select(target, '执行', len(history) + 1, list(history),
                                           [{'config': c, 'available': True} for c in configs if c['use'] in rule])
    # 找一件按 3 个候选会换人的活。
    work = next(f'w:{n}' for n in range(1000) if two[bucket(f'w:{n}', 2)] != three[bucket(f'w:{n}', 3)])
    before = choose(work, two).config['use']
    history = [{'item': work, 'stage': '执行', 'executor': before, 'outcome': 'done'}]
    after = choose(work, three, history)
    assert after.config['use'] == before
    assert f'沿用本阶段已开过轮的 {before}' in after.reason
    # 启动时报不可用的轮不算开过轮；没开过执行轮的活按新名单分。
    tried = [{'item': work, 'stage': '执行', 'executor': before, 'outcome': 'unavailable'}]
    assert choose(work, three, tried).config['use'] == three[bucket(work, 3)]
    assert choose(work, three).config['use'] == three[bucket(work, 3)]
    # 别的阶段的轮不算。
    other = [{'item': work, 'stage': '说清楚', 'executor': before, 'outcome': 'done'}]
    assert choose(work, three, other).config['use'] == three[bucket(work, 3)]
    # 原执行者这一轮不可用：照现有规则改派，理由写明。
    target = {'selection': {'item': work, 'rule_candidates': three, 'excluded': [before], 'retry_executor': None}}
    rest = [{'config': c, 'available': True} for c in configs if c['use'] != before]
    moved = ExperimentSelector().select(target, '执行', 2, history, rest)
    assert moved.config['use'] != before
    assert f'本该分给 {before}（本阶段已开过轮），它最近启动时报不可用，改分给 {moved.config["use"]}' in moved.reason
