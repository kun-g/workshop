"""收回工作目录，并在这一进程里清一次流程留下的目录。"""
from datetime import timedelta
from .models import stamp
from .ports import Retryable
from .models import Release
from .jobs import Pending
from .flows import entries


def cleanup_flows(engine):
    keep = [r.get("workdir") for r in engine.active() if not r.get("stopped")]
    for name, rule in engine.rules.data['targets'].items():
        for flow in entries(rule):
            target = engine.rules.target(name, flow)
            key = f'cleanup:{name}:{flow}'
            if engine.flow_broken(name, flow) or not engine.retry_due(key):
                continue
            try:
                if getattr(engine.plugins.get('flow', target['plugin']), 'cleanup', None):
                    engine.call('flow', target['plugin'], 'cleanup', target, keep)
                engine.retry_clear(key)
            except Pending:
                pass
            except Retryable as exc:
                engine.retry_failure(name, 'flow', target['plugin'], key, exc)
            except Exception as exc:
                engine.broken(name, 'flow', target['plugin'], exc)


def release_workdirs(engine):
    """收尾完成的活：流程说不用留了，就请为它开过工作目录的执行者收回；返工时执行者重新开。

    流程说还要留（例如还有没关的提交请求），约一小时后再问；执行者说不能收（有没交出去的改动），
    通知 Kun 一次，不再重试。没有任何执行者认领的目录不动。
    """
    running = {r["item"] for r in engine.active()}
    now = stamp(engine.clock())
    for item in engine.log.release_items():
        if (item["state"] not in ("完成", "放弃") or not item.get("workdir") or item.get("workdir_released")
                or item["id"] in running or (item.get("release_at") or "") > now):
            continue
        control = engine.control(item["target"])
        if control.get("paused") or engine.flow_broken(item["target"], item.get("flow", "")) or item["target"] not in engine.rules.data["targets"]:
            continue
        target = engine.item_target(item)
        later = stamp(engine.clock() + timedelta(hours=1))
        # 同一次收回跨拍、跨重启复用结果；延期重试和返工后的收尾必须重新判断。
        identity = [item["id"], item.get("pending_episode", 0), item["workdir"], item.get("release_at")]
        try:
            hold = getattr(engine.plugins.get("flow", target["plugin"]), "hold_workdir", None)
            reason = engine.call("flow", target["plugin"], "hold_workdir", item, target,
                               identity=identity, item=item["id"]) if hold else ""
            if not isinstance(reason, str):
                raise ValueError("流程留工作目录的原因必须是字符串")
        except Pending:
            continue
        except Retryable:
            engine.change(item, release_at=later)
            continue
        except Exception as exc:
            engine.broken(item["target"], "flow", target["plugin"], exc)
            continue
        if reason:
            engine.change(item, release_at=later, release_hold=reason)
            continue
        outcome = "无执行者认领"
        for name in sorted(item.get("sessions") or {}):
            try:
                release = getattr(engine.plugins.get("executor", name), "release", None)
                result = engine.call("executor", name, "release", item, target,
                                   identity=identity, item=item["id"]) if release else None
                if result is not None and (not isinstance(result, Release) or not result.released and not result.reason):
                    raise ValueError("执行者收回结果无效；留着时必须写原因")
            except Pending:
                outcome = None
                break
            except Retryable:
                outcome = None
                engine.change(item, release_at=later)
                break
            except Exception as exc:
                outcome = None
                engine.broken(item["target"], "executor", name, exc)
                break
            if result is None:
                continue
            if result.released:
                outcome = "已收回"
                engine.change(item, workdir=None, workdir_released=outcome, release_hold="", release_at=None)
            else:
                outcome = "留着：" + result.reason
                # 没收回的原因记在活上，status 和 why 里看；不发通知（介入点 R13）。
                engine.change(item, workdir_released=outcome, release_hold="", release_at=None)
            break
        if outcome == "无执行者认领":
            engine.change(item, workdir_released=outcome, release_hold="", release_at=None)

