import argparse
import json
import os
from pathlib import Path
import sys
from . import Watch


def main(argv=None):
    parser = argparse.ArgumentParser(description="独立检查车间成功心跳和未结算轮")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("WORKSHOP_STATE_DIR", "~/.local/state/workshop")))
    parser.add_argument("--ask-kun", help="ask-kun 可执行文件")
    parser.add_argument("--attention", type=Path, help="attention.py 路径")
    args = parser.parse_args(argv)
    try:
        state = Watch(args.state_dir, ask_kun=args.ask_kun, attention=args.attention).run()
    except Exception as exc:
        print(f"车间看门失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
