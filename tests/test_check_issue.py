"""自检走 CLI 和接单相同的读取/核对路径，状态库及来源均只读。"""
from pathlib import Path

import pytest
import yaml

from workshop.cli import main
from conftest import configuration
from test_issue_delivery import BODY, calls, edit, snapshot, world  # noqa: F401
from workshop.core.models import Receipt


@pytest.fixture
def command(world, monkeypatch):
    config = configuration()
    config['targets'] = {'alpha': {**world['target'], 'default_flow': 'issue_delivery',
        'flows': {'issue_delivery': {'triggers': []}}}}
    path = world['root'] / 'rules.yaml'
    path.write_text(yaml.safe_dump(config))
    state = world['root'] / 'never-created'
    monkeypatch.setenv('WORKSHOP_RULES', str(path))
    monkeypatch.setenv('WORKSHOP_STATE_DIR', str(state))
    # Any state access (including opening an existing database) is a regression.
    monkeypatch.setattr('workshop.cli.Log', lambda *a, **kw: pytest.fail('opened state database'))
    yield lambda *args: main(['check-issue', *args])
    assert not state.exists()
    assert not (world['root'] / 'flow').exists()
    for call in calls(world):
        assert call[0] == 'gh' and call[1] == 'api'
        assert '--method' not in call and '--input' not in call


@pytest.mark.parametrize('reference', ['ws#7', 'alpha:7'])
def test_complete_issue(command, world, capsys, reference):
    assert command(reference) == 0
    assert capsys.readouterr().out == '接单核对通过，能跑的检查：\n["scripts/check.sh"]\n'
    endpoints = [call[2] for call in calls(world)]
    assert 'repos/kun-g/workshop/issues/7' in endpoints
    assert any('/contents/' in endpoint and '?ref=' not in endpoint for endpoint in endpoints)


@pytest.mark.parametrize('body', [
    '',
    BODY.replace('## 依据\n核心 R14\n', ''),
    BODY.replace('核心 R14', 'R14'),
    BODY.replace('核心 R14', '核心 R99'),
    BODY.replace('核心 R14', '需求变更：在 docs/requirements/核心.md 新增：只通知一次。')
        .replace('检查失败时让错误可见。', '来源：运行现象'),
])
def test_missing_items_match_intake_next(command, world, capsys, body):
    edit(world, issue={'number': 7, 'body': body})
    assert command('ws#7') == 1
    output = capsys.readouterr()
    assert not output.err
    evidence = world['flow'].assess(snapshot(world, '接单'), Receipt('done', '核对'), world['target'])
    assert evidence.awaits_update
    assert evidence.reason == '接单核对没过，issue 补齐后自动接单：' + '；'.join(output.out.splitlines())


@pytest.mark.parametrize('body,code', [(BODY, 0), (BODY.replace('核心 R14', 'R14'), 1)])
def test_draft(command, world, capsys, body, code):
    path = world['root'] / 'draft.md'
    path.write_text(body)
    assert command('--file', str(path), '--target', 'ws') == code
    assert capsys.readouterr().out
    assert all('/issues/' not in call[2] for call in calls(world))


def test_template_is_incomplete(command, capsys):
    template = Path(__file__).resolve().parents[1] / '.github/ISSUE_TEMPLATE/issue-delivery.md'
    assert command('--file', str(template), '--target', 'alpha') == 1
    assert '「合并前 DoD」还有待定项' in capsys.readouterr().out


def test_flow_configuration_and_nested_default_branch_docs(command, world, monkeypatch, capsys):
    path = world['root'] / 'rules.yaml'
    config = yaml.safe_load(path.read_text())
    config['targets']['alpha']['flows']['issue_delivery'].update(
        requirement_docs=['docs/spec/'], required_checks=[['project-check', '--all']])
    path.write_text(yaml.safe_dump(config))
    edit(world, requirement_contents={'docs/spec/nested/项目.md': '**R9 项目要求。**'},
         issue={'body': BODY.replace('核心 R14', '项目 R9').replace('[["scripts/check.sh"]]', '[["project-check", "--all"]]')})
    assert command('ws#7') == 0
    assert '["project-check", "--all"]' in capsys.readouterr().out


@pytest.mark.parametrize('args', [[], ['ws#0'], ['ws#abc'], ['unknown#7'], ['ws#7', '--target', 'ws'],
    ['--file', 'missing.md'], ['ws#7', '--file', 'missing.md', '--target', 'ws'],
    ['--file', 'missing.md', '--target', 'ws']])
def test_invalid_input_is_error_without_network(command, world, capsys, args):
    assert command(*args) == 2
    assert capsys.readouterr().err.startswith('车间：')
    assert not calls(world)


def test_read_failure_does_not_claim_missing_fields(command, world, capsys):
    edit(world, contents_error='gh: Not Found (HTTP 404)')
    assert command('ws#7') == 2
    output = capsys.readouterr()
    assert '404' in output.err and not output.out


def test_pr_is_not_an_issue(command, world, capsys):
    edit(world, issue={'body': BODY, 'pull_request': {'url': 'https://example.invalid/pr'}})
    assert command('ws#7') == 2
    assert '是 PR' in capsys.readouterr().err


@pytest.mark.parametrize('fail', [False, True])
def test_configured_app_identity_is_used(command, world, monkeypatch, capsys, fail):
    from test_github_identity_flow import configure
    observed = configure(world, monkeypatch, fail=fail)
    path = world['root'] / 'rules.yaml'
    config = yaml.safe_load(path.read_text())
    config['targets']['alpha']['github_identity'] = world['target']['github_identity']
    path.write_text(yaml.safe_dump(config))
    assert command('ws#7') == (2 if fail else 0)
    assert sum(argv == 'sign' for argv, _ in observed) == 1
    if fail:
        assert observed == [('sign', None)]
        assert 'App 签发失败' in capsys.readouterr().err
    else:
        assert all(env['GH_TOKEN'] == 'app-token' for argv, env in observed if argv != 'sign')
