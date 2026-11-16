"""Issue 62 acceptance: real runner processes use fake CLIs, never subscriptions."""
import importlib
import json
from pathlib import Path
import sys

import pytest
import yaml

from workshop.core.models import Brief
from workshop.core.rules import Rules
from workshop.plugins.executors.local import adapters
from workshop.plugins.executors.local.isolation import session_environment
from workshop.plugins.executors.local.storage import read, write
from workshop.registry import Registry, load_rules
from test_executors import setup, wait_for, BRIEF, FAKE
from conftest import build, configuration, current, event, finish, FakeExecutor

RULE_PATH = Path(__file__).parents[1] / 'rules/workshop.yaml'


def profile(driver='claude', **extra):
    return dict(driver=driver, transport='headless', billing='subscription', **extra)


def config_with(profiles):
    result = configuration()
    result['executor_profiles'] = profiles
    result['executors'] = {role: [{'use': name} for name in profiles] for role in ('writer', 'reviewer', 'acceptor')}
    return result


@pytest.mark.parametrize('name', ['orca-claude', 'orca-codex', 'claude', 'codex', 'cursor-grok'])
@pytest.mark.parametrize('resume', [False, True])
def test_migration_preserves_exact_default_argv(tmp_path, name, resume):
    baseline = json.loads((Path(__file__).parent / 'fixtures/executors/argv-before-profiles.json').read_text())
    rules = load_rules(RULE_PATH)
    p = rules.data['executor_profiles'][name]
    e = adapters.Executor(name, profile=p, commands={k: '/cli/' + k for k in ('orca', 'claude', 'codex', 'cursor-agent', 'git')})
    argv = e._argv(p, '/repo', '01234567-89ab-4cde-8fab-0123456789ab', resume, tmp_path, not e.interactive)
    normalized = [a.replace(str(tmp_path), '<round>').replace(str(Path(adapters.__file__).with_name('hook.py')), '<hook>').replace(sys.executable, '<python>') for a in argv]
    assert normalized == baseline['argv'][name]['resume' if resume else 'launch']


def test_migration_preserves_candidate_order_accounting_and_quota():
    rules = load_rules(RULE_PATH)
    assert [c['use'] for c in rules.candidates('执行')] == ['orca-codex', 'orca-claude']
    writer = rules.candidates('执行')[0]
    assert (writer['model'], writer['effort'], writer['billing'], writer['quota']['min_remaining']) == ('gpt-6-astra', 'high', 'subscription', 15)
    assert [c['use'] for c in rules.candidates('审查', writer='codex')] == ['cursor-grok', 'claude']
    assert [c['use'] for c in rules.candidates('验收', writer='codex')] == ['claude']
    assert rules.candidates('验收')[0]['model'] == 'gpt-6.1-sol'


@pytest.mark.parametrize('driver,field,value', [('cursor', 'mcp_config', {}), ('claude', 'modle', 'typo'), ('codex', 'sandboxx', 'read-only'), ('claude', 'quota', {'provider': 'p', 'window':'w', 'min_remainng': 10})])
def test_rule_load_rejects_field_before_any_session(tmp_path, driver, field, value):
    name = 'invalid-profile'
    config = config_with({name: profile(driver, **{field: value})})
    path = tmp_path / 'rules.yaml'
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match=name + '.*' + field):
        load_rules(path)
    assert not (tmp_path / 'rounds').exists()


def test_same_cli_cannot_claim_different_family(tmp_path):
    p = tmp_path/'rules.yaml'
    p.write_text(yaml.safe_dump(config_with({'a': profile(family='a'), 'b': profile(family='b')})))
    with pytest.raises(ValueError, match='配置档 a 的 family'):
        load_rules(p)


@pytest.mark.parametrize('driver', ['claude', 'codex'])
def test_four_configuration_categories_reach_argv_and_environment(setup, driver):
    root, make, item, target = setup
    skill = root / 'SKILL.md'
    skill.write_text('Use the profile-specific checklist.')
    native = root / ('mcp.json' if driver == 'claude' else 'mcp.toml')
    native.write_text('{"mcpServers":{"docs":{"command":"/profile/docs"}}}' if driver == 'claude'
                      else '[mcp_servers.docs]\ncommand="/profile/docs"\n')
    account = root/'account'
    p = profile(driver, model='chosen-model', effort='high', prompt='Investigate with evidence.', skills=[str(skill)],
                mcp_config=str(native), config_dir=str(account))
    if driver == 'claude':
        p.update(tools=['Read', 'Grep'], permission_mode='plan', plugins=[str(root/'plugin')], settings={'language': 'Chinese'})
    else:
        p.update(tools={'shell': False}, sandbox='read-only', permission_mode='never',
                 plugins={'review@local': {'enabled': True}}, settings={'model_verbosity': 'low'})
    executor = make('custom-' + driver, p)
    started = executor.launch('one', Brief('审查', [], workdir=item['workdir'], readonly=True), item, {}, target)
    wait_for(lambda: (root/'agents').exists() and (root/'agents').read_text().endswith('\n'))
    spec = read(executor.directory('one')/'spec.json')
    args = ' '.join(spec['argv'])
    assert 'chosen-model' in args and 'high' in args
    assert 'Investigate with evidence.' in args and 'profile-specific checklist' in args
    call = json.loads((root/'agents').read_text())
    assert call['env']['CLAUDE_CONFIG_DIR' if driver == 'claude' else 'CODEX_HOME'] == str(account)
    assert 'GH_TOKEN' not in call['env'] and 'SSH_AUTH_SOCK' not in call['env']
    if driver == 'claude':
        for expected in (str(native), 'Read,Grep', 'plan', str(root/'plugin'), 'Chinese'):
            assert expected in args
    else:
        for expected in ('mcp_servers', '/profile/docs', 'shell_tool', 'false', 'review@local', 'enabled', 'true', 'model_verbosity', 'low'):
            assert expected in args
    executor.stop('one', started)


