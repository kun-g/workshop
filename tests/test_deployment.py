"""#293 部署目录：叠在仓库规则上的执行者定义、角色名单和 Orca 开关；校验后重启生效。"""
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path

import pytest
import yaml

from workshop import cli, executors_command, manager_cli
from workshop.core.log import Log
from workshop.core.rules import Rules
from workshop.deployment import DEPLOYMENT, REPOSITORY, assemble, directory
from workshop.plugins.executors.local import adapters
from workshop.plugins.executors.local.storage import read
from workshop.registry import Registry, load_rules
from workshop.reports.report import usage
from conftest import configuration
from test_executors import setup, wait_for, BRIEF  # noqa: F401  (pytest fixture)
from test_profile_usage import Book, DAY, row

RULES = Path(__file__).parents[1] / 'rules/workshop.yaml'
SESSION = '01234567-89ab-4cde-8fab-0123456789ab'
# 真实路径一里的 claude-review：从仓库的 orca-claude 复制，执行环境不写。
REVIEW = {k: v for k, v in yaml.safe_load(RULES.read_text())['executor_profiles']['orca-claude'].items()
          if k not in ('transport', 'executor_dir')}


def deployment(tmp_path, executors=None, settings=None, name='deploy'):
    """executors：名字 → (executor.yaml 内容或对象, {文件名: 内容})。settings 为 None 时不写 deployment.yaml。"""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for executor, (definition, files) in (executors or {}).items():
        path = root / 'executors' / executor
        path.mkdir(parents=True)
        text = definition if isinstance(definition, str) else yaml.safe_dump(definition, allow_unicode=True)
        (path / 'executor.yaml').write_text(text)
        for file, content in files.items():
            (path / file).write_text(content)
    if settings is not None:
        (root / 'deployment.yaml').write_text(yaml.safe_dump(settings, allow_unicode=True))
    return root


def small_repository(tmp_path, profiles):
    """不经 Orca 也能完整配置驱动的小规则；writer 用全部配置档，审查、验收用 grok。"""
    root = tmp_path / 'checkout'
    (root / '.git').mkdir(parents=True)
    config = configuration()
    config['executor_profiles'] = {**profiles, 'grok': {'driver': 'cursor', 'transport': 'headless', 'billing': 'subscription'}}
    config['executors'] = {'writer': [{'use': n} for n in profiles], 'reviewer': [{'use': 'grok'}], 'acceptor': [{'use': 'grok'}]}
    path = root / 'rules/workshop.yaml'
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(config, allow_unicode=True))
    return path


# ---------- 叠加：新增、同名替换、只替换一个角色、没有 deployment.yaml ----------

def test_new_executor_same_name_replacement_and_one_role_swap(tmp_path):
    root = deployment(tmp_path, {
        'claude-review': (REVIEW, {}),
        # 同名整个替换：仓库 orca-claude 的 executor_dir、permission_mode 都不带过来。
        'orca-claude': ({'driver': 'claude', 'billing': 'subscription', 'model': 'claude-sonnet-5-5'}, {}),
    }, {'roles': {'writer': ['claude-review', 'orca-claude']}})
    repository = assemble(RULES, 'none').rules.data
    result = assemble(RULES, root)
    data = result.rules.data
    assert data['executor_profiles']['claude-review']['model'] == 'claude-opus-5-5'
    assert {k: v for k, v in data['executor_profiles']['orca-claude'].items() if k != 'revision'} == {
        'driver': 'claude', 'billing': 'subscription', 'model': 'claude-sonnet-5-5', 'transport': 'orca'}
    assert data['executors']['writer'] == [{'use': 'claude-review'}, {'use': 'orca-claude'}]
    # 没写的角色沿用仓库的；项目经理一节不动。
    assert data['executors']['reviewer'] == repository['executors']['reviewer']
    assert data['executors']['acceptor'] == repository['executors']['acceptor']
    assert data['managers'] == repository['managers']
    assert result.origins['claude-review'] == result.origins['orca-claude'] == DEPLOYMENT
    assert result.origins['orca-codex'] == REPOSITORY
    assert result.unisolated == ('orca-claude',)
    assert result.rules.path == str(RULES.resolve())
    # 核心照常按新名单给候选。
    assert [c['use'] for c in result.rules.candidates('执行')] == ['claude-review', 'orca-claude']


