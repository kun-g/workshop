"""CLI driver port. Discovery uses entry points, never a list of CLI names."""
from importlib.metadata import entry_points


def load(name):
    entries = list(entry_points(group='workshop.driver', name=name))
    if len(entries) != 1:
        raise ValueError(f'driver {name} 必须恰好注册一次')
    return entries[0].load()()


def for_spec(spec):
    if spec.get('driver'):
        return load(spec['driver'])
    # Old in-flight specs carry only the profile name.
    from .. import default_profile
    return load(default_profile(spec['kind'])['driver'])
