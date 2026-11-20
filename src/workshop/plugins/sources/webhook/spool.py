"""本地 spool：入口原子写一份投递，守护进程读完、事实提交后再删。只用文件，不碰事实表。"""
import json
import os
from pathlib import Path
import time
import uuid

INCOMING = ".incoming"
QUARANTINE = "quarantine"


def write(root, envelope):
    """写临时文件 + fsync + rename；目录也 fsync，断电后要么完整在、要么不在。返回成品路径。"""
    root = Path(root)
    (root / INCOMING).mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
    temporary = root / INCOMING / name
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(envelope, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    final = root / name
    os.replace(temporary, final)
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return final


def pending(root):
    root = Path(root)
    return sorted(root.glob("*.json")) if root.exists() else []


def read(path):
    """读一份投递；截断或不是对象的抛 ValueError。"""
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    if (not isinstance(envelope, dict) or not isinstance(envelope.get("event"), str)
            or not isinstance(envelope.get("payload"), dict)):
        raise ValueError("投递缺 event 或 payload")
    return envelope


def quarantine(path):
    target = Path(path).parent / QUARANTINE
    target.mkdir(exist_ok=True)
    os.replace(path, target / Path(path).name)
