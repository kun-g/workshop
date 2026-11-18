"""装配边界：入口点 group=workshop.<端口>，name=规则里的 use/name。

入口点加载无参数工厂，返回实现端口的对象；每个 Registry 缓存一个实例。
测试与嵌入方可 register；核心只依赖 Plugins 端口，不依赖本模块。
"""
from importlib.metadata import entry_points


class Registry:
    def __init__(self, profiles=None):
        self.instances = {}
        self.profiles = profiles or {}

    def configure(self, rules):
        """Validate every driver before any stage can start a session."""
        from .core.rules import Rules
        from .github_identity import configuration
        from .core.flows import entries
        targets = {}
        for name, target in rules.data['targets'].items():
            if 'flows' not in target:
                try:
                    adapt = getattr(self.get('flow', target['plugin']), 'configure_target', None)
                except LookupError:
                    adapt = None  # 查询和恢复命令允许暂时没有装上该流程。
                target = adapt(target) if adapt else target
            targets[name] = target
        rules = Rules({**rules.data, 'targets': targets}, rules.path)
        for name, config in rules.data.get('wait_services', {}).items():
            validate = getattr(self.get('wait', name), 'validate', None)
            if validate:
                try:
                    validate(config)
                except ValueError as exc:
                    raise ValueError(f'wait_services.{name}: {exc}') from exc
        for name, target in targets.items():
            for flow in entries(target):
                configuration(rules.target(name, flow))
                configuration(rules.target(name, flow), relations_read=True)
        profiles = {}
        for name, profile in rules.data.get('executor_profiles', {}).items():
            try:
                driver = self.get('driver', profile['driver'])
            except (LookupError, ImportError) as exc:
                raise ValueError(f"配置档 {name} 的 driver：{exc}") from exc
            driver.validate(name, profile)
            profiles[name] = {**profile, 'family': driver.family}
        self.profiles = profiles
        if profiles:
            return Rules({**rules.data, 'executor_profiles': profiles}, rules.path)
        return rules

    def register(self, port: str, name: str, instance: object) -> None:
        self.instances[port, name] = instance

    def get(self, port: str, name: str) -> object:
        key = port, name
        if key not in self.instances and port == 'executor' and name in self.profiles:
            config = self.profiles[name]
            self.instances[key] = self.get('driver', config['driver']).executor(name, config)
        if key not in self.instances:
            entries = list(entry_points(group=f"workshop.{port}", name=name))
            if len(entries) != 1:
                raise LookupError(f"插件 {port}/{name} 必须恰好注册一次")
            self.instances[key] = entries[0].load()()
        return self.instances[key]


def load_rules(path):
    """Public assembly entry point: generic rule shape plus driver capabilities."""
    from .core.rules import Rules
    registry = Registry()
    return registry.configure(Rules.load(path))
