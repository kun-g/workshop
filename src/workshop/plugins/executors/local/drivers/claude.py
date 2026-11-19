"""Claude Code command, session and transcript protocol."""
import copy
from datetime import datetime, timezone
import os
import subprocess
import json
from pathlib import Path
import re
import shlex
import sys
import urllib.error
import urllib.parse
import urllib.request

from workshop.core.ports import Unavailable
from workshop.core.models import RoundUsage, Usage
from ..storage import write
from ..usage import after, number, aggregate, grouped, rows
from ..pricing import cost
from .base import Driver as Base, text, strings, options, json_options, instructions

# 个人配置里会被当成指令读进来的文件（相对每一层目录）；执行者目录模式下，工作目录以上各层都排除。
OUTSIDE_INSTRUCTIONS = ('CLAUDE.md', 'CLAUDE.local.md', 'AGENTS.md', '.claude/CLAUDE.md', '.claude/rules/**')


def outside_instructions(*workdirs):
    """工作目录以上每一层的指令文件，写成 claudeMdExcludes 的绝对路径。"""
    patterns = []
    for workdir in filter(None, workdirs):
        for parent in Path(workdir).parents:
            patterns += [str(parent / name) for name in OUTSIDE_INSTRUCTIONS]
    return list(dict.fromkeys(patterns))


