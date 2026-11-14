import json
import sys

import pytest

from test_executors import setup, BRIEF, CONFIG, wait_for
from workshop.plugins.executors.local.storage import read
from workshop.plugins.executors.local import default_profile


@pytest.mark.parametrize('kind', ['orca-claude', 'orca-codex', 'cursor-grok'])
def test_real_runner_injects_app_without_serializing_token(setup, kind):
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
    assert call['env']['GH_TOKEN'] == call['env']['GITHUB_TOKEN'] == 'test-installation-token'
    spec = read(executor.directory('app-round')/'spec.json')
    assert spec['github_identity'] == target['github_identity']
    for file in executor.root.rglob('*'):
        if file.is_file():
            assert b'test-installation-token' not in file.read_bytes()
    executor.stop('app-round', started)
