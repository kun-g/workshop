"""每周备份：成功后留一份独立文件，恢复时整体放回；路径在状态目录之外。

备份文件按 UTC 时间命名并留 `latest` 指针；备份前的库完整性检查不通过就不换指针。
"""
from pathlib import Path

from .store import online_backup, restore_backup


def run(database, root):
    from datetime import datetime, timezone
    root = Path(root).expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = root / f"memory-{stamp}.db"
    online_backup(database, destination)
    latest = root / "latest"
    temporary = root / "latest.tmp"
    temporary.write_text(destination.name)
    temporary.replace(latest)
    return destination


def restore(root, database):
    root = Path(root).expanduser().resolve()
    latest = root / "latest"
    if not latest.is_file():
        raise ValueError(f"没有备份指针：{latest}")
    return restore_backup(root / latest.read_text().strip(), database)