class Driver(Base):
    family = 'claude'
    binary = 'claude'
    fields = {
        'require_subscription_login': lambda v: type(v) is bool,
        'prompt': text, 'skills': strings, 'tools': lambda v: isinstance(v, str) or strings(v),
        'allowed_tools': strings, 'disallowed_tools': strings,
        'permission_mode': lambda v: v in ('acceptEdits', 'auto', 'bypassPermissions', 'manual', 'dontAsk', 'plan'),
        'restricted': lambda v: type(v) is bool, 'strict_mcp_config': lambda v: type(v) is bool,
        'mcp_config': options, 'plugins': strings, 'settings': options, 'config_dir': text,
        'executor_dir': text, 'gateway': lambda v: isinstance(v, dict),
    }
    settings_file = 'settings.json'
    isolation = True
    # 网关探测：不带认证访问根地址，拿到任何 HTTP 回应就算连得上。
    gateway_timeout = 3

    def directory_settings(self, path):
        file = Path(path) / self.settings_file
        if not file.exists():
            return {}
        value = json.loads(file.read_text())
        if not isinstance(value, dict):
            raise ValueError('顶层必须是 JSON 对象')
        return value

    def validate(self, name, config):
        super().validate(name, config)
        if 'gateway' in config:
            check_gateway(name, config)
        if 'executor_dir' in config and 'config_dir' in config:
            # 执行者目录模式沿用本机订阅登录（默认配置目录），不能同时换一个登录。
            raise ValueError(f'配置档 {name} 的 config_dir：和 executor_dir 不能同时写，执行者沿用本机登录')

    def environment(self, config):
        env = {'CLAUDE_CONFIG_DIR': str(Path(config['config_dir']).expanduser())} if 'config_dir' in config else {}
        if 'gateway' in config:
            # 用 AUTH_TOKEN 而不是 API_KEY：交互模式带 API_KEY 会停在「是否使用这个 key」的确认框。
            env.update(ANTHROPIC_BASE_URL=config['gateway']['base_url'],
                       ANTHROPIC_AUTH_TOKEN=config['gateway']['token'])
        return env

    def available(self, executor, config):
        """配了网关的，每次挑选时探一次网关；连不上就不可用，并把地址写进驱动日志。不在启动时抛错。"""
        if 'gateway' not in config:
            return True
        url = config['gateway']['base_url']
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(url, timeout=self.gateway_timeout):
                return True
        except urllib.error.HTTPError:
            return True  # 网关回了话（哪怕是错误码）就是连得上。
        except (OSError, ValueError) as exc:
            log(executor, f'网关连不上：{url}（{exc}）')
            return False

    def preflight(self, executor, config):
        if not config.get('require_subscription_login'):
            return
        env = dict(os.environ)
        env.update(self.environment(config))
        try:
            result = subprocess.run([executor.commands[self.binary], 'auth', 'status'], env=env,
                                    capture_output=True, text=True, timeout=30)
            value = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise Unavailable('Claude 订阅登录状态无法确认；未启动') from exc
        if (result.returncode or value.get('loggedIn') is not True or value.get('authMethod') != 'claude.ai'
                or value.get('apiProvider') != 'firstParty' or not value.get('subscriptionType')):
            raise Unavailable('Claude 当前不是订阅登录（claude auth status）；未启动')

    def argv(self, executor, config, workdir, session, resume, directory, trust_workdir=None):
        argv = [executor.commands[self.binary], '--resume' if resume else '--session-id', session]
        if config.get('model'):
            argv += ['--model', config['model']]
        if config.get('effort'):
            argv += ['--effort', config['effort']]
        # 执行者目录模式（核心 R25）：不读用户级设置、hook、全局指令和自动记忆，只读仓库这一层和执行者目录。
        isolated = bool(config.get('executor_dir'))
        settings = copy.deepcopy(self.directory_settings(config['executor_dir'])) if isolated else {}
        settings.update(copy.deepcopy(json_options(config['settings'])) if 'settings' in config else {})
        if isolated:
            argv += ['--setting-sources', 'project']
            settings['autoMemoryEnabled'] = False
            settings['claudeMdExcludes'] = [*settings.get('claudeMdExcludes', []),
                                            *outside_instructions(workdir, trust_workdir)]
        if executor.interactive:
            hook = shlex.join([sys.executable, str(Path(__file__).parents[1] / 'hook.py')])
            settings.setdefault('hooks', {}).setdefault('UserPromptSubmit', []).append({'hooks': [{'type': 'command', 'command': hook}]})
            write(directory / 'hooks.json', settings)
            argv += ['--settings', str(directory / 'hooks.json')]
        else:
            argv += ['-p', '--output-format', 'json']
            if 'settings' in config or isolated:
                argv += ['--settings', json.dumps(settings, ensure_ascii=False)]
        if config.get('restricted'):
            argv += ['--restricted']
        if 'permission_mode' in config:
            argv += ['--permission-mode', config['permission_mode']]
        for field, flag in (('tools', '--tools'), ('allowed_tools', '--allowedTools'), ('disallowed_tools', '--disallowedTools')):
            if field in config:
                value = config[field]
                argv += [flag, ','.join(value) if isinstance(value, list) else value]
        if config.get('strict_mcp_config') or isolated:
            argv += ['--strict-mcp-config']
        if 'mcp_config' in config or isolated:
            value = config.get('mcp_config', {'mcpServers': {}})
            argv += ['--mcp-config', json.dumps(value, separators=(',', ':')) if isinstance(value, dict) else str(Path(value).expanduser())]
        prompt = instructions(config)
        if prompt:
            argv += ['--append-system-prompt', prompt]
        for plugin in config.get('plugins', []):
            argv += ['--plugin-dir', str(Path(plugin).expanduser())]
        return argv

    def prepare(self, spec, env, directory):
        if spec['interactive']:
            from ..trust import prepare
            prepare(spec.get('trust_workdir', spec['workdir']), env, directory)

    def initial_argv(self, spec, directory, prompt, external):
        argv = list(spec['argv'])
        if spec['interactive']:
            if external and '--restricted' in argv:
                argv += ['--add-dir', str(directory)]
            argv += ['--', prompt]
        return argv

    def usage(self, executor, directory, spec, since):
        return collect(executor.home, spec.get('vendor_id') or spec['session'], spec['workdir'], since,
                       spec['billing'], spec.get('env', {}).get('CLAUDE_CONFIG_DIR'))

    collect = staticmethod(lambda *a, **kw: collect(*a, **kw))


GATEWAY_FIELDS = ('base_url', 'token')


