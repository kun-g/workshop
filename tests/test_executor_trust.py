import json
import os
from pathlib import Path
import stat
import time
from contextlib import contextmanager

import pytest

from workshop.plugins.executors.local import trust


def config(tmp_path, value):
    path = tmp_path/'.claude.json'
    path.write_text(json.dumps(value))
    return path


def test_registers_original_and_real_paths_preserving_fields_and_mode(tmp_path):
    real = tmp_path/'real'
    real.mkdir()
    alias = tmp_path/'alias'
    alias.symlink_to(real, target_is_directory=True)
    value = {'login': {'unchanged': 123}, 'projects': {
        str(alias): {'allowedTools': ['Read'], 'hasTrustDialogAccepted': False},
        str(real): {'history': ['existing']}, '/other': {'value': 2}}}
    path = config(tmp_path, value)
    path.chmod(0o640)
    assert trust.grant(str(alias), {'HOME': str(tmp_path)}) == 'granted'
    for key in (str(alias), str(real)):
        value['projects'][key]['hasTrustDialogAccepted'] = True
    assert json.loads(path.read_text()) == value
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not Path(str(path)+'.lock').exists()
    assert not list(tmp_path.glob('.workshop-trust-*'))


@pytest.mark.parametrize('trusted_key', ['alias', 'real'])
def test_any_trusted_candidate_leaves_config_untouched(tmp_path, trusted_key):
    (tmp_path/'real').mkdir()
    (tmp_path/'alias').symlink_to(tmp_path/'real', target_is_directory=True)
    path = config(tmp_path, {'projects': {str(tmp_path/trusted_key): {'hasTrustDialogAccepted': True}}})
    before, content = path.stat(), path.read_bytes()
    Path(str(path)+'.lock').mkdir()  # An already trusted directory needs no write or lock.
    assert trust.grant(str(tmp_path/'alias'), {'HOME': str(tmp_path)}) == 'unchanged'
    after = path.stat()
    assert path.read_bytes() == content
    assert (after.st_ino, after.st_mtime_ns, after.st_ctime_ns) == (before.st_ino, before.st_mtime_ns, before.st_ctime_ns)


@pytest.mark.parametrize('override,legacy', [(False, False), (False, True), (True, False), (True, True)])
def test_config_selection_matches_cli(tmp_path, override, legacy):
    env = {'HOME': str(tmp_path)}
    folder = tmp_path/'custom' if override else tmp_path/'.claude'
    if override:
        env['CLAUDE_CONFIG_DIR'] = str(folder)
    folder.mkdir()
    path = folder/'.config.json' if legacy else (folder if override else tmp_path)/'.claude.json'
    path.write_text('{}')
    assert trust.config_path(env) == path
    assert trust.grant(str(tmp_path/'work'), env) == 'granted'
    assert json.loads(path.read_text())['projects'][str(tmp_path/'work')]['hasTrustDialogAccepted']


def test_stale_directory_lock_is_taken_over(tmp_path):
    path = config(tmp_path, {})
    lock = Path(str(path)+'.lock')
    lock.mkdir()
    old = time.time()-11
    os.utime(lock, (old, old))
    assert trust.grant(str(tmp_path/'work'), {'HOME': str(tmp_path)}) == 'granted'
    assert not lock.exists()


def test_config_symlink_preserved(tmp_path):
    target = tmp_path/'actual.json'
    target.write_text('{}')
    path = tmp_path/'.claude.json'
    path.symlink_to(target)
    assert trust.grant(str(tmp_path/'work'), {'HOME': str(tmp_path)}) == 'granted'
    assert path.is_symlink()
    assert json.loads(target.read_text())['projects'][str(tmp_path/'work')]['hasTrustDialogAccepted']


def test_rereads_after_taking_lock(tmp_path, monkeypatch):
    path = config(tmp_path, {})
    original = trust.config_lock
    @contextmanager
    def concurrent_update(lock):
        path.write_text('{"another_writer": 42}')
        with original(lock) as check:
            yield check
    monkeypatch.setattr(trust, 'config_lock', concurrent_update)
    trust.grant(str(tmp_path/'work'), {'HOME': str(tmp_path)})
    assert json.loads(path.read_text())['another_writer'] == 42


@pytest.mark.parametrize('value', [[], {'projects': []}])
def test_nonobject_config_untouched_and_warning_recorded(tmp_path, value):
    path = config(tmp_path, value)
    before = path.read_bytes()
    trust.prepare(str(tmp_path/'work'), {'HOME': str(tmp_path)}, tmp_path/'round')
    assert path.read_bytes() == before
    assert json.loads((tmp_path/'round/trust-warning.json').read_text())['message'] == '没能预先信任目录'


def test_read_failure_is_nonfatal_and_does_not_record_secrets(tmp_path, monkeypatch):
    path = config(tmp_path, {})
    def unreadable(_):
        raise PermissionError('sensitive content must not be logged')
    monkeypatch.setattr(trust, 'read_config', unreadable)
    trust.prepare(str(tmp_path/'work'), {'HOME': str(tmp_path)}, tmp_path/'round')
    assert path.read_text() == '{}'
    record = (tmp_path/'round/trust-warning.json').read_text()
    assert 'PermissionError' in record and 'sensitive' not in record


def test_replace_failure_preserves_config_releases_lock_and_cleans_temp(tmp_path, monkeypatch):
    path = config(tmp_path, {})
    def fail(*_):
        raise PermissionError('read-only filesystem')
    monkeypatch.setattr(trust.os, 'replace', fail)
    with pytest.raises(PermissionError):
        trust.grant(str(tmp_path/'work'), {'HOME': str(tmp_path)})
    assert path.read_text() == '{}'
    assert not Path(str(path)+'.lock').exists()
    assert not list(tmp_path.glob('.workshop-trust-*'))


def test_concurrent_writers_keep_all_directory_entries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = config(tmp_path, {'untouched': 7})
    directories = [str(tmp_path/f'work-{i}') for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda directory: trust.grant(directory, {'HOME': str(tmp_path)}), directories))
    assert results == ['granted']*4
    value = json.loads(path.read_text())
    assert value['untouched'] == 7
    assert set(value['projects']) == set(directories)
    assert all(entry['hasTrustDialogAccepted'] for entry in value['projects'].values())
