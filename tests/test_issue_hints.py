"""ws#326：check-issue 和接单核对提示约束与 DoD 的路径、命令冲突；只提示，不改核对结论。"""
import pytest
import yaml

from workshop.cli import command_tree, main
from workshop.plugins.issue_delivery import dod
from workshop.plugins.issue_delivery.hints import hints
from workshop.reports.report import why
from conftest import configuration
from test_issue_delivery import BODY, edit, world  # noqa: F401
from test_issue_end_to_end import engine_for, item, set_body, start

TARGET = {'name': 'alpha', 'requirement_docs': ['docs/requirements/']}
WORKSHOP = {'name': 'workshop', 'requirement_docs': ['docs/requirements/']}
TREE = {'check-issue': {}, 'why': {}, 'reports': {'messages': {}}}


def issue(dod_items, constraints, background='失败输出要原样留下（核心 R14）。'):
    return (f'## 背景和目标\n{background}\n## 合并前 DoD\n' + ''.join(f'- {d}\n' for d in dod_items)
            + '## 约束\n' + ''.join(f'- {c}\n' for c in constraints) + '## 要不要亲眼看\n否\n'
            + '## Kun 的原话\n> 新增 `src/workshop/core/x.py`\n')


ONLY = '只改介入点插件和报告：`src/workshop/plugins/intervention/`、`src/workshop/reports/`；不改核心。'


def test_path_outside_only_scope_names_constraint_and_dod_sentence():
    body = issue(['在 `src/workshop/cli.py` 新增子命令，测试覆盖', '报告改成按人汇总：`scripts/check.sh` 通过'], [ONLY])
    assert hints(body, TARGET) == [
        '提示：约束「' + ONLY + '」只改范围外：合并前 DoD「在 `src/workshop/cli.py` 新增子命令，测试覆盖」要改 src/workshop/cli.py']


def test_path_inside_not_scope():
    constraint = '不改 `src/workshop/core/`'
    body = issue(['修改 src/workshop/core/engine.py 的派活'], [constraint])
    assert hints(body, TARGET) == [
        '提示：约束「' + constraint + '」不改 src/workshop/core/：合并前 DoD「修改 src/workshop/core/engine.py 的派活」要改 src/workshop/core/engine.py']


@pytest.mark.parametrize('sentence', [
    '新增回归测试，`scripts/check.sh` 通过',                        # 登记的检查不算
    '新增断言，`uv run pytest tests/test_messages.py` 通过',          # 只写「通过」的检查命令不算
    '不修改 `src/workshop/cli.py`',                                  # 否定的动词不算
    '在 `src/workshop/reports/messages.py` 新增报告',                 # 范围内
    '在 `workshop/reports/messages.py` 新增报告',                     # 相对写法，仍在范围内
    '新增到 `kun-g/workshop` 的 R12/R13 说明',                        # 仓库名、编号不是路径
    '`src/workshop/cli.py` 的输出不变',                               # 同一句没有改动的动词
])
def test_not_counted(sentence):
    assert hints(issue([sentence], [ONLY]), TARGET) == []


def test_body_sentence_is_named_by_section_and_quotes_and_constraints_are_not_scanned():
    body = issue(['`scripts/check.sh` 通过'], [ONLY], background='顺手删除 `docs/old/说明.md`。')
    assert hints(body, TARGET) == [
        '提示：约束「' + ONLY + '」只改范围外：背景和目标「顺手删除 `docs/old/说明.md`」要改 docs/old/说明.md']


def test_requirement_change_outside_only_scope():
    background = '加度量。\n\n需求变更：在 `docs/requirements/介入点.md` 新增 R35：发件先量。'
    body = issue(['`scripts/check.sh` 通过'], [ONLY], background)
    documents = {'docs/requirements/介入点.md': '', 'docs/requirements/核心.md': ''}
    assert hints(body, TARGET, documents) == [
        '提示：需求变更要改 docs/requirements/介入点.md，不在约束「' + ONLY + '」的只改范围内']
    # 没有需求文档列表时从需求变更原文取路径。
    assert hints(body, TARGET) == hints(body, TARGET, documents)
    widened = ONLY.replace('`src/workshop/reports/`', '`src/workshop/reports/`、`docs/requirements/`')
    assert hints(issue(['`scripts/check.sh` 通过'], [widened], background), TARGET, documents) == []
    assert hints(issue(['`scripts/check.sh` 通过'], ['不改核心'], background), TARGET, documents) == []


