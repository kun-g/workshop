"""Compare delivery semantics using the same inputs and a local acceptance mailbox."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile

import yaml
from workshop.plugins.intervention.client import request

if sys.argv[1] == 'receive':
    payload = json.load(sys.stdin)
    assert payload['action'] == 'send'
    with Path(sys.argv[2]).open('a') as mailbox:
        mailbox.write(json.dumps(payload, sort_keys=True) + '\n')
    print(json.dumps({'state': 'sent', 'external': 'local:' + payload['key']}))
    sys.exit()

root = Path(tempfile.mkdtemp(dir='/tmp', prefix='ws312-local-'))
endpoint = root / 'run/socket'
endpoint.parent.mkdir(mode=0o700)
mailbox = root / 'mailbox.jsonl'
rules = yaml.safe_load(Path('rules/workshop.yaml').read_text())
rules['intervention']['status']['recipient'] = 'local-test'
rules['intervention']['transport'] = [sys.executable, __file__, 'receive', str(mailbox)]
rulepath = root / 'rules.yaml'
rulepath.write_text(yaml.safe_dump(rules, allow_unicode=True))
read_fd, write_fd = os.pipe()
argv = [sys.executable, '-m', 'workshop.plugins.intervention', '--state-dir', str(root), '--rules', str(rulepath), '--endpoint', str(endpoint), 'serve', '--ready-fd', str(write_fd)]
child = subprocess.Popen(argv, pass_fds=(write_fd,))
os.close(write_fd)
try:
    assert select.select([read_fd], [], [], 10)[0] and os.read(read_fd, 6) == b'ready\n'
    def call(action, **data):
        return request(endpoint, action, data)
    for version in ('v1', 'v2'):
        owner = 'acceptance:' + version
        permit = call('permit', operation='permit:' + version, owner=owner, point='visual', scope='contract:' + version,
                      basis=dict(contract='local-only', location='local-mailbox', response='local-session'))['ref']
        ref = call('enqueue', operation='enqueue:' + version, permit=permit, payload=dict(text='local receipt check', version=version))['ref']
        assert call('dispatch', ref=ref)['state'] == 'sent'
        assert call('dispatch', ref=ref)['state'] == 'sent'
        if version == 'v1':
            wait = call('create', operation='wait:' + version, owner=owner, point='visual', content=dict(version=version, delivery=ref))['ref']
            call('answer', operation='answer:' + version, ref=wait, value='local acceptance', source=dict(channel='session', session='local-acceptance', quote='local acceptance', version=version))
    print('commit=' + sys.argv[1], 'dispatch=4', 'local_mailbox_messages=' + str(len(mailbox.read_text().splitlines())))
    print('state_dir=' + str(root))
finally:
    os.close(read_fd)
    child.terminate()
    child.wait(timeout=10)
