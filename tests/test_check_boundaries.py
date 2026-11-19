"""Run the boundary command against source files; never execute plugin code."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/check_boundaries.py'


def check(tmp_path, source, module='sample', *, new_core=False):
    (tmp_path / 'scripts').mkdir()
    shutil.copy2(SCRIPT, tmp_path / 'scripts/check_boundaries.py')
    (tmp_path / '.local').mkdir()
    package = tmp_path / 'src/workshop'
    shutil.copytree(SCRIPT.parents[1] / 'src/workshop', package, ignore=shutil.ignore_patterns('__pycache__'))
    if new_core:
        (package / 'core/new_mechanism.py').write_text('')
    path = package / 'plugins' / (module + '.py')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    env = {**os.environ, 'PYTHONPATH': str(tmp_path / 'src'), 'VIRTUAL_ENV': sys.prefix,
           'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}
    return subprocess.run([sys.executable, str(tmp_path / 'scripts/check_boundaries.py')],
                          cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)


def test_plugin_dynamic_import_is_rejected(tmp_path):
    result = check(tmp_path, "__import__('workshop.core.beat')\n")
    assert result.returncode != 0
    assert 'sample.py:1' in result.stderr
    assert '动态' in result.stderr


@pytest.mark.parametrize('source', [
    "import importlib; importlib.import_module('workshop.core.beat')",
    "from importlib import import_module; import_module('workshop.core.beat')",
    "from importlib import import_module as load; load('workshop.core.beat')",
    "from builtins import __import__ as load; load('workshop.core.beat')",
    "from importlib.metadata import entry_points; entry_points(group='workshop.flow')",
    "from importlib.metadata import entry_points as entries; entries(group='workshop.driver')",
    "import importlib.metadata as meta; meta.entry_points().select(group='workshop.flow')",
])
def test_plugin_cannot_bypass_boundary_with_dynamic_loading(tmp_path, source):
    result = check(tmp_path, source)
    assert result.returncode != 0
    assert 'sample.py:1' in result.stderr


def test_executor_can_load_its_own_drivers(tmp_path):
    result = check(tmp_path, "from importlib.metadata import entry_points as entries; entries(group='workshop.driver')",
                   'executors/local/probe')
    assert result.returncode == 0, result.stdout + result.stderr
    assert '插件只用核心的 models 和 ports KEPT' in result.stdout
    assert '共用库不引用插件 KEPT' in result.stdout


@pytest.mark.parametrize('call', ["entry_points(group='workshop.flow')", "entry_points(group=group)",
                                  "entry_points(**options)"])
def test_executor_cannot_load_other_or_unknown_groups(tmp_path, call):
    result = check(tmp_path, 'from importlib.metadata import entry_points; ' + call, 'executors/local/probe')
    assert result.returncode != 0
    assert 'probe.py:1' in result.stderr


def test_new_core_module_is_automatically_forbidden(tmp_path):
    result = check(tmp_path, 'from workshop.core import new_mechanism', new_core=True)
    assert result.returncode != 0
    assert 'workshop.core.new_mechanism' in result.stdout
    assert 'BROKEN' in result.stdout


@pytest.mark.parametrize('source', [
    "from workshop.core.rules import Rules\nRules.load('rules/workshop.yaml')\n",
    "from workshop.core.rules import Rules as R\nR.load('rules/workshop.yaml')\n",
])
def test_rule_loading_outside_the_assembly_function_is_rejected(tmp_path, source):
    # #293：src 里读规则都经 workshop.deployment.assemble，否则会漏掉部署目录。
    result = check(tmp_path, '', 'harmless')
    assert result.returncode == 0, result.stdout + result.stderr
    (tmp_path / 'src/workshop/sneaky.py').write_text(source)
    env = {**os.environ, 'PYTHONPATH': str(tmp_path / 'src'), 'VIRTUAL_ENV': sys.prefix,
           'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']}
    result = subprocess.run([sys.executable, str(tmp_path / 'scripts/check_boundaries.py')],
                            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert 'sneaky.py:2' in result.stderr and 'assemble' in result.stderr
