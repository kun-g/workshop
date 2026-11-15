"""Common driver helpers; no provider switches or policy defaults."""
import json
from pathlib import Path

from workshop.core.models import RoundUsage, Usage

COMMON = {'use', 'driver', 'transport', 'family', 'billing', 'quota', 'first_when_writer', 'model', 'effort'}


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value):
    return isinstance(value, list) and all(text(v) for v in value)


def mapping(value):
    return isinstance(value, dict) and all(isinstance(k, str) for k in value)


def options(value):
    return text(value) or mapping(value)


def json_options(value):
    if isinstance(value, dict):
        return value
    path = Path(value).expanduser()
    return json.loads(path.read_text())


def instructions(config):
    parts = [config['prompt']] if config.get('prompt') else []
    for name in config.get('skills', []):
        path = Path(name).expanduser().resolve()
        if path.is_dir():
            path /= 'SKILL.md'
        parts.append(f'Skill: {path}\nRelative resources are relative to {path.parent}.\n{path.read_text()}')
    return '\n\n'.join(parts)


class Driver:
    fields = {}
    family = ''
    binary = ''
    allocated_session = False
    stdin_arg = False

    def validate(self, name, config):
        def fail(field, reason):
            raise ValueError(f'配置档 {name} 的 {field}：{reason}')
        for key, value in config.items():
            if key not in COMMON and key not in self.fields:
                fail(key, '驱动不支持或字段拼错')
            if key in self.fields and not self.fields[key](value):
                fail(key, '类型或取值无效')
        for key in ('driver', 'transport', 'billing'):
            if not text(config.get(key)):
                fail(key, '必须写明')
        if config['transport'] not in ('orca', 'headless'):
            fail('transport', '只能是 orca 或 headless')
        if config['billing'] not in ('subscription', 'metered'):
            fail('billing', '只能是 subscription 或 metered')
        if config.get('family', self.family) != self.family:
            fail('family', f'由驱动确定为 {self.family}，不能将同一家伪装成不同家')
        for key in ('model', 'effort'):
            if key in config and not text(config[key]):
                fail(key, '必须是非空字符串')
        if 'first_when_writer' in config and not strings(config['first_when_writer']):
            fail('first_when_writer', '必须是字符串列表')
        if 'quota' in config:
            quota = config['quota']
            if not mapping(quota):
                fail('quota', '必须是对象')
            for key in quota:
                if key not in ('provider', 'window', 'min_remaining', 'label'):
                    fail('quota.' + key, '字段拼错')
            for key in ('provider', 'window'):
                if not text(quota.get(key)):
                    fail('quota.' + key, '必须写明')
            if 'min_remaining' in quota and (type(quota['min_remaining']) not in (int, float) or not 0 <= quota['min_remaining'] <= 100):
                fail('quota.min_remaining', '必须在 0 到 100 之间')

    def executor(self, name, config):
        from ..adapters import Executor
        return Executor(name, profile=config, driver=self)

    def model(self, config):
        return config.get('model')

    def environment(self, config):
        return {}

    def available(self, executor, config):
        return True

    def preflight(self, executor, config):
        pass

    def session_from_output(self, output):
        return None

    def missing_session(self, spec, output, code):
        """Whether a failed resume proves the stored session cannot be used."""
        return False

    def recover_session(self, executor, directory, spec):
        found = self.session_from_output(directory / 'output.log')
        if found:
            from ..storage import write
            write(Path(spec['session_file']), {'vendor_id': found, 'model': spec.get('model')})
        return found

    def prepare(self, spec, env, directory):
        pass

    def initial_argv(self, spec, directory, prompt, external):
        return [*spec['argv'], '--', prompt] if spec['interactive'] else [*spec['argv'], *(['-'] if self.stdin_arg else [])]

    def hook_session(self, data, spec):
        return None

    def final_message(self, output, start=0):
        if not output.exists():
            return None
        with output.open('rb') as stream:
            stream.seek(start)
            content = stream.read().decode(errors='replace')
        final = None
        for line in content.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('type') == 'result' and isinstance(row.get('result'), str):
                final = row['result']
        return final if final is not None else content

    def usage(self, executor, directory, spec, since):
        return RoundUsage(main=Usage(billing=spec['billing']))

    def collect(self, home, session, workdir, since, billing, config_dir=None):
        return RoundUsage(main=Usage(billing=billing))
