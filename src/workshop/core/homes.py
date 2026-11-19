"""工单目录。"""
from .jobs import Pending
from .ports import Retryable


def taskhome(engine, item):
    name = engine.rules.data.get("taskhome")
    if not name:
        return True
    retry_key = f'taskhome:{item["id"]}'
    if not engine.retry_due(retry_key):
        return False
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
        engine.retry_clear(retry_key)
        return True
    except Pending:
        return False
    except Retryable as exc:
        engine.retry_failure(item["target"], "taskhome", name, retry_key, exc)
        return False
    except Exception as exc:
        engine.broken(item["target"], "taskhome", name, exc)
        return False

