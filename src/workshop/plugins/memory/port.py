"""核心记忆端口（`Memory`）的实现；经入口点 `workshop.memory` 注册。

只读加载本轮开头该带的固定部分。设置来自规则 `memory:` 一节（随 config 传入），
按活的目标解析后定位 memory.db；作业进程继承守护进程的 `WORKSHOP_STATE_DIR`。
"""
from . import config
from . import context as loading
from .store import open_store
from pathlib import Path


class Memory:
    def context(self, scope, item, config_value):
        if not isinstance(config_value, dict):
            raise ValueError("记忆配置必须是对象")
        root = Path(config_value.get("state_dir")).expanduser().resolve() if config_value.get("state_dir") else config.state_root(None)
        # 规则里的值随 config 传进来；去掉本模块不认的字段，其余照常用作覆盖。
        overrides = {k: v for k, v in config_value.items() if k not in ("plugin", "state_dir")}
        data = config.resolve(root, overrides)
        store = open_store(data["path"])
        try:
            loaded = loading.context(store, scope, data)
        finally:
            store.close()
        return loading.brief_section(loaded)


def make_memory():
    return Memory()
