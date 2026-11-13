"""Read provider records, retain unknown fields, and exclude previous rounds."""
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re

from workshop.core.models import RoundUsage, Usage
from .pricing import cost


def number(value):
    return value if type(value) is int and value >= 0 else None


def rows(path):
    result, complete = [], True
    try:
        for line in path.read_text().splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                complete = False
    except (OSError, UnicodeError):
        complete = False
    return result, complete


def after(row, since):
    try:
        return datetime.fromisoformat(row['timestamp']) >= datetime.fromisoformat(since)
    except (ValueError, KeyError, TypeError):
        return False


def aggregate(entries, billing):
    if not entries:
        return Usage(billing=billing)
    models = {entry.model_actual for entry in entries}
    values = {}
    for field in ('input', 'cache_read', 'cache_write', 'output', 'cost_usd'):
        parts = [getattr(entry, field) for entry in entries]
        values[field] = sum(parts) if all(p is not None for p in parts) else None
    return Usage(model_actual=next(iter(models)) if len(models) == 1 else None, billing=billing, **values)


def claude_entries(records, since, billing):
    messages = {}
    for row in records:
        if row.get('type') != 'assistant' or not after(row, since):
            continue
        msg = row.get('message', {})
        if not isinstance(msg.get('usage'), dict):
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
        entries.append(Usage(msg.get('model'), ordinary, read, write, output,
                             cost(msg.get('model'), ordinary, read, short, long, output), billing))
    return entries


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


def grouped(entries, billing):
    by_model = defaultdict(list)
    for entry in entries:
        by_model[entry.model_actual].append(entry)
    return [aggregate(group, billing) for group in by_model.values()]


def collect(kind, home, session, workdir, since, billing):
    unknown = RoundUsage(main=Usage(billing=billing))
    if not session:
        return unknown
    if kind in ('claude', 'orca-claude'):
        root = home / '.claude' / 'projects'
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
    root = home / '.codex' / 'sessions'
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
