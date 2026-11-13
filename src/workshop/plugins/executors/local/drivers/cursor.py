"""Cursor Agent protocol; supported profile options are deliberately small."""
import json
import re
import time
from .base import Driver as Base
from ..storage import read


class Driver(Base):
    family = 'grok'
    binary = 'cursor-agent'
    allocated_session = True
    fields = {'permission_mode': lambda v: v in ('plan', 'ask', 'agent')}

    def model(self, config):
        model = config.get('model') or 'grok-4.7-high'
        if config.get('effort'):
            model = (model + '[' if '[' not in model else model.rstrip(']') + ',') + 'effort=' + config['effort'] + ']'
        return model

    def argv(self, executor, config, workdir, session, resume, directory, trust_workdir=None):
        argv = [executor.commands[self.binary]]
        if not executor.interactive:
            argv += ['-p']
        if config.get('permission_mode') in ('plan', 'ask'):
            argv += ['--mode', config['permission_mode']]
        argv += ['--trust', '--model', self.model(config), '--workspace', workdir]
        if not executor.interactive:
            argv += ['--output-format', 'stream-json']
        return argv + (['--resume', session] if resume else [])

    def available(self, executor, config):
        for path in (executor.root / 'rounds').glob('*/output.log'):
            ended = path.parent / 'exited.json'
            if ended.exists() and read(ended).get('code') not in (None, 0) and time.time() - path.stat().st_mtime < 300 and re.search(
                    r'(usage limit reached|quota exceeded|insufficient credits|no usage remaining)', path.read_text(errors='replace')[-8000:], re.I):
                return False
        return True

    def session_from_output(self, output):
        if output.exists():
            for line in output.read_text(errors='replace').splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get('session_id'):
                    return row['session_id']
        return None
