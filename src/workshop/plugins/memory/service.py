"""记忆服务：命令背后的业务操作，和核心记忆端口共用一个 store。

不 import 介入点插件或其他插件；送达状态经只读发件端口，事实经只读事实端口。
文件都按路径现开；CLI 在独立进程里直接调用，守护进程的派活说明经 `port.Memory` 取这一节。
"""
from datetime import datetime, timezone
from pathlib import Path

from . import backup as backuping
from . import config
from . import context as loading
from . import dream as dreaming
from . import ports, reported
from .store import open_store


def settings(target=None):
    return config.resolve(_root(target), _overrides(target))


def _root(target):
    if isinstance(target, dict) and (target.get("runtime") or {}).get("state_dir"):
        return Path(target["runtime"]["state_dir"]).expanduser().resolve()
    return config.state_root(target)


def _overrides(target):
    return target.get("memory") if isinstance(target, dict) else None


def store_of(target=None):
    return open_store(settings(target)["path"])


def _now():
    return datetime.now(timezone.utc).isoformat()


class MemoryService:
    def add(self, *, kind, scope, claim, summary, source_fact_seqs, item=None, state=None, token=None,
            target=None):
        data = settings(target)
        if not token:
            raise ValueError("写入记忆必须带 --token（本轮轮次令牌）")
        status = ports.round_status(data["facts_db"], token)
        if status not in ("starting", "running"):
            raise ValueError(f"轮次令牌无效、不属于这一轮，或这一轮已结束：{token}（状态 {status or '不存在'}）")
        if not source_fact_seqs:
            raise ValueError("写入记忆必须带真实的来源事实 source_fact_seqs")
        facts = ports.Facts(data["facts_db"])
        missing = [seq for seq, found in facts.exists(source_fact_seqs).items() if not found]
        if missing:
            raise ValueError(f"来源事实不存在：{missing}")
        item_id = item or (ports.round_record(data["facts_db"], token) or {}).get("item")
        store = open_store(data["path"])
        try:
            return store.append(kind=kind, scope=scope, claim=claim, summary=summary or "",
                                source_fact_seqs=source_fact_seqs, batch=f"round:{token}",
                                item=item_id, state=state)
        finally:
            store.close()

    def show(self, entry_id, target=None):
        store = store_of(target)
        try:
            row = store.entry(entry_id)
        finally:
            store.close()
        if row is None:
            raise ValueError(f"没有这条记忆：{entry_id}")
        return row

    def entries(self, target=None, kind=None, scope=None, include_corrected=False):
        store = store_of(target)
        try:
            rows = store.effective(kind=kind, scope=scope)
            hidden = set() if include_corrected else set(store.corrected())
            return [row for row in rows if row["id"] not in hidden]
        finally:
            store.close()

    def correct(self, entry_id, phrase, watermark=None, target=None):
        store = store_of(target)
        try:
            if watermark is None:
                watermark = ports.Facts(settings(target)["facts_db"]).max_seq()
            return store.append_correction(entry_id, phrase, watermark)
        finally:
            store.close()

    def dream(self, target=None):
        data = settings(target)
        store = open_store(data["path"])
        try:
            return dreaming.run(store, ports.Facts(data["facts_db"]), _dreamer(data), batch=f"dream:{_now()}")
        finally:
            store.close()

    def backup(self, target=None):
        data = settings(target)
        path = backuping.run(data["path"], data["backup_dir"])
        return {"result": "ok", "database": data["path"], "backup": str(path),
                "backup_dir": data["backup_dir"]}

    def restore(self, target=None, backup=None):
        data = settings(target)
        path = backuping.restore_backup(backup, data["path"]) if backup else backuping.restore(data["backup_dir"], data["path"])
        return {"result": "ok", "database": str(path)}

    def record_reported(self, target=None, scope="*"):
        data = settings(target)
        store = open_store(data["path"])
        try:
            return reported.record(store, ports.Outbox(data["intervention_db"]), scope=scope,
                                    batch=f"reported:{_now()}", watermark=ports.Facts(data["facts_db"]).max_seq())
        finally:
            store.close()


def _dreamer(settings):
    from .defaults import make_dreamer
    return make_dreamer(None, settings)


def schedule_options(state_dir, rules_data):
    """每周做梦、每周备份的 R34 登记参数（数据）；登记由命令行用核心的周期任务做。"""
    given = (rules_data or {}).get("memory")
    if not given:
        return []
    data = config.resolve(Path(state_dir).expanduser().resolve(), given)
    config.write_snapshot(Path(state_dir).expanduser().resolve(), given)
    return config.schedules(data)
