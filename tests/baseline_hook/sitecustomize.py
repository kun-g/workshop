"""录基线时挂上。只有被标过的子进程才装记录器。

pytest 自己的进程由 conftest 装插件。录制代码在拉起子进程时设置
WORKSHOP_BASELINE_CHILD，这里认这个变量，不看命令行。
"""
import os
import sys
from pathlib import Path

if os.environ.get("WORKSHOP_RECORD_BASELINE") and os.environ.get("WORKSHOP_BASELINE_CHILD"):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import baseline_plugin
    except ModuleNotFoundError:
        pass
    else:
        baseline_plugin.install()
        baseline_plugin.enable_child_trace()