def test_managers_role_replaces_only_the_candidate_list(tmp_path):
    root = deployment(tmp_path, {'manager-extra': ({'driver': 'claude', 'billing': 'subscription'}, {})},
                      {'roles': {'managers': ['manager-extra', 'manager-claude']}})
    repository = assemble(RULES, 'none').rules.data['managers']
    managers = assemble(RULES, root).rules.data['managers']
    assert managers == {**repository, 'candidates': ['manager-extra', 'manager-claude']}


def test_without_deployment_yaml_roles_stay_as_the_repository_wrote_them(tmp_path):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    repository = assemble(RULES, 'none').rules.data
    data = assemble(RULES, root).rules.data
    assert 'claude-review' in data['executor_profiles']
    assert data['executors'] == repository['executors']
    assert {k: v for k, v in data.items() if k != 'executor_profiles'} == {
        k: v for k, v in repository.items() if k != 'executor_profiles'}


def test_subdirectory_without_executor_yaml_is_not_an_executor(tmp_path):
    root = deployment(tmp_path)
    (root / 'executors/notes').mkdir(parents=True)
    (root / 'executors/notes/rules.md').write_text('草稿')
    assert assemble(RULES, root).rules.data == assemble(RULES, 'none').rules.data


# ---------- 执行环境：不写按 Orca；部署关掉 Orca 时全部后台 ----------

def test_transport_defaults_to_orca_in_repository_and_deployment(tmp_path):
    rules = small_repository(tmp_path, {'plain': {'driver': 'claude', 'billing': 'subscription'},
                                        'quiet': {'driver': 'claude', 'transport': 'headless', 'billing': 'subscription'}})
    root = deployment(tmp_path, {'added': ({'driver': 'claude', 'billing': 'subscription'}, {}),
                                 'added-quiet': ({'driver': 'claude', 'transport': 'headless', 'billing': 'subscription'}, {})})
    profiles = load_rules(rules, root).data['executor_profiles']
    assert profiles['plain']['transport'] == 'orca'
    assert profiles['added']['transport'] == 'orca'
    # 明确写了的不变。
    assert profiles['quiet']['transport'] == 'headless'
    assert profiles['added-quiet']['transport'] == 'headless'
    # 没有部署目录时也一样按 Orca。
    assert load_rules(rules, 'none').data['executor_profiles']['plain']['transport'] == 'orca'


def test_orca_switch_off_moves_every_orca_profile_to_headless(tmp_path):
    rules = small_repository(tmp_path, {'plain': {'driver': 'claude', 'billing': 'subscription'},
                                        'explicit': {'driver': 'claude', 'transport': 'orca', 'billing': 'subscription'},
                                        'quiet': {'driver': 'claude', 'transport': 'headless', 'billing': 'subscription'}})
    root = deployment(tmp_path, {'added': ({'driver': 'claude', 'billing': 'subscription'}, {}),
                                 'added-orca': ({'driver': 'claude', 'transport': 'orca', 'billing': 'subscription'}, {})},
                      {'orca': False})
    profiles = load_rules(rules, root).data['executor_profiles']
    assert {name: p['transport'] for name, p in profiles.items()} == {
        'plain': 'headless', 'explicit': 'headless', 'quiet': 'headless', 'grok': 'headless',
        'added': 'headless', 'added-orca': 'headless'}
    (root / 'deployment.yaml').write_text('orca: true\n')
    assert load_rules(rules, root).data['executor_profiles']['explicit']['transport'] == 'orca'


