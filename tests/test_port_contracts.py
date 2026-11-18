"""检查器自身要能拦错端口、漏声明和不完整的入口点实现。"""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('check_ports', Path(__file__).parents[1] / 'scripts/check_ports.py')
checker = importlib.util.module_from_spec(spec)
# dataclass resolves its defining module.
import sys
sys.modules[spec.name] = checker
spec.loader.exec_module(checker)


def errors(code, declarations=None):
    declared = declarations or {key: checker.members(p) for key, p in checker.PROTOCOLS.items()}
    scan = checker.Calls(Path('src/workshop/core/example.py'), declared)
    scan.visit(ast.parse(code))
    return scan.errors


@pytest.mark.parametrize('code,missing', [
    ('engine.call("flow", "x", "undeclared")', 'flow.undeclared'),
    ('port, name = "flow", "x"\nengine.call(port, name, "undeclared")', 'flow.undeclared'),
    ('engine.plugins.get("flow", "x").undeclared({})', 'flow.undeclared'),
    ('plugin = engine.plugins.get("flow", "x")\ngetattr(plugin, "undeclared", None)', 'flow.undeclared'),
    ('plugin = engine.plugins.get("driver", "x")\nalias = plugin\nvalue = alias.undeclared', 'driver.undeclared'),
    ('engine.call("source", "x", "brief")', 'source.brief'),
    ('def helper(flow: Flow):\n    return flow.undeclared', 'flow.undeclared'),
])
def test_all_call_forms_are_checked(code, missing):
    assert any(missing in error for error in errors(code))


def test_deleted_declaration_and_dynamic_names_fail():
    declared = {key: checker.members(p) for key, p in checker.PROTOCOLS.items()}
    del declared['flow']['stages']
    assert any('flow.stages' in error for error in errors('engine.call("flow", "x", "stages")', declared))
    assert errors('engine.call("flow", "x", unknown)')
    assert errors('plugin = engine.plugins.get("flow", "x")\ngetattr(plugin, unknown)')


def test_real_tree_and_entry_points_match():
    assert checker.check_calls() == []
    assert checker.check_implementations() == []


class Automatic:
    def automatic(self, item, stage, target):
        return True


class Conditional:
    def automatic(self, item, stage, target):
        return stage == '执行'


@pytest.mark.parametrize('plugin,missing', [(Automatic(), False), (Conditional(), True), (object(), True)])
def test_brief_optional_only_for_constant_automatic(plugin, missing):
    assert ('brief' in checker.missing_members('flow', plugin)) is missing
    assert 'stages' in checker.missing_members('flow', plugin)


def test_entry_point_missing_required_method_is_rejected(monkeypatch):
    entry = SimpleNamespace(group='workshop.source', name='incomplete', load=lambda: object)
    monkeypatch.setattr(checker, 'entry_points', lambda *, group: [entry] if group == entry.group else [])
    assert checker.check_implementations() == ['workshop.source/incomplete: 缺少必需成员 source.poll']
