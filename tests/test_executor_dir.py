"""#283 核心 R25：写代码的执行者只读规则、自己的执行者目录和活所在仓库的 AGENTS.md，不读本机个人配置。"""
import json
from pathlib import Path

import pytest
import yaml

from workshop.core.models import Brief
from workshop.plugins.executors.local import adapters
from workshop.plugins.executors.local.storage import read
from workshop.registry import load_rules
from test_executors import setup, wait_for  # noqa: F401  (pytest fixture)
from test_executor_profiles import RULE_PATH, config_with, profile

SESSION = '01234567-89ab-4cde-8fab-0123456789ab'


def repository(tmp_path, profiles, files=None):
    """一个最小仓库：规则在 rules/ 下，执行者目录相对仓库根写。"""
    root = tmp_path / 'checkout'
    (root / '.git').mkdir(parents=True)
    (root / 'rules').mkdir()
    for name, content in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    rules = root / 'rules/workshop.yaml'
    rules.write_text(yaml.safe_dump(config_with(profiles)))
    return root, rules


def test_relative_directory_resolves_from_the_rule_files_repository(tmp_path):
    root, rules = repository(tmp_path, {'w': profile(executor_dir='rules/executors/w')},
                             {'rules/executors/w/settings.json': '{}', 'rules/executors/w/rules.md': ''})
    loaded = load_rules(rules)
    assert loaded.data['executor_profiles']['w']['executor_dir'] == str(root / 'rules/executors/w')
    # 已装配的规则再装配一次（绝对路径）结果不变。
    assert load_rules(rules).data['executor_profiles']['w']['executor_dir'] == str(root / 'rules/executors/w')


@pytest.mark.parametrize('files,reason', [
    ({}, '不存在'),
    ({'rules/executors/w/settings.json': '{"env": '}, 'settings.json 格式不对'),
    ({'rules/executors/w/settings.json': '[]'}, 'settings.json 格式不对'),
])
def test_missing_directory_or_bad_settings_fail_rule_load_naming_the_profile(tmp_path, files, reason):
    _, rules = repository(tmp_path, {'broken-writer': profile(executor_dir='rules/executors/w')}, files)
    with pytest.raises(ValueError, match=r'配置档 broken-writer 的 executor_dir：.*' + reason):
        load_rules(rules)


def test_codex_directory_is_refused_until_codex_can_skip_personal_config(tmp_path):
    _, rules = repository(tmp_path, {'cx': profile('codex', executor_dir='rules/executors/cx')},
                          {'rules/executors/cx/config.toml': ''})
    with pytest.raises(ValueError, match='配置档 cx 的 executor_dir：Codex'):
        load_rules(rules)
    _, rules = repository(tmp_path / 'bad', {'cx': profile('codex', executor_dir='rules/executors/cx')},
                          {'rules/executors/cx/config.toml': 'model = '})
    with pytest.raises(ValueError, match='配置档 cx 的 executor_dir：.*config.toml 格式不对'):
        load_rules(rules)


def test_directory_cannot_switch_to_another_login(tmp_path):
    _, rules = repository(tmp_path, {'w': profile(executor_dir='rules/executors/w', config_dir='~/elsewhere')},
                          {'rules/executors/w/settings.json': '{}'})
    with pytest.raises(ValueError, match='配置档 w 的 config_dir：和 executor_dir 不能同时写'):
        load_rules(rules)