def test_orca_switch_off_launches_without_orca(setup, tmp_path):
    root, make, item, target = setup
    deploy = deployment(tmp_path, {'claude-review': (REVIEW, {'rules.md': '只用中文回答。\n'})}, {'orca': False})
    profile = load_rules(RULES, deploy).data['executor_profiles']['claude-review']
    assert profile['transport'] == 'headless'
    executor = make('claude-review', profile)
    assert not executor.interactive
    started = executor.launch('one', BRIEF, item, {}, target)
    wait_for(lambda: (root / 'agents').exists() and (root / 'agents').read_text().endswith('\n'))
    spec = read(executor.directory('one') / 'spec.json')
    assert spec['interactive'] is False
    assert not any('orca' in Path(part).name for part in spec['argv'])
    assert (executor.directory('one') / 'supervisor.json').exists()
    assert not (root / 'orca-calls').exists()  # 一次 Orca 都没调
    call = json.loads((root / 'agents').read_text().splitlines()[0])
    assert call['name'] == 'claude' and '-p' in call['args']
    assert '只用中文回答。' in call['args'][call['args'].index('--append-system-prompt') + 1]
    executor.stop('one', started)


# ---------- 部署目录不存在时和现在完全一样 ----------

def test_without_deployment_directory_the_load_equals_the_core_load(tmp_path, monkeypatch):
    core = Rules.load(RULES)
    for value in ('none', '', str(tmp_path / 'missing')):
        assembled = assemble(RULES, value).rules
        assert assembled.data == core.data and assembled.path == core.path
    monkeypatch.setenv('WORKSHOP_DEPLOYMENT', str(tmp_path / 'missing'))
    assert assemble(RULES).rules.data == core.data
    assert load_rules(RULES).data == Registry().configure(core).data


def test_test_session_never_reads_the_machines_deployment_directory():
    # conftest 的夹具对所有测试生效：不读 ~/.config/workshop。
    assert os.environ['WORKSHOP_DEPLOYMENT'] == 'none'
    assert directory() is None


def test_default_directory_and_environment_override(tmp_path, monkeypatch):
    monkeypatch.delenv('WORKSHOP_DEPLOYMENT')
    assert directory() == Path('~/.config/workshop').expanduser().resolve()
    monkeypatch.setenv('WORKSHOP_DEPLOYMENT', str(tmp_path))
    assert directory() == tmp_path.resolve()


# ---------- 入口：项目经理入口也看到部署目录里的执行者 ----------

def test_manager_entry_sees_deployment_executors(tmp_path, monkeypatch):
    root = deployment(tmp_path, {'manager-extra': ({'driver': 'claude', 'transport': 'headless',
                                                    'billing': 'subscription'}, {})},
                      {'roles': {'managers': ['manager-extra']}})
    monkeypatch.setenv('WORKSHOP_DEPLOYMENT', str(root))
    seen = []

    class Stop(Exception):
        pass

    class FakeRegistry:
        def configure(self, rules):
            seen.append(rules)
            raise Stop

    monkeypatch.setattr(manager_cli, 'Registry', FakeRegistry)
    message = tmp_path / 'input.json'
    message.write_text('{}')
    with pytest.raises(Stop):
        manager_cli.main(['--state-dir', str(tmp_path / 'state'), '--rules', str(RULES), 'tick', '--file', str(message)])
    assert seen[0].data['executor_profiles']['manager-extra']['billing'] == 'subscription'
    assert seen[0].data['managers']['candidates'] == ['manager-extra']


def test_daemon_entry_sees_deployment_executors(tmp_path, monkeypatch):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})}, {'roles': {'writer': ['claude-review']}})
    monkeypatch.setenv('WORKSHOP_DEPLOYMENT', str(root))
    args = cli.parser().parse_args(['--rules', str(RULES), 'check-issue', '--file', 'x'])
    rules = cli.current_rules(args, None)
    assert [c['use'] for c in rules.candidates('执行')] == ['claude-review']


# ---------- 隔离 ----------

@pytest.mark.parametrize('files', [{'rules.md': '只用中文写提交说明。\n'}, {'settings.json': '{"env": {"MARK": "1"}}'},
                                   {'rules.md': '规矩', 'settings.json': '{}'}])
