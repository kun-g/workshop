"""Codex command, session hooks and rollout accounting."""
from datetime import datetime
import json
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

from workshop.core.models import RoundUsage, Usage
from workshop.core.ports import Unavailable
from ..storage import read, write
from ..usage import after, number, aggregate, grouped, rows
from ..pricing import cost
from .base import Driver as Base, text, strings, options, mapping, instructions


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k) + '=' + toml_value(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ','.join(toml_value(v) for v in value) + ']'
    if type(value) in (str, bool, int, float):
        return json.dumps(value, ensure_ascii=False)
    raise ValueError('Codex settings 只接受 TOML 字符串、布尔、数字、列表和对象')


def settings(value):
    if isinstance(value, dict):
        return value
    path = Path(value).expanduser()
    return json.loads(path.read_text()) if path.suffix == '.json' else tomllib.loads(path.read_text())


class Driver(Base):
    family = 'codex'
    binary = 'codex'
    allocated_session = True
    stdin_arg = True
    fields = {
        'prompt': text, 'skills': strings, 'settings': options, 'config_dir': text,
        'mcp_config': options, 'plugins': mapping,
        'tools': lambda v: mapping(v) and all(k in ('shell', 'web_search') and
                (type(x) is bool if k == 'shell' else x in ('disabled', 'cached', 'live')) for k, x in v.items()),
        'sandbox': lambda v: v in ('read-only', 'workspace-write', 'danger-full-access'),
        'permission_mode': lambda v: v in ('untrusted', 'on-request', 'never'),
        'model_provider': text, 'ignore_user_config': lambda v: type(v) is bool,
        'ignore_rules': lambda v: type(v) is bool,
    }

    def environment(self, config):
        return {'CODEX_HOME': str(Path(config['config_dir']).expanduser())} if 'config_dir' in config else {}

    def preflight(self, executor, config):
        if executor.interactive:
            missing = [key for key in ('model', 'effort') if not text(config.get(key))]
            if missing:
                raise ValueError(f'配置档 {executor.kind} 缺 {"、".join(missing)}，拒绝启动')
        if executor.interactive and config.get('billing') == 'subscription':
            import os
            env = dict(os.environ)
            env.update(self.environment(config))
            try:
                result = subprocess.run([executor.commands[self.binary], 'login', 'status'], env=env,
                                        capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise Unavailable('Codex 订阅登录状态无法确认；未启动') from exc
            if result.returncode != 0 or 'ChatGPT' not in result.stdout + result.stderr:
                raise Unavailable('Codex 当前不是 ChatGPT 订阅登录（codex login status）；未启动')

    def validate(self, name, config):
        super().validate(name, config)
        if config['transport'] == 'orca':
            for field in ('model', 'effort'):
                if not text(config.get(field)):
                    raise ValueError(f'配置档 {name} 的 {field}：交互启动必须写明')
        if config['transport'] == 'orca' and (config.get('ignore_rules') or config.get('ignore_user_config')):
            raise ValueError(f'配置档 {name} 的 ignore_rules/ignore_user_config：当前 CLI 只支持 headless')
        if 'plugins' in config:
            for plugin, value in config['plugins'].items():
                if not mapping(value) or type(value.get('enabled')) is not bool:
                    raise ValueError(f'配置档 {name} 的 plugins.{plugin}：须含 enabled 布尔值')
        if 'settings' in config and isinstance(config['settings'], dict):
            try:
                toml_value(config['settings'])
            except ValueError as exc:
                raise ValueError(f'配置档 {name} 的 settings：{exc}') from exc

    def argv(self, executor, config, workdir, session, resume, directory, trust_workdir=None):
        argv = [executor.commands[self.binary]]
        if executor.interactive:
            if resume:
                argv += ['resume']
            argv += ['-m', config['model'], '-c', 'model_reasoning_effort=' + json.dumps(config['effort'])]
            if 'sandbox' in config:
                argv += ['-s', config['sandbox']]
            if 'permission_mode' in config:
                argv += ['-a', config['permission_mode']]
            argv += ['-C', workdir]
            if config.get('model_provider'):
                argv += ['-c', 'model_provider=' + json.dumps(config['model_provider'])]
            hook = json.dumps(shlex.join([sys.executable, str(Path(__file__).parents[1] / 'hook.py')]))
            hooks = '[{hooks=[{type="command",command=%s}]}]' % hook
            trusted = ','.join(json.dumps(path) + '={trust_level="trusted"}' for path in dict.fromkeys((trust_workdir or workdir, workdir)))
            argv += ['-c', 'projects={' + trusted + '}', '--dangerously-bypass-hook-trust',
                     '-c', 'hooks.SessionStart=' + hooks, '-c', 'hooks.UserPromptSubmit=' + hooks]
        else:
            argv += ['exec']
            for field, native in (('sandbox', 'sandbox_mode'), ('permission_mode', 'approval_policy')):
                if field in config:
                    argv += ['-c', native + '=' + json.dumps(config[field])]
            for field, flag in (('ignore_user_config', '--ignore-user-config'), ('ignore_rules', '--ignore-rules')):
                if config.get(field):
                    argv += [flag]
            argv += ['--json']
            if config.get('model'):
                argv += ['--model', config['model']]
            if config.get('effort'):
                argv += ['-c', 'model_reasoning_effort=' + json.dumps(config['effort'])]
            if config.get('model_provider'):
                argv += ['-c', 'model_provider=' + json.dumps(config['model_provider'])]
        additions = dict(settings(config['settings'])) if 'settings' in config else {}
        prompt = instructions(config)
        if prompt:
            additions['developer_instructions'] = '\n\n'.join(filter(None, (additions.get('developer_instructions'), prompt)))
        if 'mcp_config' in config:
            value = settings(config['mcp_config'])
            additions['mcp_servers'] = value.get('mcp_servers', value)
        if 'plugins' in config:
            additions['plugins'] = config['plugins']
        for tool, value in config.get('tools', {}).items():
            if tool == 'shell':
                additions.setdefault('features', {})['shell_tool'] = value
            else:
                additions['web_search'] = value
        # Emit leaf settings, so custom hooks/features don't overwrite the round's hooks.
        def emit(key, value):
            # CLI paths are split on dots, not parsed as TOML keys. Quoted
            # segments become literal quote characters and are silently ignored.
            # A map with non-bare names stays an inline TOML table so names such
            # as plugin@local or an MCP server containing a dot survive intact.
            if (isinstance(value, dict) and value
                    and all(re.fullmatch(r'[A-Za-z0-9_-]+', child) for child in value)):
                for child, content in value.items():
                    emit(key + '.' + child, content)
            else:
                argv.extend(['-c', key + '=' + toml_value(value)])
        for key, value in additions.items():
            emit(key, value)
        if resume:
            argv += [session] if executor.interactive else ['resume', session]
        elif not executor.interactive and 'sandbox' in config:
            argv += ['--sandbox', config['sandbox']]
        return argv

    def missing_session(self, spec, output, code):
        session = spec.get('vendor_id')
        if code <= 0 or spec['interactive'] or not session or not output.exists():
            return False
        pattern = (r'(?:Error: )?(?:thread/resume: thread/resume failed: )?no rollout found for thread id[ :]+[\x27\"]?'
                   + re.escape(session) + r'[\x27\"]?\.?(?: \(code -32600\))?')
        for line in output.read_text(errors='replace').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                message = line.strip()
            else:
                # Task output quoting an error is not a failed resume.
                message = row.get('message', '') if isinstance(row, dict) and row.get('type') == 'error' else ''
            if isinstance(message, str) and re.fullmatch(pattern, message):
                return True
        return False

    def session_from_output(self, output):
        if not output.exists():
            return None
        for line in output.read_text(errors='replace').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('type') == 'thread.started':
                return row.get('thread_id')
        return None

    def hook_session(self, data, spec):
        return data.get('session_id')

    def recover_session(self, executor, directory, spec):
        found = super().recover_session(executor, directory, spec)
        path = Path(spec['session_file'])
        if path.exists():
            return read(path).get('vendor_id')
        if spec['interactive']:
            home = Path(spec.get('env', {}).get('CODEX_HOME', executor.home / '.codex'))
            found = session_in(home, {spec['workdir'], spec.get('trust_workdir', spec['workdir'])},
                               (directory / 'spec.json').stat().st_mtime - 1)
            if found:
                write(path, {'vendor_id': found, 'model': spec.get('model')})
        return found

    def final_message(self, output, start=0):
        content = super().final_message(output, start)
        final = None
        for line in (content or '').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('type') == 'item.completed' and row.get('item', {}).get('type') == 'agent_message':
                final = row['item'].get('text', '')
        return final if final is not None else content

    def usage(self, executor, directory, spec, since):
        session = self.recover_session(executor, directory, spec)
        return collect(executor.home, session, spec['workdir'], since, spec['billing'], spec.get('env', {}).get('CODEX_HOME'))

    collect = staticmethod(lambda *a, **kw: collect(*a, **kw))


def session_in(root, workdirs, since):
    found = set()
    for path in (Path(root) / 'sessions').rglob('rollout-*.jsonl'):
        try:
            if path.stat().st_mtime < since:
                continue
            with path.open() as stream:
                first = json.loads(stream.readline())
            meta = first['payload']
            started = datetime.fromisoformat(meta['timestamp'].replace('Z', '+00:00')).timestamp()
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
        if first.get('type') == 'session_meta' and meta.get('cwd') in workdirs and started >= since and not isinstance(meta.get('source'), dict):
            found.add(meta['id'])
    return found.pop() if len(found) == 1 else None
def codex_entries(records, since, billing):
    model, previous = None, None
    entries = []
    for row in records:
        data = row.get('payload', {})
        if row.get('type') == 'turn_context':
            model = data.get('model')
        if row.get('type') != 'event_msg' or data.get('type') != 'token_count':
            continue
        info = data.get('info') or {}
        total = info.get('total_token_usage')
        if not isinstance(total, dict):
            continue
        if previous == total:
            continue
        last = info.get('last_token_usage') or {}
        # Totals are cumulative within a session; last_token_usage may repeat on status events.
        if previous is not None:
            values = {k: number(total.get(k) - previous.get(k))
                      if type(total.get(k)) is int and type(previous.get(k)) is int else None
                      for k in ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')}
            # A context reset can restart cumulative totals. The event's observed last call is authoritative.
            if any(value is None for key, value in values.items() if key in total and key in previous):
                values = last
        else:
            values = last
        previous = total
        if not after(row, since):
            continue
        inp, read, write, output = [number(values.get(k)) for k in (
            'input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens')]
        ordinary = number(inp - read - write) if None not in (inp, read, write) else None
        # Unknown TTL for positive OpenAI writes: keep the count, don't guess a price.
        short = long = 0 if write == 0 else None
        entries.append(Usage(model, ordinary, read, write, output,
                             cost(model, ordinary, read, short, long, output), billing))
    return entries



def collect(home, session, workdir, since, billing, config_dir=None):
    unknown = RoundUsage(main=Usage(billing=billing))
    if not session:
        return unknown
    root = (Path(config_dir).expanduser() if config_dir else home / '.codex') / 'sessions'
    index, complete = {}, True
    # Codex children use separate rollout files and source.subagent.thread_spawn.parent_thread_id.
    for path in root.rglob('*.jsonl'):
        try:
            with path.open() as stream:
                first = json.loads(stream.readline())
            meta = first['payload']
            if first.get('type') == 'session_meta':
                index[meta['id']] = (path, meta)
        except (OSError, ValueError, KeyError):
            complete = False
    if session not in index:
        return unknown
    data, ok = rows(index[session][0])
    complete &= ok
    entries = codex_entries(data, since, billing)
    descendants, todo = set(), [session]
    children = []
    while todo:
        parent = todo.pop()
        for child_id, (path, meta) in index.items():
            source = meta.get('source')
            if not isinstance(source, dict):
                continue
            spawned = source.get('subagent', {}).get('thread_spawn', {})
            if spawned.get('parent_thread_id') != parent or child_id in descendants:
                continue
            descendants.add(child_id)
            todo.append(child_id)
            child_rows, ok = rows(path)
            complete &= ok
            child_entries = codex_entries(child_rows, since, billing)
            children.extend(child_entries)
            if any(after(r, since) for r in child_rows) and not child_entries:
                complete = False
    # Archived compressed rollouts and missing spawn outputs cannot be claimed fully counted.
    if any(path.stat().st_mtime >= datetime.fromisoformat(since).timestamp()
           for path in root.rglob('*.jsonl.zst')):
        complete = False
    for row in data:
        payload = row.get('payload', {})
        if after(row, since) and payload.get('type') == 'function_call_output':
            output = payload.get('output', '')
            try:
                spawned = json.loads(output).get('agent_id')
            except (ValueError, TypeError, AttributeError):
                spawned = None
            if spawned and spawned not in descendants:
                complete = False
    return RoundUsage(aggregate(entries, billing), grouped(children, billing), complete and bool(entries))
