"""记忆 R36 的边界：插件不互引，核心不出现介入点的词。

静态检查范围按 DoD 限定在 `src/workshop/plugins/memory/`（不扫 tests/）。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMORY = ROOT / "src/workshop/plugins/memory"


def imports(path):
    tree = ast.parse(path.read_text())
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            result.add(node.module)
    return result


def test_memory_plugin_does_not_import_other_plugins():
    for path in MEMORY.rglob("*.py"):
        for name in imports(path):
            assert not (name.startswith("workshop.plugins.") and not name.startswith("workshop.plugins.memory")), \
                f"{path}: 记忆插件不许 import 其他插件：{name}"


def test_memory_plugin_does_not_import_intervention():
    for path in MEMORY.rglob("*.py"):
        # 只读打开介入点发件表是允许的（路径字符串）；引用介入点的模块或代码不行。
        names = imports(path)
        assert not any(name.startswith("workshop.plugins.intervention") for name in names), \
            f"{path}: 严禁 import 介入点插件"


def test_core_has_no_intervention_words():
    for path in (ROOT / "src/workshop/core").rglob("*.py"):
        text = path.read_text()
        assert "intervention.db" not in text, f"{path}: 核心不许出现 intervention.db"
        assert "intervention" not in text, f"{path}: 核心不许出现 intervention"


def test_intervention_plugin_does_not_import_core():
    for path in (ROOT / "src/workshop/plugins/intervention").rglob("*.py"):
        for name in imports(path):
            assert not name.startswith("workshop.core"), f"{path}: 介入点插件不许 import {name}"


def test_memory_plugin_only_uses_core_models_and_ports():
    """核心 R22 的同一份边界：记忆插件只许用 core 的 models 和 ports。"""
    allowed = {"workshop.core.models", "workshop.core.ports"}
    for path in MEMORY.rglob("*.py"):
        for name in imports(path):
            if name.startswith("workshop.core"):
                assert name in allowed, f"{path}: 记忆插件只能用核心的 models 和 ports，不许 {name}"
