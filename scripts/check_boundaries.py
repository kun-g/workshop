"""R22 与插件隔离：动态覆盖新增的插件，不维护第二份固定名单。"""
import ast
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
for path in (root / "src/workshop/core").rglob("*.py"):
    if re.search(r"issue|pull|merge|(?<![a-z])prs?(?![a-z])|视频|周报|worktree", path.read_text(), re.IGNORECASE):
        raise SystemExit(f"核心里出现了某一类活的词（R22）：{path}")

# 动态加载也属于依赖边界；先扫描源码，不执行任何插件代码。
for area in ("core", "plugins"):
    for path in (root / "src/workshop" / area).rglob("*.py"):
        tree = ast.parse(path.read_text())
        aliases = {"__import__": "__import__", "import_module": "import_module", "entry_points": "entry_points"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for name in node.names:
                    if name.name in aliases:
                        aliases[name.asname or name.name] = name.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (aliases.get(node.func.id) if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute) else None)
            if name in ("__import__", "import_module"):
                raise SystemExit(f"禁止动态导入：{path}:{node.lineno}: {ast.get_source_segment(path.read_text(), node)}")
            if area == "plugins" and name == "entry_points":
                groups = [kw.value for kw in node.keywords if kw.arg == "group"]
                own_driver = (path.is_relative_to(root / "src/workshop/plugins/executors/local")
                              and not node.args and all(kw.arg is not None for kw in node.keywords)
                              and len(groups) == 1 and isinstance(groups[0], ast.Constant)
                              and groups[0].value == "workshop.driver")
                if not own_driver:
                    raise SystemExit(f"插件只能动态加载自己的入口点组：{path}:{node.lineno}")

plugins = root / "src/workshop/plugins"
modules = []
categories = {"sources", "executors", "channels", "taskhome", "selectors"}
for child in plugins.iterdir():
    if child.name.startswith(("__", ".")):
        continue
    if child.is_dir() and child.name in categories:
        candidates = child.iterdir()
    else:
        candidates = [child]
    for path in candidates:
        if path.name.startswith(("__", ".")):
            continue
        if path.is_dir() and (path / "__init__.py").exists() or path.suffix == ".py":
            relative = path.relative_to(root / "src")
            if path.is_file():
                relative = relative.with_suffix("")
            modules.append(".".join(relative.parts))
config = "[importlinter]\nroot_package = workshop\n"
if len(modules) > 1:
    config += "\n[importlinter:contract:plugins]\nname = 插件之间不互引\ntype = independence\nmodules =\n"
    config += "".join(f"    {name}\n" for name in sorted(modules))

allowed = {"__init__", "models", "ports"}
forbidden = sorted(f"workshop.core.{p.stem}" for p in (root / "src/workshop/core").glob("*.py")
                   if p.stem not in allowed)
config += """
[importlinter:contract:plugins-core]
name = 插件只用核心的 models 和 ports
type = forbidden
source_modules = workshop.plugins
forbidden_modules =
""" + "".join(f"    {name}\n" for name in forbidden) + """
# models / ports 内部的依赖由核心维护，只限制插件的直接引用。
allow_indirect_imports = True
ignore_imports =
    # H2：核心把可填阶段交给流程后删除。
    workshop.plugins.issue_delivery -> workshop.core.gates
    # H6：卡片移到介入点插件后删除。
    workshop.plugins.issue_delivery -> workshop.core.cards
    # H6：旧通道卡片路径迁走后删除。
    workshop.plugins.channels.ask_kun -> workshop.core.cards
    # H3：核心完成重启确认后删除。
    workshop.plugins.scheduled_release.io -> workshop.core.wake
    # H4：确认存量迁移完成、删除一次性迁移后删除。
    workshop.plugins.watch_updates.migration -> workshop.core.log

[importlinter:contract:plugins-outer]
name = 插件不引用装配层、看门和查看工具
type = forbidden
source_modules = workshop.plugins
forbidden_modules =
    workshop.cli
    workshop.registry
    workshop.reports
    workshop.watch

[importlinter:contract:shared-libs]
name = 共用库不引用插件
type = forbidden
source_modules =
    workshop.github_identity
    workshop.path_policy
    workshop.running
forbidden_modules = workshop.plugins
"""
(root / ".local").mkdir(exist_ok=True)
with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", dir=root / ".local", delete=True) as file:
    file.write(config)
    file.flush()
    subprocess.run(["uv", "run", "lint-imports", "--config", file.name], cwd=root, check=True)
print("核心用词与插件边界检查通过")
