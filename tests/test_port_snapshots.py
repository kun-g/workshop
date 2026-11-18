"""端口临时字段不落库、不泄到其他端口，派活说明保持原样。"""
import pytest

from conftest import event, item, reach
from test_issue_delivery import world, snapshot  # noqa: F401


def test_allowed_advances_only_in_brief(engine, monkeypatch):
    seen = []
    call = engine.call

    def observe(port, name, method, *args, **kwargs):
        for arg in args:
            if isinstance(arg, dict) and 'object_key' in arg:
                assert ('allowed_advances' in arg) == (port == 'flow' and method == 'brief')
                if method == 'brief':
                    seen.append(arg['allowed_advances'])
        return call(port, name, method, *args, **kwargs)

    monkeypatch.setattr(engine, 'call', observe)
    engine.ingest(event())
    reach(engine, '执行')
    assert seen and all(isinstance(value, list) for value in seen)
    assert 'allowed_advances' not in item(engine)


@pytest.mark.parametrize('stage,allowed,expected', [
    ('执行', ['交付'], 'advance 只能是「交付」，可以不填。'),
    ('收尾', ['执行'], 'advance 只能是「执行」，可以不填。'),
    ('执行', ['证据', '验收'], 'advance 只能是「证据」、「验收」，可以不填。'),
    ('执行', [], 'advance 只能是空（不填），可以不填。'),
])
def test_brief_advance_sentence_unchanged(world, stage, allowed, expected):
    value = world['flow'].brief({**snapshot(world, stage), 'allowed_advances': allowed}, stage, world['target'])
    text = value if isinstance(value, str) else value.instructions
    assert expected in text