def check_gateway(name, config):
    """gateway 只认 base_url、token 两项，都必须写；base_url 是 http(s) 地址。"""
    def fail(field, reason):
        raise ValueError(f'配置档 {name} 的 {field}：{reason}')
    gateway = config['gateway']
    for key in gateway:
        if key not in GATEWAY_FIELDS:
            fail('gateway.' + str(key), '字段拼错；只认 base_url、token')
    for key in GATEWAY_FIELDS:
        if not text(gateway.get(key)):
            fail('gateway.' + key, '必须写明非空字符串')
    parsed = urllib.parse.urlsplit(gateway['base_url'])
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        fail('gateway.base_url', '必须是 http:// 或 https:// 地址')
    if config.get('require_subscription_login'):
        fail('require_subscription_login', '配了 gateway 登录用网关的，不查本机订阅登录')


def log(executor, message):
    """驱动日志：执行者状态目录下的 driver.log，一行一条。"""
    try:
        executor.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with (executor.root / 'driver.log').open('a') as stream:
            stream.write(f'{stamp} {message}\n')
    except (OSError, AttributeError):
        pass


def model_name(model):
    """会话记录里经网关的模型名带来源前缀（codex/gpt-6.2-astra），记用量和查价格用去掉前缀的名字。"""
    return model.rsplit('/', 1)[-1] if isinstance(model, str) else model


def claude_entries(records, since, billing):
    messages = {}
    for row in records:
        if row.get('type') != 'assistant' or not after(row, since):
            continue
        msg = row.get('message', {})
        if msg.get('model') == '<synthetic>':
            continue
        if not isinstance(msg.get('usage'), dict):
            continue
        if not msg.get('model') and all(msg['usage'].get(key) == 0 for key in
                ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')):
            continue
        # Streaming content blocks can repeat one API message; use its final cumulative usage.
        key = msg.get('id') or row.get('uuid')
        if not key:
            continue
        messages[key] = msg
    entries = []
    for msg in messages.values():
        u = msg['usage']
        ordinary, read, write, output = [number(u.get(k)) for k in (
            'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')]
        split = u.get('cache_creation') or {}
        short, long = number(split.get('ephemeral_5m_input_tokens')), number(split.get('ephemeral_1h_input_tokens'))
        if write == 0:
            short = long = 0
        if short is not None and long is not None and short + long != write:
            short = long = None
        model = model_name(msg.get('model'))
        entries.append(Usage(model, ordinary, read, write, output,
                             cost(model, ordinary, read, short, long, output), billing))
    return entries



def collect(home, session, workdir, since, billing, config_dir=None):
    unknown = RoundUsage(main=Usage(billing=billing))
    if not session:
        return unknown
    root = (Path(config_dir).expanduser() if config_dir else home / '.claude') / 'projects'
    encoded = re.sub(r'[^a-zA-Z0-9]', '-', workdir)
    path = root / encoded / (session + '.jsonl')
    if not path.exists():
        paths = list(root.glob('*/' + session + '.jsonl'))
        if len(paths) != 1:
            return unknown
        path = paths[0]
    main_rows, complete = rows(path)
    entries = claude_entries(main_rows, since, billing)
    children = []
    all_rows = list(main_rows)
    child_paths = list((path.parent / session / 'subagents').glob('agent-*.jsonl'))
    for child in child_paths:
        data, ok = rows(child)
        all_rows.extend(data)
        complete &= ok
        child_entries = claude_entries(data, since, billing)
        children.extend(child_entries)
        if any(after(r, since) for r in data) and not child_entries:
            complete = False
    # Tool evidence of a child without its separate transcript is an accounting gap.
    launches = any(block.get('type') == 'tool_use' and block.get('name') in ('Agent', 'Task')
                   for row in all_rows if after(row, since)
                   for block in row.get('message', {}).get('content', []) if isinstance(block, dict))
    referenced = {row['toolUseResult']['agentId'] for row in all_rows if after(row, since)
                  and isinstance(row.get('toolUseResult'), dict) and row['toolUseResult'].get('agentId')}
    present = {child.stem.removeprefix('agent-') for child in child_paths}
    complete &= referenced <= present and (not launches or bool(referenced))
    return RoundUsage(aggregate(entries, billing), grouped(children, billing), complete and bool(entries))