def test_settings_or_rules_file_isolates_from_its_own_directory(tmp_path, files):
    root = deployment(tmp_path, {'claude-review': (REVIEW, files)})
    profile = load_rules(RULES, root).data['executor_profiles']['claude-review']
    assert profile['executor_dir'] == str((root / 'executors/claude-review').resolve())
    executor = adapters.Executor('claude-review', profile=profile, commands={'claude': '/cli/claude', 'orca': '/cli/orca'})
    workdir = tmp_path / 'work'
    argv = executor._argv(profile, str(workdir), SESSION, False, tmp_path, False, str(workdir))
    assert argv[argv.index('--setting-sources') + 1] == 'project'
    if 'rules.md' in files:
        assert files['rules.md'].strip() in argv[argv.index('--append-system-prompt') + 1]
    if 'MARK' in files.get('settings.json', ''):
        assert json.loads((tmp_path / 'hooks.json').read_text())['env'] == {'MARK': '1'}


def test_no_settings_or_rules_file_means_no_isolation(tmp_path):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    profile = load_rules(RULES, root).data['executor_profiles']['claude-review']
    assert 'executor_dir' not in profile
    executor = adapters.Executor('claude-review', profile=profile, commands={'claude': '/cli/claude', 'orca': '/cli/orca'})
    argv = executor._argv(profile, str(tmp_path), SESSION, False, tmp_path, False)
    assert '--setting-sources' not in argv


@pytest.mark.parametrize('definition,files,message', [
    ({**REVIEW, 'executor_dir': '/elsewhere'}, {}, '不许写 executor_dir'),
    ({**REVIEW, 'config_dir': '~/other-login'}, {'rules.md': '规矩'}, '不许再写 config_dir'),
    ({'driver': 'codex', 'billing': 'subscription', 'model': 'gpt-6-astra', 'effort': 'high'}, {'rules.md': '规矩'},
     'codex 壳不支持隔离'),
    ({'driver': 'cursor', 'billing': 'subscription'}, {'settings.json': '{}'}, 'cursor 壳不支持隔离'),
])
def test_isolation_rules_are_enforced_at_load(tmp_path, definition, files, message):
    root = deployment(tmp_path, {'claude-review': (definition, files)})
    with pytest.raises(ValueError, match=message) as caught:
        assemble(RULES, root)
    assert 'claude-review' in str(caught.value) and 'executor.yaml' in str(caught.value)


def test_config_dir_without_isolation_files_is_allowed(tmp_path):
    root = deployment(tmp_path, {'claude-review': ({**REVIEW, 'config_dir': '~/other-login'}, {})})
    profile = assemble(RULES, root).rules.data['executor_profiles']['claude-review']
    assert profile['config_dir'] == '~/other-login' and 'executor_dir' not in profile


# ---------- 错误信息里有执行者名和文件名 ----------

@pytest.mark.parametrize('executors,settings,file', [
    ({'claude-review': ('driver: claude\nmodel: [unclosed\n', {})}, None, 'executor.yaml'),
    ({'claude-review': ('- just\n- a list\n', {})}, None, 'executor.yaml'),
    ({'claude-review': ({**REVIEW, 'modle': 'typo'}, {})}, None, 'executor.yaml'),
    ({'claude-review': ({**REVIEW, 'effort': ''}, {})}, None, 'executor.yaml'),
    ({'claude-review': ({k: v for k, v in REVIEW.items() if k != 'driver'}, {})}, None, 'executor.yaml'),
    ({}, {'roles': {'writer': ['orca-codex', 'claude-review']}}, 'deployment.yaml'),
    ({'claude-review': ({**REVIEW, 'billing': 'metered'}, {})}, {'roles': {'managers': ['claude-review']}}, 'executor.yaml'),
])
def test_load_errors_name_the_executor_and_the_file(tmp_path, executors, settings, file):
    root = deployment(tmp_path, executors, settings)
    with pytest.raises(ValueError) as caught:
        assemble(RULES, root)
    assert 'claude-review' in str(caught.value) and file in str(caught.value), caught.value


