"""叫醒之后让对应查询过期，并算出下一拍要等多久。"""
from datetime import datetime
from pathlib import Path
from inspect import signature


def wake_paths(engine):
    """可选端口只返回本地路径；路径变动只使声明者的查询结果过期。"""
    paths = {}
    channel = engine.rules.data["channel"]
    for target, rule in engine.rules.data["targets"].items():
        entries = [("source", name, (target,)) for name in rule.get("sources", [])]
        entries.append(("channel", channel, ("poll",)))
        for port, name, identity in entries:
            method = getattr(engine.plugins.get(port, name), "wake_paths", None)
            if method:
                declared = method(engine.rules.target(target)) if signature(method).parameters else method()
                for path in declared:
                    path = Path(path).expanduser().absolute()
                    paths.setdefault(path, set()).add((port, name, identity))
    return paths


def awaken(engine, wake, paths):
    if engine.jobs is None:
        return
    affected = set()
    for path in wake.paths:
        affected.update(paths.get(path, ()))
    for message in wake.messages:
        parts = message.split(":", 2)
        if len(parts) == 3 and parts[0] == "source":
            _, target, name = parts
            if name in engine.rules.data["targets"].get(target, {}).get("sources", []):
                affected.add(("source", name, (target,)))
    for port, name, identity in affected:
        engine.jobs.invalidate(port, name, list(identity))


def wait_timeout(engine):
    """等到最近一个尚需处理的截止时间，最长 60 秒写一次心跳。"""
    now = engine.clock()
    start = getattr(engine, "tick_started", now)
    waits = [60]
    def include(value, *, overdue=False):
        if value:
            at = datetime.fromisoformat(value)
            if overdue or at > start:
                waits.append(max(0, (at - now).total_seconds()))
    for timer in engine.log.all("timers", unless=("fired", True)):
        if not timer["fired"] and not engine.control(timer["target"]).get("broken"):
            include(timer["at"], overdue=True)
    for control in engine.log.all("controls"):
        if control["id"].startswith("retry:"):
            include(control.get("at"))
    for row in engine.active():
        include(row.get("deadline"))
    for item in engine.log.open_items() + engine.log.release_items():
        include(item.get("retry_at"))
        include(item.get("release_at"))
    if engine.jobs is not None:
        waits.append(engine.jobs.wait_timeout())
    return min(waits)

