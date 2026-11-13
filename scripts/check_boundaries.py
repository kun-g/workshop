"""R22 与插件隔离：动态覆盖新增的插件，不维护第二份固定名单。"""
import ast
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
for path in (root / "src/workshop/core").rglob("*.py"):
    if re.search(r"\b(PR|pull|issue|merge)\b|视频|周报", path.read_text()):
        raise SystemExit(f"核心里出现了某一类活的词（R22）：{path}")

plugins = root / "src/workshop/plugins"
modules = []
categories = {"sources", "executors", "channels", "taskhome"}
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
if len(modules) > 1:
    config = "[importlinter]\nroot_package = workshop\n\n[importlinter:contract:plugins]\nname = 插件之间不互引\ntype = independence\nmodules =\n"
    config += "".join(f"    {name}\n" for name in modules)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ini", dir=root / ".local", delete=True) as file:
        file.write(config)
        file.flush()
        subprocess.run(["uv", "run", "lint-imports", "--config", file.name], cwd=root, check=True)
else:
    print("插件隔离合约：当前不足两个实现模块；新增后自动检查")

# 装配允许在顶层，核心不得经动态导入绕过静态合约。
for path in (root / "src/workshop/core").rglob("*.py"):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id == "__import__"
                or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"):
            raise SystemExit(f"核心禁止动态导入：{path}")
print("核心用词与插件边界检查通过")
