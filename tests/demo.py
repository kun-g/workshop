"""生成用于命令行验收的状态；只用假插件，无网络/Orca/消息动作。"""
from pathlib import Path
import sys

from conftest import build, reach, finish


def main():
    path = Path(sys.argv[1])
    if (path / "workshop.db").exists():
        raise SystemExit("验收目录已有数据库，请选空目录")
    engine = build(path)
    reach(engine, "收尾")
    finish(engine, leave={"kind": "工序", "owner": "车间", "at": "+1d", "about": "观察结果"},
           notice=[{"key": "accepted", "text": "验收通过，不用回"}])
    engine.log.close()
    print(f"验收状态：{path}；活 alpha:one，8 轮走到收尾")


if __name__ == "__main__":
    main()
