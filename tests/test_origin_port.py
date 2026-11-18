"""查看工具只读流程的来源摘要；资料格式由流程负责。"""
from conftest import event, item
from workshop.plugins.issue_delivery import IssueDelivery
from workshop.reports.report import origin, overview
from workshop.reports.status_writer import StatusWriter


def material(title='标题', number=7, body='正文\n> Kun 原话\n> 第二句'):
    return {'body': {'repo': 'kun-g/workshop', 'number': number,
                     'data': {'title': title, 'html_url': f'https://github.com/kun-g/workshop/issues/{number}', 'body': body}}}


def test_issue_origin_keeps_last_title_ref_and_first_quote():
    flow = IssueDelivery()
    work = {'materials': [material('旧标题', 1), {'body': '普通资料'}, material()]}
    assert flow.origin(work, {}) == {'title': '标题', 'url': 'https://github.com/kun-g/workshop/issues/7',
                                     'ref': 'workshop#7', 'quote': 'Kun 原话'}
    assert flow.origin({'materials': []}, {}) is None
    assert flow.origin({'materials': [material(body='没有原话')]}, {})['quote'] == ''


def test_overview_and_background_writer_use_selected_flow(engine, monkeypatch):
    engine.ingest(event())
    expected = {'title': '其他来源', 'url': 'https://example.test/7', 'ref': '本地#7', 'quote': '原话'}
    calls = []

    def describe(work, target):
        calls.append((work['id'], target['name'], target['plugin']))
        return expected

    flow = engine.plugins.get('flow', 'flow')
    monkeypatch.setattr(flow, 'origin', describe, raising=False)
    view = overview(engine.log, plugins=engine.plugins, rules=engine.rules.data)
    assert view['rows'][0]['origin'] == expected
    assert calls == [('alpha:one', 'alpha', 'flow')]
    writer = StatusWriter(engine.log.path, clock=engine.clock, plugins=engine.plugins)
    writer.refresh()
    html = (engine.log.path.parent / 'status.html').read_text()
    assert '其他来源' in html and 'https://example.test/7' in html


def test_missing_or_broken_origin_does_not_hide_item(engine, monkeypatch):
    engine.ingest(event())
    flow = engine.plugins.get('flow', 'flow')
    for value in (None, 'bad', {'title': 'partial'}):
        monkeypatch.setattr(flow, 'origin', lambda *args: value, raising=False)
        assert origin(item(engine), {'name': 'alpha'}, flow) is None

    def broken(*args):
        raise RuntimeError('摘要不可用')

    monkeypatch.setattr(flow, 'origin', broken)
    view = overview(engine.log, plugins=engine.plugins, rules=engine.rules.data)
    assert len(view['rows']) == 1 and view['rows'][0]['origin'] is None
    assert origin(item(engine), {}, object()) is None