def test_repository_manager_candidate_turned_metered_names_the_file(tmp_path):
    root = deployment(tmp_path, {'manager-claude': ({'driver': 'claude', 'transport': 'headless', 'billing': 'metered'}, {})})
    with pytest.raises(ValueError, match=r'项目经理候选 manager-claude 不是订阅计费.*manager-claude/executor\.yaml'):
        assemble(RULES, root)


@pytest.mark.parametrize('text,message', [
    ('orca: maybe\n', 'orca 只能是 true 或 false'),
    ('roles: {writer: []}\n', 'roles.writer'),
    ('roles: {builder: [orca-codex]}\n', '不认 builder'),
    ('extra: 1\n', '不认的字段 extra'),
])
def test_bad_deployment_yaml_names_the_file(tmp_path, text, message):
    root = deployment(tmp_path)
    (root / 'deployment.yaml').write_text(text)
    with pytest.raises(ValueError, match=message) as caught:
        assemble(RULES, root)
    assert 'deployment.yaml' in str(caught.value)


# ---------- 内容变了算新的一版 ----------

def test_changing_only_rules_md_is_a_new_version_and_repository_directories_are_not(tmp_path):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {'rules.md': '第一版规矩'})})
    before = load_rules(RULES, root).data['executor_profiles']
    (root / 'executors/claude-review/rules.md').write_text('第二版规矩')
    after = load_rules(RULES, root).data['executor_profiles']
    assert before['claude-review']['revision'] != after['claude-review']['revision']
    assert ({k: v for k, v in before['claude-review'].items() if k != 'revision'}
            == {k: v for k, v in after['claude-review'].items() if k != 'revision'})
    assert adapters.Executor.fingerprint(before['claude-review']) != adapters.Executor.fingerprint(after['claude-review'])
    # 仓库里的执行者目录没有版本：内容变了也不算新的一版。
    assert 'revision' not in before['orca-claude'] and before['orca-claude'] == after['orca-claude']
    # workshop usage「按执行者配置档」分成两行。
    candidates = lambda profiles: {**profiles['claude-review'], 'use': 'claude-review', 'family': 'claude'}
    book = Book(tmp_path)
    book.item('a:1')
    book.run('a:1', 'claude-review', candidates(before), 10)
    book.at += timedelta(days=1)
    book.item('a:2')
    book.run('a:2', 'claude-review', candidates(after), 20)
    text = usage(book.log, DAY + timedelta(days=3), since='2026-11-20')
    first = DAY.astimezone().date().isoformat()
    later = (DAY + timedelta(days=1)).astimezone().date().isoformat()
    assert row(text, f'claude-review 第1版（{first} 起）')[1] == '1'
    assert row(text, f'claude-review 第2版（{later} 起）')[1] == '1'
    book.log.close()


def test_rules_md_change_opens_a_new_session_but_a_started_round_keeps_its_content(setup, tmp_path):
    root, make, item, target = setup
    deploy = deployment(tmp_path, {'claude-review': (REVIEW, {'rules.md': '第一版规矩'})})
    old = load_rules(RULES, deploy).data['executor_profiles']['claude-review']
    first = make('claude-review', old).launch('one', BRIEF, item, {}, target)
    wait_for(lambda: (root / 'agents').exists() and (root / 'agents').read_text().endswith('\n'))
    (deploy / 'executors/claude-review/rules.md').write_text('第二版规矩')
    new = load_rules(RULES, deploy).data['executor_profiles']['claude-review']  # 车间重启后读到新内容
    # 同一轮内进程重启：照启动时的内容，不读新内容、不重起。
    assert make('claude-review', new).launch('one', BRIEF, item, {}, target) == first
    spec = read(make('claude-review', new).directory('one') / 'spec.json')
    prompt = spec['argv'][spec['argv'].index('--append-system-prompt') + 1]
    assert '第一版规矩' in prompt and '第二版规矩' not in prompt
    assert len((root / 'agents').read_text().splitlines()) == 1
    make('claude-review', new).stop('one', first)
    # 之后的轮开新会话、不接回旧会话。
    target['runtime']['env']['WORKSHOP_ROUND_TOKEN'] = 'two'
    second = make('claude-review', new).resume('two', first.session, BRIEF, item, {}, target)
    wait_for(lambda: len((root / 'agents').read_text().splitlines()) == 2)
    spec = read(make('claude-review', new).directory('two') / 'spec.json')
    assert spec['fresh_session'] is True and '--resume' not in spec['argv']
    assert spec['vendor_id'] != first.session
    assert '第二版规矩' in spec['argv'][spec['argv'].index('--append-system-prompt') + 1]
    make('claude-review', new).stop('two', second)


