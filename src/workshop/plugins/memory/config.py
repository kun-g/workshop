"""记忆库设置：值写在唯一规则 `memory:` 一节（核心 R16），装配层解析后落在状态目录里一份快照。

插件不读规则文件：`workshop run` 启动时由命令行把解析好的设置写到
`<状态目录>/memory/settings.json`，流程和命令都从这份快照取；没有快照就用默认值和环境变量。
"""
import json
import os
from pathlib import Path

DEFAULTS = {
    "path": "memory.db",
    # 空字符串表示「状态目录旁的同名备份目录」；它一定在状态目录之外。
    "backup_dir": "",
    "intervention_db": "~/.local/state/workshop-intervention/intervention.db",
    # 到点开出来的活挂到哪个交付对象、走哪个流程；默认车间自己。
    "target": "workshop",
    "flow": "memory",
    "budgets": {"max_entries": 40, "max_chars": 6000, "reported_recent": 10},
    "dream": {"every": "weekly", "weekday": "mon", "at": "04:00", "zone": "Asia/Shanghai"},
    "backup": {"every": "weekly", "weekday": "mon", "at": "05:00", "zone": "Asia/Shanghai"},
}
TASKS = {"dream": ("memory-dream", "记忆：每周做梦"),
         "backup": ("memory-backup", "记忆：每周备份")}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SNAPSHOT = "memory/settings.json"


def state_root(target=None):
    if isinstance(target, dict) and (target.get("runtime") or {}).get("state_dir"):
        return Path(target["runtime"]["state_dir"]).expanduser().resolve()
    return Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")).expanduser().resolve()


def merge(base, given):
    result = dict(base)
    for key, value in (given or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


def written(target=None):
    path = state_root(target) / SNAPSHOT
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve(root, overrides=None):
    """合并默认值、状态目录快照、显式覆盖；校验一遍。"""
    data = merge(DEFAULTS, written({"runtime": {"state_dir": str(root)}}))
    data = merge(data, overrides if isinstance(overrides, dict) else {})
    path = Path(str(data["path"])).expanduser()
    data["path"] = str(path if path.is_absolute() else root / path)
    data["state_dir"] = str(root)
    # 只读事实表是核心状态库；记忆库是另一份文件，两种都按状态目录展开。
    data["facts_db"] = str(root / "workshop.db")
    # 备份必须在状态目录之外：默认放在同一父目录下的 <状态目录名>-memory-backups。
    given = str(data["backup_dir"] or "").strip()
    data["backup_dir"] = str(Path(given).expanduser().resolve() if given
                             else (root.parent / (root.name + "-memory-backups")).resolve())
    data["intervention_db"] = str(Path(str(data["intervention_db"])).expanduser().resolve())
    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        raise ValueError("memory.budgets 必须是对象")
    data["budgets"] = {**DEFAULTS["budgets"], **budgets}
    for key, value in data["budgets"].items():
        if type(value) is not int or value < 0:
            raise ValueError(f"memory.budgets.{key} 必须是非负整数")
    for key in ("dream", "backup"):
        rhythm = data.get(key)
        if not isinstance(rhythm, dict):
            raise ValueError(f"memory.{key} 必须是对象")
        data[key] = {**DEFAULTS[key], **rhythm}
        if data[key]["every"] not in ("daily", "weekly"):
            raise ValueError(f"memory.{key}.every 只支持 daily、weekly")
        if data[key]["every"] == "weekly" and data[key]["weekday"] not in WEEKDAYS:
            raise ValueError(f"memory.{key}.weekday 须是 mon 到 sun")
    if Path(data["backup_dir"]).resolve().is_relative_to(root.resolve()):
        raise ValueError("memory.backup_dir 必须在状态目录之外")
    return data


def settings(target=None):
    """按活的目标（带 runtime.state_dir）或当前状态目录解析设置。"""
    overrides = target.get("memory") if isinstance(target, dict) else None
    return resolve(state_root(target), overrides)


def write_snapshot(root, value):
    """装配层在启动守护进程前调用：把规则里的 memory 一节存成快照。"""
    if not isinstance(value, dict) or not value:
        return None
    path = Path(root).expanduser().resolve() / SNAPSHOT
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = resolve(Path(root).expanduser().resolve(), value)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({k: resolved[k] for k in DEFAULTS}, ensure_ascii=False, indent=1))
    temporary.replace(path)
    return path


def schedules(data):
    """每周做梦、每周备份的 R34 登记参数。"""
    result = []
    for key in ("dream", "backup"):
        name, title = TASKS[key]
        rhythm = data[key]
        result.append({"schedule": name, "title": title, "target": data["target"], "flow": data["flow"],
                       "every": rhythm["every"], "at": rhythm["at"],
                       "weekday": rhythm["weekday"] if rhythm["every"] == "weekly" else None,
                       "zone": rhythm["zone"], "about": f"记忆：{key}"})
    return result
