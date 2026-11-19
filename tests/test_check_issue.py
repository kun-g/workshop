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
    assert capsys.readouterr().out == '接单核对通过，要跑的检查（交付对象规则登记）：\n["scripts/check.sh"]\n'
    endpoints = [call[2] for call in calls(world)]
    assert 'repos/kun-g/workshop/issues/7' in endpoints
    assert any('/contents/' in endpoint and '?ref=' not in endpoint for endpoint in endpoints)


@pytest.mark.parametrize('body', [
    '',
    BODY.replace('## 约束\n无\n', ''),
    BODY.replace('## 约束\n无\n', '## 约束\n写这张单特有的限制\n'),
    BODY.replace('（核心 R14）。\n', '（核心 R14）。\n\n需求变更：在 docs/requirements/核心.md 新增：只通知一次。\n')
        .replace('检查失败时让错误可见。', '来源：运行现象'),
])
def test_missing_items_match_intake_next(command, world, capsys, body):
    edit(world, issue={'number': 7, 'body': body})
    assert command('ws#7') == 1
    output = capsys.readouterr()
    assert not output.err
    evidence = world['flow'].assess(snapshot(world, '接单'), Receipt('done', '核对'), world['target'])
    assert evidence.awaits_update
    problems = [line for line in output.out.splitlines() if not line.startswith('进交付时要求 PR 改的需求文档：')]
    assert evidence.reason == '接单核对没过，issue 补齐后自动接单：' + '；'.join(problems)


@pytest.mark.parametrize('draft', [False, True])
def test_requirement_changes_show_gate_documents_only(command, world, capsys, draft):
    from workshop.plugins.issue_delivery.requirements import change_documents, requirement_basis

    documents = {'docs/requirements/核心.md': '**R14 要求。**',
                 'docs/requirements/交付.md': '**R6 要求。**'}
    path = world['root'] / 'rules.yaml'
    config = yaml.safe_load(path.read_text())
    config['targets']['alpha']['requirement_docs'] = ['docs/requirements/']
    path.write_text(yaml.safe_dump(config))
    body = BODY.replace('（核心 R14）。\n', '（核心 R14）。\n\n需求变更：在 核心.md 新增：失败后只通知一次。\n\n参见交付 R6。\n')
    edit(world, requirement_contents=documents, issue={'number': 7, 'body': body})
    draft_path = world['root'] / 'draft.md'
    draft_path.write_text(body)
    args = ['--file', str(draft_path), '--target', 'ws'] if draft else ['ws#7']
    assert command(*args) == 0
    required = change_documents(requirement_basis(body), documents, config['targets']['alpha'])
    assert required == {'docs/requirements/核心.md'}
    assert capsys.readouterr().out == ('进交付时要求 PR 改的需求文档：' + '、'.join(sorted(required))
                                    + '\n接单核对通过，要跑的检查（交付对象规则登记）：\n["scripts/check.sh"]\n')


@pytest.mark.parametrize('body,code', [(BODY, 0), (BODY.replace('## 约束\n无\n', ''), 1)])
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
         issue={'body': BODY.replace('核心 R14', '项目 R9')})
    assert command('ws#7') == 0
    # 检查只取交付对象规则，issue 里不用写。
    out = capsys.readouterr().out
    assert '["project-check", "--all"]' in out and 'scripts/check.sh"]' not in out


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


SESSION = '2ba67f64-4e2b-47b0-9213-00c871c6e28c'
QUOTED = BODY.replace('## Kun 的原话\n检查失败时让错误可见。\n',
                      f'## Kun 的原话\n2026-11-19，协调对话 `{SESSION}`。Kun：\n> 检查失败时让错误可见。\n')


def intake_rules(world, **switch):
    path = world['root'] / 'rules.yaml'
    config = yaml.safe_load(path.read_text())
    config['targets']['alpha'].update(auto_intake=True, intake_app_logins=['lambdatheory-agent[bot]'], **switch)
    path.write_text(yaml.safe_dump(config, allow_unicode=True))


@pytest.mark.parametrize('switch,body,code,reason', [
    ({}, QUOTED.replace(f'`{SESSION}`', '`<会话号>`'), 1, '引用行以外没写会话号'),
    ({}, QUOTED.replace('> 检查', '检查'), 1, '没有 > 引用的原话'),
    ({}, QUOTED, 0, None),
    ({'intake_require_session': True}, QUOTED.replace(f'`{SESSION}`', '`<会话号>`'), 1, '没写会话号'),
    ({'intake_require_session': False}, QUOTED.replace(f'`{SESSION}`', '`<会话号>`'), 0, None),
    ({'intake_require_session': False}, QUOTED.replace('> 检查', '检查'), 1, '没有 > 引用的原话'),
])
def test_draft_runs_intake_origin_check(command, world, capsys, switch, body, code, reason):
    intake_rules(world, **switch)
    path = world['root'] / 'draft.md'
    path.write_text(body)
    assert command('--file', str(path), '--target', 'ws') == code
    out = capsys.readouterr().out
    if reason:
        assert reason in out and '接单核对通过' not in out
    else:
        assert '接单核对通过' in out


def test_draft_origin_matches_intake(command, world, capsys):
    """草稿判不可信的，开出来自动接单也丢弃，且理由相同。"""
    from workshop.core.models import Event
    intake_rules(world)
    body = QUOTED.replace(f'`{SESSION}`', '`<会话号>`')
    path = world['root'] / 'draft.md'
    path.write_text(body)
    assert command('--file', str(path), '--target', 'ws') == 1
    problems = capsys.readouterr().out.strip().splitlines()
    edit(world, issue={'number': 7, 'body': body, 'state': 'open', 'labels': [],
                       'user': {'login': 'lambdatheory-agent[bot]'}})
    target = {**world['target'], 'auto_intake': True, 'intake_app_logins': ['lambdatheory-agent[bot]']}
    value = world['flow'].classify(Event('github', 'one', 'alpha', '7', '登记', {'kind': 'issue', 'number': '7'}),
                                   None, target, None)
    assert value.discard and value.summary == '未开放或开单来源未核实：' + '；'.join(problems)


@pytest.mark.parametrize('author,code', [('kun-g', 0), ('lambdatheory-agent[bot]', 1), ('stranger', 1)])
def test_open_issue_checks_real_author(command, world, capsys, author, code):
    intake_rules(world)
    edit(world, issue={'number': 7, 'body': BODY, 'user': {'login': author}})
    assert command('ws#7') == code
    out = capsys.readouterr().out
    assert ('开单来源' in out) == bool(code)