# ---------- workshop executors check ----------

def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def state_with_rounds(tmp_path, executors):
    state = tmp_path / 'state'
    state.mkdir()
    log = Log(state / 'workshop.db')
    for index, (executor, status) in enumerate(executors):
        log.append('event', {'target': 'workshop', 'object_key': str(index), 'title': 't', 'depth': 0, 'action': 'ready',
                             'body': {}, 'ext_id': f'e{index}'}, f'workshop:{index}', ext_id=f'e{index}', at=DAY.isoformat())
        log.append('round_reserved', dict(token=f't{index}', item=f'workshop:{index}', stage='执行', executor=executor,
                                          config={}, status=status, at=DAY.isoformat()), f'workshop:{index}',
                   ext_id=f'r{index}', at=DAY.isoformat())
    log.close()
    return state


def test_check_prints_one_line_per_executor_and_does_not_write_state(tmp_path, capsys):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {'rules.md': '只用中文回答。'})},
                      {'roles': {'writer': ['orca-codex', 'claude-review']}})
    state = state_with_rounds(tmp_path, [('orca-codex', 'running')])
    before = (state / 'workshop.db').read_bytes()
    code, out, err = run_cli(capsys, '--state-dir', str(state), 'executors', 'check', str(root))
    assert code == 0, err
    lines = [line.split('\t') for line in out.splitlines()]
    assert ['claude-review', 'claude', 'claude-opus-5-5', 'orca', 'writer', '部署'] in lines
    assert ['orca-codex', 'codex', 'gpt-6-astra', 'orca', 'writer', '仓库'] in lines
    assert ['codex', 'codex', 'gpt-6.1-sol', 'headless', 'reviewer,acceptor', '仓库'] in lines
    assert ['manager-codex', 'codex', 'gpt-6.1-sol', 'headless', 'managers', '仓库'] in lines
    assert ['watch-inventory', 'codex', 'gpt-6.1-sol', 'headless', '—', '仓库'] in lines
    assert ['cursor-grok', 'cursor', '—', 'headless', 'reviewer', '仓库'] in lines
    assert not any(line.startswith('警告') for line in out.splitlines())
    assert (state / 'workshop.db').read_bytes() == before
    assert not (root / 'executors/claude-review/.workshop').exists()


def test_check_failure_prints_the_error_and_exits_nonzero(tmp_path, capsys):
    root = deployment(tmp_path, {'claude-review': ({**REVIEW, 'modle': 'typo'}, {})})
    code, out, err = run_cli(capsys, '--state-dir', str(tmp_path / 'state'), 'executors', 'check', str(root))
    assert code != 0 and out == ''
    assert 'claude-review' in err and 'executor.yaml' in err
    code, out, err = run_cli(capsys, '--state-dir', str(tmp_path / 'state'), 'executors', 'check', str(tmp_path / 'nowhere'))
    assert code != 0 and '不存在' in err


def test_check_warns_about_running_rounds_of_executors_that_are_gone(tmp_path, capsys):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    state = state_with_rounds(tmp_path, [('claude-old', 'running'), ('claude-older', 'ended'), ('orca-codex', 'running')])
    code, out, err = run_cli(capsys, '--state-dir', str(state), 'executors', 'check', str(root))
    assert code == 0, err
    warnings = [line for line in out.splitlines() if line.startswith('警告')]
    assert len(warnings) == 1 and 'claude-old' in warnings[0] and 'workshop:0' in warnings[0]


