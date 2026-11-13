"""UserPromptSubmit observes steering; SessionStart records the Codex session id. Never blocks or dispatches work."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    item = os.environ.get('WORKSHOP_ITEM')
    if not item:
        return 0
    try:
        data = json.load(sys.stdin)
        directory = Path(os.environ['WORKSHOP_EXECUTOR_ROUND'])
        spec = json.loads((directory / 'spec.json').read_text())
        if data.get('hook_event_name') == 'SessionStart':
            # Codex 不接受预先指定的会话号；开会话时记下它，下一轮按它接回、按它记用量。
            if spec['kind'] == 'orca-codex' and data.get('session_id'):
                path = Path(spec['session_file'])
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + '.tmp')
                temporary.write_text(json.dumps({'vendor_id': data['session_id'], 'model': spec.get('model')}))
                os.replace(temporary, path)
            return 0
        prompt = data.get('prompt', '')
        # Compare the actual initial prompt, including on resume; never drop the first human message.
        initial = (directory / 'initial-prompt.txt')
        if prompt == Path(spec['prompt']).read_text() or (initial.exists() and prompt == initial.read_text()):
            return 0
        stable = data.get('uuid') or data.get('prompt_id') or data.get('turn_id') or str(uuid.uuid4())
        event = {'source': 'orca', 'ext_id': spec['token'] + ':' + hashlib.sha256(stable.encode()).hexdigest(),
                 'target': spec['item']['target'], 'object_key': spec['item']['object_key'],
                 'title': 'Orca 插话', 'body': {'item_id': item, 'text': prompt}}
        env = dict(os.environ)
        env.pop('WORKSHOP_ROUND_TOKEN', None)
        subprocess.run(spec['receipt_command'] + ['event', '--json', json.dumps(event, ensure_ascii=False)],
                       capture_output=True, timeout=10, check=False, env=env)
    except Exception as exc:
        # Claude must receive the user's original prompt even when the journal is unavailable.
        print('车间插话记录失败: ' + str(exc), file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