def test_unknown_workshop_subcommand():
    body = issue(['`workshop reports outbox` 按人输出；另列次数', '范围声明：`workshop reports outbox` 的输出注明',
                  '`workshop why ws#1` 能看到', '新增 `workshop schedule add` 命令',
                  '出现 `workshop <子命令>` 时提示', '`workshop --state-dir /tmp/x reports messages` 照旧',
                  '`workshop check-issue/why` 能用'], ['无'])
    assert hints(body, WORKSHOP, commands=TREE) == [
        '提示：合并前 DoD「`workshop reports outbox` 按人输出」用到 `workshop reports outbox`，'
        '现有命令里没有 reports outbox，同一句也没写「新增」']
    # 斜杠并列的几个子命令（ws#311 的 `workshop schedule add/list/disable`）逐个查，都在就不提示。
    slash = issue(['`workshop reports messages/outbox` 输出'], ['无'])
    assert hints(slash, WORKSHOP, commands=TREE) == [
        '提示：合并前 DoD「`workshop reports messages/outbox` 输出」用到 `workshop reports messages/outbox`，'
        '现有命令里没有 reports outbox，同一句也没写「新增」']
    # 只看 workshop 交付对象；没有命令表时不查。
    assert hints(body, TARGET, commands=TREE) == []
    assert hints(body, WORKSHOP) == []


def test_command_tree_comes_from_cli_parser():
    tree = command_tree()
    assert tree['reports'] == {'messages': {}}
    assert tree['check-issue'] == {} and 'why' in tree


def test_hints_do_not_change_registration():
    body = issue(['在 `src/workshop/cli.py` 新增子命令'], [ONLY])
    assert hints(body, TARGET)
    assert dod.registration(body, TARGET) == ([], [['scripts/check.sh']])


@pytest.fixture
def check(world, monkeypatch):
    config = configuration()
    config['targets'] = {'workshop': {**world['target'], 'default_flow': 'issue_delivery',
                                      'flows': {'issue_delivery': {'triggers': []}}}}
    path = world['root'] / 'rules.yaml'
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv('WORKSHOP_RULES', str(path))
    monkeypatch.setenv('WORKSHOP_STATE_DIR', str(world['root'] / 'never-created'))
    draft = world['root'] / 'draft.md'

    def run(body):
        draft.write_text(body)
        return main(['check-issue', '--file', str(draft), '--target', 'workshop'])
    return run


def test_check_issue_prints_hints_and_still_passes(check, capsys):
    body = BODY.replace('## 约束\n无\n', '## 约束\n- 只改 `src/workshop/reports/`\n').replace(
        '- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n',
        '- 错误输出和非零退出码保留：`scripts/check.sh` 通过。\n- `workshop reports outbox` 输出基线\n'
        '- 在 `src/workshop/cli.py` 新增子命令\n')
    assert check(body) == 0
    assert capsys.readouterr().out == (
        '接单核对通过，要跑的检查（交付对象规则登记）：\n["scripts/check.sh"]\n'
        '提示：约束「只改 `src/workshop/reports/`」只改范围外：合并前 DoD「在 `src/workshop/cli.py` 新增子命令」要改 src/workshop/cli.py\n'
        '提示：合并前 DoD「`workshop reports outbox` 输出基线」用到 `workshop reports outbox`，'
        '现有命令里没有 reports outbox，同一句也没写「新增」\n')


def test_check_issue_with_problems_still_lists_hints(check, capsys):
    body = BODY.replace('## 约束\n无\n', '## 约束\n写这张单特有的限制\n').replace(
        '`scripts/check.sh` 通过。\n', '`scripts/check.sh` 通过。\n- `workshop reports outbox` 输出\n')
    assert check(body) == 1
    out = capsys.readouterr().out.splitlines()
    assert out == [dod.CONSTRAINT, '提示：合并前 DoD「`workshop reports outbox` 输出」用到 `workshop reports outbox`，'
                   '现有命令里没有 reports outbox，同一句也没写「新增」']


def test_intake_with_hints_proceeds_and_why_shows_them(world):
    body = BODY.replace('## 约束\n无\n', '## 约束\n- 只改 `src/workshop/reports/`\n').replace(
        '`scripts/check.sh` 通过。\n', '`scripts/check.sh` 通过。\n- 在 `src/workshop/cli.py` 新增子命令\n')
    set_body(world, body)
    engine = engine_for(world, 'hints')
    start(engine)
    assert item(engine, '7')['stage'] == '执行'
    text = why(engine.log, 'alpha:7')
    assert ('接单核对：合并前 DoD、约束都在，检查按交付对象规则；提示：约束「只改 `src/workshop/reports/`」只改范围外：'
            '合并前 DoD「在 `src/workshop/cli.py` 新增子命令」要改 src/workshop/cli.py') in text
    engine.log.close()
