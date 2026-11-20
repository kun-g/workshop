"""#306：从三种来源的真实 poll 到来源退避，只替换子进程和时钟。"""
from datetime import timedelta
import json
import subprocess

import pytest

from test_watch_updates import software
from workshop import github_identity
from workshop.core.ports import Retryable, TargetBlocked
from workshop.plugins.scheduled_release.source import MainBranchSource
from workshop.plugins.sources.github import GitHubSource
from workshop.plugins.watch_updates.io import IO, write
from workshop.plugins.watch_updates.model import SCHEMA
from workshop.plugins.watch_updates.source import Source


TOKEN = 'private-test-credential'


def identity(mode):
    return {'mode': 'app', 'signer': ['sign'], 'gh': '/fake/gh'} if mode == 'app' else {'mode': 'inherit'}


def install(engine, tmp_path, name, mode):
    target = engine.rules.data['targets']['alpha']
    target.update(repo='example/project', github_identity=identity(mode), sources=[name])
    if name == 'github':
        source = GitHubSource('/fake/gh', tmp_path / 'cache')
    elif name == 'main_branch':
        target.update(checkout=str(tmp_path), scheduled_release_state_dir=str(tmp_path))
        source = MainBranchSource(engine.clock)
    else:
        watchlist = tmp_path / 'watchlist.json'
        write(watchlist, {'schema': SCHEMA, 'software': [software()]})
        target['watch_updates'] = dict(enabled=True, timezone='UTC', hour=0, weekday=0,
                                       watchlist=str(watchlist), state_dir=str(tmp_path / 'watch'))
        source = Source(IO(engine.clock))
    engine.plugins.register('source', name, source)
    return source


@pytest.mark.parametrize('name,command_arg', [('watch_updates', '/releases?'), ('main_branch', 'ls-remote'),
                                              ('github', '--include')])
@pytest.mark.parametrize('mode', ['inherit', 'app'])
@pytest.mark.parametrize('fault', ['timeout', 'oserror'])
def test_each_read_reaches_source_backoff(engine, tmp_path, monkeypatch, name, command_arg, mode, fault):
    source = install(engine, tmp_path, name, mode)
    monkeypatch.setenv('GH_TOKEN', TOKEN)

    def command(argv, **kwargs):
        if argv[0] == 'sign':
            return subprocess.CompletedProcess(argv, 0, TOKEN, '')
        if fault == 'timeout':
            raise subprocess.TimeoutExpired(argv, 7, output=TOKEN, stderr=f'{TOKEN} diagnostic tail'.encode())
        raise ConnectionResetError(TOKEN)

    monkeypatch.setattr(subprocess, 'run', command)
    with pytest.raises(Retryable) as error:
        source.poll(engine.rules.target('alpha'), None)
    message = str(error.value)
    assert command_arg in message and TOKEN not in message
    if fault == 'timeout':
        assert '7 秒' in message and 'diagnostic tail' in message
    else:
        assert 'ConnectionResetError' in message
    engine.sources()
    assert engine.control(f'retry:source:alpha:{name}')['count'] == 1
    assert not any(row.get('broken') for row in engine.log.all('controls'))
    retry = next(f['body'] for f in engine.log.facts() if f['kind'] == 'plugin_retry')
    assert retry['error'] == message
    engine.clock.now += timedelta(minutes=30)
    engine.sources()
    failure = next(f['body'] for f in engine.log.facts() if f['kind'] == 'plugin_failed')
    assert message in failure['error'] and TOKEN not in json.dumps(engine.log.facts())


def test_sign_then_read_timeout_keeps_one_retry_window(engine, tmp_path, monkeypatch):
    install(engine, tmp_path, 'watch_updates', 'app')
    calls = []

    def command(argv, **kwargs):
        calls.append(argv[0])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, 30, output=TOKEN)
        if argv[0] == 'sign':
            return subprocess.CompletedProcess(argv, 0, TOKEN, '')
        raise subprocess.TimeoutExpired(argv, 60, stderr=f'failed: {TOKEN}')

    monkeypatch.setattr(subprocess, 'run', command)
    engine.sources()
    engine.clock.now += timedelta(minutes=1)
    engine.sources()
    attempts = [f['body'] for f in engine.log.facts() if f['kind'] == 'plugin_retry']
    assert [a['count'] for a in attempts] == [1, 2]
    assert 'App 签发失败' in attempts[0]['error'] and '/releases?' in attempts[1]['error']
    assert calls == ['sign', 'sign', '/fake/gh']
    assert TOKEN not in json.dumps(engine.log.facts())
    assert not any(row.get('broken') for row in engine.log.all('controls'))


@pytest.mark.parametrize('mode', ['inherit', 'app'])
@pytest.mark.parametrize('fault', ['timeout', 'oserror'])
def test_ambiguous_action_is_not_retryable(mode, fault, monkeypatch):
    def command(argv, **kwargs):
        if argv[0] == 'sign':
            return subprocess.CompletedProcess(argv, 0, TOKEN, '')
        if fault == 'timeout':
            raise subprocess.TimeoutExpired(argv, 10, stderr=TOKEN)
        raise OSError(TOKEN)

    monkeypatch.setattr(subprocess, 'run', command)

    @github_identity.scoped
    def action(target):
        github_identity.run(['gh', 'issue', 'comment', '1'])

    with pytest.raises(TargetBlocked) as error:
        action({'repo': 'example/project', 'github_identity': identity(mode)})
    assert not isinstance(error.value, Retryable) and TOKEN not in str(error.value)


def test_explicit_env_stays_clean_and_ambiguous_write_is_blocked(monkeypatch):
    def command(argv, **kwargs):
        assert argv == ['git', 'push'] and kwargs['env'] == {}
        raise OSError('write failed')

    monkeypatch.setattr(subprocess, 'run', command)

    @github_identity.scoped
    def action(target):
        github_identity.run(['git', 'push'], env={})

    with pytest.raises(TargetBlocked) as error:
        action({'repo': 'example/project', 'github_identity': identity('app')})
    assert not isinstance(error.value, Retryable)


def test_redacts_argv_and_long_stderr_before_truncation(monkeypatch):
    monkeypatch.setenv('GH_TOKEN', TOKEN)

    def command(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 2, stderr='x' * 1000 + TOKEN + 'y' * 490)

    monkeypatch.setattr(subprocess, 'run', command)
    with pytest.raises(Retryable) as error:
        github_identity.run_read(['gh', 'api', '-H', 'Authorization: Bearer ' + TOKEN])
    message = str(error.value)
    assert TOKEN not in message and 'test-credential' not in message and 'x' * 501 not in message