def test_two_claude_profiles_start_distinct_sessions_and_configs(setup):
    root, make, item, target = setup
    sessions = []
    for index, name in enumerate(('code', 'diagnose')):
        mcp = root / (name + '.json')
        mcp.write_text('{"mcpServers":{}}')
        p = profile(prompt=name, tools=['Read'] if index == 0 else ['Read', 'Grep'], mcp_config=str(mcp), config_dir=str(root/name))
        executor = make(name, p)
        token = str(index)
        started = executor.launch(token, BRIEF, item, {}, target)
        sessions.append(started.session)
        wait_for(lambda: (root/'agents').exists() and len((root/'agents').read_text().splitlines()) == index + 1)
        spec = read(executor.directory(token)/'spec.json')
        assert spec['vendor_id'] == started.session
        assert '--resume' not in spec['argv'] and name in spec['argv']
        assert spec['env']['CLAUDE_CONFIG_DIR'] == str(root/name)
        executor.stop(token, started)
    assert sessions[0] != sessions[1]


def test_profile_switch_unavailability_and_retry_are_independent(tmp_path):
    config = config_with({'a': profile(), 'b': profile(), 'review': profile('cursor')})
    config['executors']['writer'] = [{'use': 'a'}, {'use': 'b'}]
    config['stage_executors'] = {'说清楚': [{'use': 'b'}, {'use': 'a'}]}
    engine = build(tmp_path, config)
    try:
        for name in ('a', 'b', 'review'):
            engine.plugins.register('executor', name, FakeExecutor(name))
        engine.ingest(event())
        engine.tick()
        assert current(engine)['executor'] == 'a'
        finish(engine, '说清楚')
        assert current(engine)['executor'] == 'b'
        assert engine.plugins.get('executor', 'b').calls[0][0] == 'launch'
        assert engine.rules.candidates('验收', writer='claude')[0]['use'] == 'review'
        # Return to a stage with A first; only A unavailable, so B remains a candidate.
        engine.submit(current(engine)['token'], {'outcome': 'done', 'summary': 'clear', 'advance': '执行'})
        engine.plugins.get('executor', 'a').enabled = False
        engine.tick()
        assert current(engine)['executor'] == 'b'
        # Failure pins the retry to the profile name, not its shared CLI/family.
        engine.plugins.get('executor', 'a').enabled = True
        engine.plugins.get('flow', 'flow').passed = False
        engine.submit(current(engine)['token'], {'outcome': 'done', 'summary': 'needs evidence', 'advance': '证据'})
        engine.tick()
        assert engine.log.get('items', 'alpha:one')['retry_executor'] == 'b'
    finally:
        engine.log.close()


def test_old_orca_claude_session_resumes_without_fingerprint(setup):
    root, make, item, target = setup
    executor = make('orca-claude')
    first = executor.launch('one', BRIEF, item, {}, target)
    wait_for(lambda: (root/'agents').exists())
    executor.stop('one', first)
    spec_path = executor.directory('one')/'spec.json'
    spec = read(spec_path)
    spec.pop('profile_fingerprint')
    spec.pop('driver')
    write(spec_path, spec)
    second = make('orca-claude').resume('two', first.session, BRIEF, item, {}, target)
    wait_for(lambda: len((root/'agents').read_text().splitlines()) == 2)
    args = json.loads((root/'agents').read_text().splitlines()[1])['args']
    assert args[args.index('--resume') + 1] == first.session
    executor.stop('two', second)


def test_credentials_cannot_be_restored_by_profile_or_runtime_environment(tmp_path):
    spec = {'readonly': True, 'billing':'subscription', 'gh_config': str(tmp_path/'empty-gh'),
            'env': {'GH_TOKEN': 'injected', 'SSH_AUTH_SOCK': '/injected', 'AWS_SECRET_ACCESS_KEY':'injected',
                    'GH_CONFIG_DIR':'/injected', 'GIT_CONFIG_VALUE_0':'unsafe', 'WORKSHOP_ROUND_TOKEN':'round'}}
    env = session_environment(spec, {'HOME':'/home', 'GITHUB_TOKEN':'ambient', 'AZURE_CLIENT_SECRET':'ambient', 'GOOGLE_APPLICATION_CREDENTIALS':'ambient'})
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'AWS_SECRET_ACCESS_KEY', 'AZURE_CLIENT_SECRET', 'GOOGLE_APPLICATION_CREDENTIALS'):
        assert key not in env
    assert env['GH_CONFIG_DIR'] == str(tmp_path/'empty-gh') and env['GIT_CONFIG_VALUE_0'] == ''
    assert env['WORKSHOP_ROUND_TOKEN'] == 'round'


