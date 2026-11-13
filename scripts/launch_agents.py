#!/usr/bin/env python3
"""生成车间 LaunchAgent；工作区安装器负责安装，不在这里调用 launchctl。"""
import argparse
import json
from pathlib import Path
import plistlib
import sys

SPECS = {"com.kun.workshop": {"keep_alive": True},
         "com.kun.workshop-watch": {"interval_seconds": 300, "run_at_load": True}}


def payload(label, spec, *, home=None, repository=None, state_dir=None):
    if label not in SPECS or spec != SPECS[label]:
        raise ValueError("invalid workshop LaunchAgent spec")
    home = Path(home or Path.home()).expanduser().resolve()
    repository = Path(repository or home / "workspace/runtime/repos/workshop").expanduser().resolve()
    state = Path(state_dir or home / ".local/state/workshop").expanduser().resolve()
    python = repository / ".venv/bin/python"
    daemon = label == "com.kun.workshop"
    args = ([str(python), "-m", "workshop.cli", "--state-dir", str(state),
             "--rules", str(repository / "rules/workshop.yaml"), "run"] if daemon else
            [str(python), "-m", "workshop.watch", "--state-dir", str(state)])
    # launchd 在 exec 前打开日志文件，安装器须先 mkdir 状态目录。
    config = {"Label": label, "ProgramArguments": args, "WorkingDirectory": str(repository),
              "RunAtLoad": True, "StandardOutPath": str(state / (label + ".stdout.log")),
              "StandardErrorPath": str(state / (label + ".stderr.log")),
              "EnvironmentVariables": {"HOME": str(home), "WORKSHOP_STATE_DIR": str(state),
                  "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "PYTHONUNBUFFERED": "1"}}
    config.update({"KeepAlive": True, "ThrottleInterval": 10} if daemon else {"StartInterval": 300})
    return plistlib.dumps(config)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("label", choices=SPECS)
    parser.add_argument("--spec", help="安装器传入的 manifest JSON，按白名单验证")
    parser.add_argument("--home", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    spec = json.loads(args.spec) if args.spec else SPECS[args.label]
    sys.stdout.buffer.write(payload(args.label, spec, home=args.home, repository=args.repository, state_dir=args.state_dir))


if __name__ == "__main__":
    main()
