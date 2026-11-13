"""装配边界：入口点 group=workshop.<端口>，name=规则里的 use/name。

入口点加载无参数工厂，返回实现端口的对象；每个 Registry 缓存一个实例。
测试与嵌入方可 register；核心只依赖 Plugins 端口，不依赖本模块。
"""
from importlib.metadata import entry_points


class Registry:
    def __init__(self):
        self.instances = {}

    def register(self, port: str, name: str, instance: object) -> None:
        self.instances[port, name] = instance

    def get(self, port: str, name: str) -> object:
        key = port, name
        if key not in self.instances:
            entries = list(entry_points(group=f"workshop.{port}", name=name))
            if len(entries) != 1:
                raise LookupError(f"插件 {port}/{name} 必须恰好注册一次")
            self.instances[key] = entries[0].load()()
        return self.instances[key]
