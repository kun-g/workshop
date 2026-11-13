"""UserPromptSubmit observes steering; never blocks or dispatches work."""
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
        prompt = data.get('prompt', '')
        # Compare the actual initial prompt, including on resume; never drop the first human message.
        if prompt == Path(spec['prompt']).read_text():
            return 0
        stable = data.get('uuid') or data.get('prompt_id') or str(uuid.uuid4())
        event = {'source': 'orca', 'ext_id': spec['token'] + ':' + hashlib.sha256(stable.encode()).hexdigest(),
                 'target': spec['item']['target'], 'object_key': spec['item']['object_key'],
                 'title': 'Orca 插话', 'body': {'item_id': item, 'text': prompt}}
        subprocess.run(spec['receipt_command'] + ['event', '--json', json.dumps(event, ensure_ascii=False)],
                       capture_output=True, timeout=10, check=False)
    except Exception as exc:
        # Claude must receive the user's original prompt even when the journal is unavailable.
        print('车间插话记录失败: ' + str(exc), file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