def test_check_warns_when_replacement_drops_isolation(tmp_path, capsys):
    root = deployment(tmp_path, {'orca-claude': ({**REVIEW}, {})})
    state = state_with_rounds(tmp_path, [])
    code, out, err = run_cli(capsys, '--state-dir', str(state), 'executors', 'check', str(root))
    assert code == 0, err
    warnings = [line for line in out.splitlines() if line.startswith('警告')]
    assert len(warnings) == 1 and 'orca-claude' in warnings[0] and '不再隔离' in warnings[0]


def test_check_warns_when_state_cannot_be_read_and_skips_round_check(tmp_path, capsys):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    code, out, err = run_cli(capsys, '--state-dir', str(tmp_path / 'no-state'), 'executors', 'check', str(root))
    assert code == 0, err
    warnings = [line for line in out.splitlines() if line.startswith('警告')]
    assert len(warnings) == 1 and '状态库读不了' in warnings[0]


# ---------- workshop executors reload ----------

class Machine:
    """假的 launchd 和时钟：等待推进时钟；重启后第 delay 次查看时心跳换成新进程。"""

    def __init__(self, state, beats):
        self.state, self.beats = state, list(beats)
        self.now = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        self.restarts = 0
        self.pauses = []
        self.beat(100, self.now - timedelta(minutes=1))

    def beat(self, pid, at):
        (self.state / 'heartbeat.json').write_text(json.dumps({'process': {'pid': pid}, 'last_ok_at': at.isoformat()}))

    def clock(self):
        return self.now

    def restart(self):
        self.restarts += 1

    def pause(self, seconds):
        self.pauses.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.beats:
            pid, offset = self.beats.pop(0)
            if pid is not None:
                self.beat(pid, self.now + timedelta(seconds=offset))


def reload(tmp_path, root, machine):
    out, err = io.StringIO(), io.StringIO()
    code = executors_command.reload(RULES, root, machine.state, out, err, restart=machine.restart,
                                    clock=machine.clock, pause=machine.pause, timeout=30, interval=2)
    return code, out.getvalue(), err.getvalue()


def test_reload_stops_before_restart_when_check_fails(tmp_path):
    root = deployment(tmp_path, {'claude-review': ({**REVIEW, 'modle': 'typo'}, {})})
    machine = Machine(state_with_rounds(tmp_path, []), [])
    code, out, err = reload(tmp_path, root, machine)
    assert code != 0 and machine.restarts == 0
    assert 'claude-review' in err and '没有重启' in err


def test_reload_succeeds_once_a_new_process_finishes_a_tick(tmp_path):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    # 先是旧心跳，再是新进程还没跑通（成功时间早于重启），最后跑通一拍。
    machine = Machine(state_with_rounds(tmp_path, []), [(None, 0), (200, -60), (200, 0)])
    code, out, err = reload(tmp_path, root, machine)
    assert code == 0, err
    assert machine.restarts == 1 and len(machine.pauses) == 3
    assert '100 → 200' in out


def test_reload_counts_a_watchdog_restart_during_the_wait_as_success(tmp_path):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    machine = Machine(state_with_rounds(tmp_path, []), [(200, -60), (300, 0)])
    code, out, err = reload(tmp_path, root, machine)
    assert code == 0, err
    assert machine.restarts == 1 and '100 → 300' in out


def test_reload_timeout_says_the_workshop_is_down_and_needs_a_person(tmp_path):
    root = deployment(tmp_path, {'claude-review': (REVIEW, {})})
    machine = Machine(state_with_rounds(tmp_path, []), [])
    code, out, err = reload(tmp_path, root, machine)
    assert code != 0 and machine.restarts == 1
    assert sum(machine.pauses) == 30
    assert '没起来' in err and '没有自动回退' in err and '要人处理' in err
