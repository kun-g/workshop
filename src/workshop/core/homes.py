"""工单目录。"""
from .jobs import Pending


def taskhome(engine, item):
    name = engine.rules.data.get("taskhome")
    if not name:
        return True
    try:
        engine.plugins.get("taskhome", name)
        target = engine.item_target(item)
        if not item.get("ticket"):
            key = f'ticket:{item["id"]}'
            ticket = engine.effect(key, lambda: engine.call("taskhome", name, "open", item["id"], item, target, identity=[key]))
            engine.change(item, ticket=ticket)
        else:
            key = f'ticket:{item["id"]}:{item["revision"]}'
            engine.effect(key, lambda: engine.call("taskhome", name, "update", key, item["ticket"], item, target, identity=[key]))
        return True
    except Pending:
        return False
    except Exception as exc:
        engine.broken(item["target"], "taskhome", name, exc)
        return False

