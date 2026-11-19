"""Execution adapters registered through workshop.executor entry points."""
from pathlib import Path
import yaml
from .adapters import Executor


def default_profile(name):
    """Compatibility entry points read the same rule file; no second policy table."""
    path = Path(__file__).resolve().parents[5] / 'rules/workshop.yaml'
    profiles = yaml.safe_load(path.read_text())['executor_profiles']
    if name not in profiles:
        raise ValueError(f'配置档 {name} 未在规则中定义')
    from .drivers import load
    return load(profiles[name]['driver']).bind(name, dict(profiles[name]), path)


def make_orca_claude():
    return Executor('orca-claude')


def make_orca_codex():
    return Executor('orca-codex')


def make_codex():
    return Executor('codex')


def make_cursor_grok():
    return Executor('cursor-grok')


def make_claude():
    return Executor('claude')
