"""流程的真实审查副本交给执行者；只起假命令进程，不起真实模型或 Orca。"""
import json
from pathlib import Path

import pytest

from workshop.core.models import Brief
from test_executors import setup, wait_for, CONFIG
from test_issue_delivery import world, snapshot


@pytest.mark.parametrize('kind', ['codex', 'orca-claude'])
def test_review_process_uses_flow_copy_without_ambient_credentials(setup, world, kind):
    root, make, _, executor_target = setup
    item = snapshot(world, '审查')
    item['workdir'] = world['target']['checkout']
    flow_brief = world['flow'].brief(item, '审查', world['target'])
    brief = Brief(flow_brief.instructions, [], workdir=flow_brief.workdir, readonly=flow_brief.readonly)
    target = {**world['target'], 'runtime': executor_target['runtime']}
    executor = make(kind)
    started = executor.launch('review-cross-block', brief, item, CONFIG, target)
    try:
        wait_for(lambda: (root / 'agents').exists() and (root / 'agents').read_text().endswith('\n'))
        child = json.loads((root / 'agents').read_text().splitlines()[0])
        assert child['cwd'] == brief.workdir == flow_brief.workdir
        assert Path(child['cwd']).is_relative_to(world['io'].root(target) / 'reviews')
        assert Path(child['cwd']).stat().st_mode & 0o222 == 0
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK', 'AWS_SECRET_ACCESS_KEY'):
            assert name not in child['env']
        assert child['env']['GIT_CONFIG_VALUE_0'] == ''
        assert child['env']['KUBECONFIG'] == '/dev/null'
        assert started.workdir == item['workdir']  # 审查目录不能覆盖活的写代码目录。
    finally:
        executor.stop('review-cross-block', started)
        world['flow'].cleanup(target, [])
