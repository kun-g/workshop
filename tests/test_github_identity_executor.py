import json
from pathlib import Path
import sys

import pytest

from test_executors import setup, BRIEF, CONFIG, wait_for
from workshop.plugins.executors.local.storage import read
from workshop.plugins.executors.local import default_profile


@pytest.mark.parametrize('kind', ['orca-claude', 'orca-codex', 'cursor-grok'])
def test_real_runner_injects_app_signer_without_serializing_token(setup, kind):
    root, make, item, target = setup
    signer = root / 'signer.py'
    signer.write_text("import sys\nassert sys.argv[1:] == ['--repo','kun-g/workshop']\nprint('test-installation-token')\n")
    target['repo'] = 'kun-g/workshop'
    target['github_identity'] = {'mode': 'app', 'gh': '/usr/bin/gh', 'signer': [sys.executable, str(signer)]}
    profile = {**default_profile(kind), 'transport': 'orca', 'permission_mode': 'agent'} if kind == 'cursor-grok' else None
    executor = make(kind, profile)
    started = executor.launch('app-round', BRIEF, item, CONFIG, target)
    wait_for(lambda: (root/'agents').exists() and (root/'agents').read_text().endswith('\n'))
    call = json.loads((root/'agents').read_text().splitlines()[0])
    # 执行轮环境里不再有令牌；只有签发器配置，git/gh 每次用时现签。
    assert 'GH_TOKEN' not in call['env'] and 'GITHUB_TOKEN' not in call['env']
    assert json.loads(call['env']['WORKSHOP_GITHUB_SIGNER'])[-1] == str(signer)
    wrapper = Path(call['env']['PATH'].split(':')[0]) / 'gh'
    assert wrapper.exists() and 'test-installation-token' not in wrapper.read_text()
    spec = read(executor.directory('app-round')/'spec.json')
    assert spec['github_identity'] == target['github_identity']
    for file in executor.root.rglob('*'):
        if file.is_file():
            assert b'test-installation-token' not in file.read_bytes()
    executor.stop('app-round', started)


def test_worktree_fetch_uses_target_app_identity(setup):
    root, make, item, target = setup
    item.pop('workdir')
    signer = root / 'signer.py'
    signer.write_text("import sys\nassert sys.argv[1:] == ['--repo','kun-g/workshop']\nprint('fetch-app-token')\n")
    target['repo'] = 'kun-g/workshop'
    target['github_identity'] = {'mode': 'app', 'gh': '/usr/bin/gh', 'signer': [sys.executable, str(signer)]}
    git = root / 'git'
    git.write_text('#!' + sys.executable + '\n' + """
import os, sys
assert os.environ['GH_TOKEN'] == os.environ['GITHUB_TOKEN'] == 'fetch-app-token'
assert os.environ['GIT_CONFIG_VALUE_0'] == ''
os.execv('/usr/bin/git', ['/usr/bin/git', *sys.argv[1:]])
""")
    git.chmod(0o755)
    executor = make('orca-claude')
    executor.commands['git'] = str(git)
    workdir = executor._create_worktree(executor.directory('fetch'), item, target)
    assert workdir == target['checkout']
    for file in executor.root.rglob('*'):
        if file.is_file():
            assert b'fetch-app-token' not in file.read_bytes()