def test_cursor_effort_and_permission_mode_are_explicit(setup):
    root, make, item, target = setup
    e = make('cursor-test', profile('cursor', model='grok', effort='high', permission_mode='ask'))
    started = e.launch('one', BRIEF, item, {}, target)
    wait_for(lambda: (root/'agents').exists())
    argv = read(e.directory('one')/'spec.json')['argv']
    assert argv[argv.index('--model') + 1] == 'grok[effort=high]'
    assert argv[argv.index('--mode') + 1] == 'ask'
    e.stop('one', started)


def test_new_cli_needs_only_driver_file_and_entry_point(setup, monkeypatch):
    root, make, item, target = setup
    common = Path(adapters.__file__).parent
    unchanged = {name: (common/name).read_bytes() for name in ('adapters.py', 'runner.py', 'usage.py', 'hook.py', '__init__.py')}
    # An installed wheel's registration is just this entry_points.txt metadata.
    extension = root / 'extension'
    extension.mkdir()
    (extension/'sample_driver.py').write_text('''from workshop.plugins.executors.local.drivers.base import Driver as Base
class Driver(Base):
    family = "sample"
    binary = "sample-cli"
    def argv(self, executor, config, workdir, session, resume, directory, trust_workdir=None):
        return [executor.commands[self.binary], "--example", config.get("model", "sample")]
''')
    metadata = extension/'sample_driver-1.0.dist-info'
    metadata.mkdir()
    (metadata/'METADATA').write_text('Metadata-Version: 2.1\nName: sample-driver\nVersion: 1.0\n')
    (metadata/'entry_points.txt').write_text('[workshop.driver]\nsample = sample_driver:Driver\n')
    monkeypatch.syspath_prepend(str(extension))
    monkeypatch.setenv('PATH', str(root) + ':' + __import__('os').environ['PATH'])
    monkeypatch.setenv('PYTHONPATH', str(extension))
    monkeypatch.setenv('WORKSHOP_EXECUTOR_STATE_DIR', str(root/'custom-state'))
    importlib.invalidate_caches()
    cli = root/'sample-cli'
    cli.write_text('#!' + sys.executable + '\n' + FAKE)
    cli.chmod(0o755)
    (root/'finish').touch()
    (root/'success').touch()
    path = root/'sample-rules.yaml'
    path.write_text(yaml.safe_dump(config_with({'sample-profile': profile('sample', model='custom-model')})))
    registry = Registry()
    rules = registry.configure(load_rules(path))
    executor = registry.get('executor', 'sample-profile')
    started = executor.launch('sample-token', BRIEF, item, rules.candidates('执行')[0], target)
    wait_for(lambda: (executor.directory('sample-token')/'exited.json').exists())
    result = read(executor.directory('sample-token')/'receipt-result.json')
    assert result['code'] == 0
    assert read(executor.directory('sample-token')/'fallback-receipt.json')['outcome'] == 'done'
    call = json.loads((root/'agents').read_text())
    assert call['name'] == 'sample-cli' and call['args'] == ['--example', 'custom-model']
    executor.stop('sample-token', started)
    assert all((common/name).read_bytes() == content for name, content in unchanged.items())


@pytest.mark.parametrize('transport', ['headless', 'orca'])
@pytest.mark.parametrize('resume', [False, True])
def test_codex_overrides_use_cli_paths_not_quoted_toml_keys(tmp_path, transport, resume):
    import tomllib
    p = profile('codex', model='test', effort='high',
                sandbox='workspace-write', permission_mode='never',
                prompt='只读\n带 "引号"', settings={'sandbox_workspace_write': {'network_access': True}},
                mcp_config={'mcp_servers': {'docs.test': {'command': '/read/docs'}}},
                plugins={'review@local': {'enabled': True}})
    p['transport'] = transport
    executor = adapters.Executor('probe', profile=p)
    argv = executor._argv(p, '/repo', 'session', resume, tmp_path, False)
    settings = [argv[i + 1] for i, v in enumerate(argv[:-1]) if v == '-c']
    # codex 的路径解析按点拆开，值再按 TOML 读；键上的引号不会被剥掉。
    effective = {}
    for setting in settings:
        key, value = setting.split('=', 1)
        container = effective
        parts = key.split('.')
        for part in parts[:-1]:
            container = container.setdefault(part, {})
        container[parts[-1]] = tomllib.loads('value=' + value)['value']
    assert effective['developer_instructions'] == p['prompt']
    assert effective['sandbox_workspace_write']['network_access'] is True
    assert effective['mcp_servers']['docs.test']['command'] == '/read/docs'
    assert effective['plugins']['review@local']['enabled'] is True
    if transport == 'orca':
        assert effective['hooks']['SessionStart'][0]['hooks'][0]['command']
        assert effective['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
