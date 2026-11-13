"""Read provider records, retain unknown fields, and exclude previous rounds."""
from collections import defaultdict
from datetime import datetime
import json

from workshop.core.models import Usage


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


def grouped(entries, billing):
    by_model = defaultdict(list)
    for entry in entries:
        by_model[entry.model_actual].append(entry)
    return [aggregate(group, billing) for group in by_model.values()]


def collect(kind, home, session, workdir, since, billing):
    from .drivers import for_spec
    return for_spec({'kind': kind}).collect(home, session, workdir, since, billing)
