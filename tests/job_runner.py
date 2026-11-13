"""作业测试的作业进程入口：装上文件替身后照 workshop job 一样跑一个作业。

argv：状态目录、作业目录（作业目录由 Jobs 接在命令最后）。"""
from pathlib import Path
import sys

from workshop.core.engine import finder
from workshop.core.jobs import execute
from workshop.core.log import Log

import job_plugins

log = Log(Path(sys.argv[1]) / "workshop.db")
execute(Path(sys.argv[2]), job_plugins.registry(), lambda target: finder(log, target))
