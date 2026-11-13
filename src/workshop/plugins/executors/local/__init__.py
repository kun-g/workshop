"""Execution adapters registered through workshop.executor entry points."""
from .adapters import Executor


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