def test_claude_reads_only_the_repository_layer_and_its_own_directory(tmp_path):
    root, rules = repository(tmp_path, {'w': {**profile(executor_dir='rules/executors/w', permission_mode='bypassPermissions'),
                                           'transport': 'orca'}},
                             {'rules/executors/w/settings.json': json.dumps({'env': {'MARK': '1'}}),
                              'rules/executors/w/rules.md': '只用中文写提交说明。\n'})
    p = load_rules(rules).data['executor_profiles']['w']
    e = adapters.Executor('w', profile=p, commands={'claude': '/cli/claude', 'orca': '/cli/orca'})
    workdir = tmp_path / 'home/workspace/runtime/worktrees/repo'
    argv = e._argv(p, str(workdir), SESSION, False, tmp_path, False, str(workdir))
    assert argv[argv.index('--setting-sources') + 1] == 'project'
    assert argv[argv.index('--mcp-config') + 1] == '{"mcpServers":{}}' and '--strict-mcp-config' in argv
    assert argv[argv.index('--append-system-prompt') + 1] == '只用中文写提交说明。'
    settings = read(tmp_path / 'hooks.json')
    assert settings['env'] == {'MARK': '1'} and settings['autoMemoryEnabled'] is False
    excluded = settings['claudeMdExcludes']
    # 工作目录以上每一层（如 ~/workspace/AGENTS.md）的指令都排除，工作目录自己的 AGENTS.md 不排除。
    assert str(tmp_path / 'home/workspace/AGENTS.md') in excluded
    assert str(tmp_path / 'home/workspace/CLAUDE.md') in excluded
    assert str(tmp_path / 'home/.claude/rules/**') in excluded
    assert not any(pattern.startswith(str(workdir) + '/') for pattern in excluded)
    # 车间自己的会话登记 hook 照常加在同一份设置里。
    assert 'hook.py' in settings['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']


def test_empty_rules_add_no_prompt_and_headless_passes_settings_inline(tmp_path):
    _, rules = repository(tmp_path, {'w': profile(executor_dir='rules/executors/w')},
                          {'rules/executors/w/rules.md': '\n'})
    p = load_rules(rules).data['executor_profiles']['w']
    e = adapters.Executor('w', profile=p, commands={'claude': '/cli/claude'})
    argv = e._argv(p, '/repo', SESSION, False, tmp_path, True)
    assert '--append-system-prompt' not in argv
    assert json.loads(argv[argv.index('--settings') + 1]) == {'autoMemoryEnabled': False,
                                                               'claudeMdExcludes': [f'/{n}' for n in (
                                                                   'CLAUDE.md', 'CLAUDE.local.md', 'AGENTS.md',
                                                                   '.claude/CLAUDE.md', '.claude/rules/**')]}


def test_rule_file_points_writers_at_their_directories_and_leaves_other_profiles_alone():
    rules = load_rules(RULE_PATH)
    profiles = rules.data['executor_profiles']
    repo = RULE_PATH.parents[1]
    assert profiles['orca-claude']['executor_dir'] == str(repo / 'rules/executors/orca-claude')
    assert (repo / 'rules/executors/orca-claude/rules.md').read_text() == ''
    # Codex 在它的 CLI 支持之前例外（#283 Kun 选 C）：配置档不变，不指向目录。
    assert 'executor_dir' not in profiles['orca-codex']
    # 不读用户设置后，bypassPermissions 交互启动会停在确认框（#283 真实启动实测），这项只能写在执行者目录里。
    settings = json.loads((repo / 'rules/executors/orca-claude/settings.json').read_text())
    assert settings['skipDangerousModePermissionPrompt'] is True
    for name in ('codex', 'claude', 'cursor-grok', 'manager-codex', 'manager-claude', 'watch-inventory'):
        assert 'executor_dir' not in profiles[name]


def test_writer_keeps_subscription_login_and_workshop_environment(setup, monkeypatch):
    root, make, item, target = setup
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'metered-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'metered-key')
    directory = root / 'executor-dir'
    directory.mkdir()
    (directory / 'settings.json').write_text('{}')
    executor = make('orca-claude', {**adapters.Executor('orca-claude').profile, 'executor_dir': str(directory)})
    started = executor.launch('one', Brief('执行', [], workdir=item['workdir']), item, {}, target)
    wait_for(lambda: (root / 'agents').exists() and (root / 'agents').read_text().endswith('\n'))
    call = json.loads((root / 'agents').read_text())
    # 沿用本机登录：不换配置目录；按量计费的密钥照旧不带进去。
    for key in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY'):
        assert key not in call['env']
    # 车间加给执行者的环境照常：回执用的轮次和活号，终端外壳的登录环境。
    assert call['env']['WORKSHOP_ITEM'] == item['id'] and call['env']['WORKSHOP_ROUND_TOKEN'] == 'one'
    assert call['env']['LOGIN_ONLY'] == 'from-terminal-shell'
    assert '--setting-sources' in call['args']
    settings = read(executor.directory('one') / 'hooks.json')
    assert 'hook.py' in settings['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    executor.stop('one', started)

