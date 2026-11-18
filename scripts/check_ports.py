"""核对端口声明、调用和入口点实现；不执行插件的业务方法。

追踪注册表取出的对象、局部别名、字符串/元组、循环和条件表达式。
跨函数传递的插件参数用端口类型注解；动态转发仅允许 Engine.call / jobs.execute。
无法解析的端口/成员名报错，避免新增动态调用静默绕过检查。
"""
import ast
from dataclasses import dataclass
from importlib.metadata import entry_points
import inspect
from pathlib import Path
import textwrap

from workshop.core import ports

ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = {name: getattr(ports, cls) for name, cls in {
    'source': 'Source', 'flow': 'Flow', 'executor': 'Executor', 'channel': 'Channel',
    'taskhome': 'TaskHome', 'selector': 'Selector', 'wait': 'WaitService',
    'driver': 'Driver', 'plugins': 'Plugins',
}.items()}


def members(protocol):
    return {**{name: value for name, value in vars(protocol).items()
               if not name.startswith('_') and callable(value)},
            **dict.fromkeys(protocol.__annotations__)}


@dataclass(frozen=True)
class Value:
    strings: frozenset = frozenset()
    objects: frozenset = frozenset()
    parts: tuple = ()

    def union(self, other):
        parts = (tuple(a.union(b) for a, b in zip(self.parts, other.parts))
                 if len(self.parts) == len(other.parts) else ())
        return Value(self.strings | other.strings, self.objects | other.objects, parts)


EMPTY = Value()


class Calls(ast.NodeVisitor):
    def __init__(self, path, declarations):
        self.path, self.declarations = path, declarations
        self.env, self.errors = {}, []
        self.function, self.owner = '', ''

    def error(self, node, text):
        self.errors.append(f'{self.path}:{node.lineno}: {text}')

    def forwarded(self):
        return ((str(self.path).endswith('core/engine.py') and self.owner == 'Engine' and self.function == 'call')
                or (str(self.path).endswith('core/jobs.py') and self.function == 'execute'))

    def check(self, node, objects, names):
        if self.forwarded() and (not names or objects == {'*'}):
            return
        if not objects or not names:
            self.error(node, '无法静态解析插件端口或成员名；用字面量或端口类型注解')
        for port in objects:
            for name in names:
                if name not in self.declarations.get(port, {}):
                    self.error(node, f'{port}.{name} 未在对应 Protocol 声明')

    def value(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return Value(strings=frozenset([node.value]))
        if isinstance(node, ast.Name):
            return self.env.get(node.id, EMPTY)
        if isinstance(node, (ast.Tuple, ast.List)):
            # Tuples are records; lists are collections of possible record values.
            if isinstance(node, ast.Tuple):
                return Value(parts=tuple(self.value(v) for v in node.elts))
            result = EMPTY
            for element in node.elts:
                result = self.value(element) if result == EMPTY else result.union(self.value(element))
            return result
        if isinstance(node, ast.ListComp):
            return self.value(node.elt)
        if isinstance(node, ast.IfExp):
            return self.value(node.body).union(self.value(node.orelse))
        if isinstance(node, ast.Attribute) and node.attr == 'plugins':
            return Value(objects=frozenset(['plugins']))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == 'Registry':
                return Value(objects=frozenset(['plugins']))
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and node.args:
                receiver = self.value(node.func.value).objects
                local = (self.owner == 'Registry' and isinstance(node.func.value, ast.Name)
                         and node.func.value.id == 'self')
                if 'plugins' in receiver or local:
                    return Value(objects=self.value(node.args[0]).strings or frozenset(['*']))
        return EMPTY

    def assign(self, target, value):
        if isinstance(target, ast.Name):
            self.env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for i, element in enumerate(target.elts):
                self.assign(element, value.parts[i] if i < len(value.parts) else EMPTY)

    def visit_ClassDef(self, node):
        old = self.owner
        self.owner = node.name
        self.generic_visit(node)
        self.owner = old

    def visit_FunctionDef(self, node):
        old = self.env, self.function
        self.env, self.function = dict(self.env), node.name
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            names = {n.id for n in ast.walk(arg.annotation) if isinstance(n, ast.Name)} if arg.annotation else set()
            kinds = {key for key, protocol in PROTOCOLS.items() if protocol.__name__ in names}
            self.env[arg.arg] = Value(objects=frozenset(kinds))
        for statement in node.body:
            self.visit(statement)
        self.env, self.function = old

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        self.visit(node.value)
        for target in node.targets:
            self.assign(target, self.value(node.value))

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)
            self.assign(node.target, self.value(node.value))

    def visit_For(self, node):
        self.visit(node.iter)
        self.assign(node.target, self.value(node.iter))
        for statement in (*node.body, *node.orelse):
            self.visit(statement)

    def visit_If(self, node):
        self.visit(node.test)
        before = dict(self.env)
        for statement in node.body:
            self.visit(statement)
        left = self.env
        self.env = dict(before)
        for statement in node.orelse:
            self.visit(statement)
        self.env = {key: left.get(key, EMPTY).union(self.env.get(key, EMPTY))
                    for key in left.keys() | self.env.keys()}

    def visit_Attribute(self, node):
        objects = self.value(node.value).objects
        if objects:
            self.check(node, objects, {node.attr})
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'call':
            receiver = ast.unparse(func.value)
            if receiver == 'engine' or receiver == 'self' and self.owner == 'Engine':
                if len(node.args) < 3:
                    self.error(node, '插件调用缺端口/成员参数')
                else:
                    self.check(node, self.value(node.args[0]).strings, self.value(node.args[2]).strings)
        if isinstance(func, ast.Name) and func.id in ('getattr', 'hasattr') and len(node.args) >= 2:
            objects = self.value(node.args[0]).objects
            if objects:
                self.check(node, objects, self.value(node.args[1]).strings)
        if isinstance(func, ast.Attribute) and func.attr == 'append' and isinstance(func.value, ast.Name) and node.args:
            key = func.value.id
            self.env[key] = self.env.get(key, EMPTY).union(self.value(node.args[0]))
        self.generic_visit(node)


def check_calls(root=ROOT, declarations=None):
    declarations = declarations or {key: members(value) for key, value in PROTOCOLS.items()}
    files = [*(root / 'src/workshop/core').rglob('*.py'), root / 'src/workshop/registry.py',
             root / 'src/workshop/cli.py', *(root / 'src/workshop/reports').rglob('*.py')]
    errors = []
    for path in sorted(files):
        visitor = Calls(path.relative_to(root), declarations)
        visitor.visit(ast.parse(path.read_text()))
        errors.extend(visitor.errors)
    return errors


def always_automatic(plugin):
    """只放过字面 return True 的实现；不靠抽样调用证明恒真，也不执行副作用。"""
    method = getattr(plugin, 'automatic', None)
    if method is None:
        return False
    tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return (len(body) == 1 and isinstance(body[0], ast.Return)
            and isinstance(body[0].value, ast.Constant) and body[0].value.value is True)


def missing_members(port, plugin):
    missing = []
    for name, declaration in members(PROTOCOLS[port]).items():
        if declaration and '可选' in (inspect.getdoc(declaration) or ''):
            continue
        if port == 'flow' and name == 'brief' and always_automatic(plugin):
            continue
        value = getattr(plugin, name, None)
        if value is None or declaration is not None and not callable(value):
            missing.append(name)
    return missing


def check_implementations():
    errors = []
    for port in PROTOCOLS:
        for entry in entry_points(group=f'workshop.{port}'):
            try:
                plugin = entry.load()()
                for name in missing_members(port, plugin):
                    errors.append(f'{entry.group}/{entry.name}: 缺少必需成员 {port}.{name}')
            except Exception as exc:
                errors.append(f'{entry.group}/{entry.name}: 不能检查实现：{exc}')
    return errors


def main():
    errors = check_calls() + check_implementations()
    if errors:
        print('\n'.join(errors))
        return 1
    print('端口声明、调用与入口点实现检查通过')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
