"""每天读上游，每周发一次对账事件；游标与事件由核心同时确认。"""
import json
import subprocess
from dataclasses import replace

from workshop.core.models import Event, Poll
from .io import IO, write
from .calendar import period
from .model import digest, release_range, upstream, validate_software
from .checks import destination


# 事件和活的资料只留这些字段；发布说明正文写进文件，判断时按需读。
# 11-15 首跑：n8n 297 条、NetBird 客户端 117 条发布连同正文和 GitHub 元数据，
# 一件活的派活说明有 460 万字，超过 codex 单次输入上限（约 105 万字）。
KEEP = ('tag_name', 'name', 'html_url', 'published_at', 'prerelease', 'draft', 'notes_missing')


def slim(release):
    return {**{k: release[k] for k in KEEP if k in release}, 'notes_chars': len(release.get('body') or '')}


class Source:
    def __init__(self, io=None):
        self.io = io or IO()

    def wake_paths(self, target):
        return [str(self.io.state_file(target))]

    def normalize(self, batch, target):
        """上游游标仍按版本去重；入核心前才决定检查活的归属。"""
        state = self.io.state(target)
        check, active = destination(state)
        if not active and state.get('checks'):
            previous = next(reversed(state['checks']))
            if self.io.check_open(target, previous):
                check, active = previous, True
        events = []
        for event in batch.events:
            if event.body['kind'] == 'upgrade':
                events.append(event)
                continue
            if event.body['kind'] == 'wake' and not active:
                continue  # 已收尾项的旧 outbox 不开空的检查活。
            data = {**event.body, 'entry_key': event.object_key, 'resume': active}
            if data['kind'] in ('version', 'reconcile', 'failure'):
                record_id = digest([event.source, event.ext_id])
                write(self.io.file(target, 'state_dir') / 'events' / (record_id + '.json'), data)
                data = {k: data[k] for k in ('kind', 'entry_key', 'resume')}
                data['record_id'] = record_id
            events.append(replace(event, object_key=check, title='检查更新', body=data))
        return Poll(events, batch.cursor, normalized=True)

    def poll(self, target, cursor):
        c = self.io.config(target)
        if not c.get('enabled', False):
            return Poll([], cursor)
        old = json.loads(cursor) if cursor else {'seen': [], 'outbox': [], 'day': '', 'week': ''}
        day, week = period(c, self.io.clock())
        events = []
        state = self.io.state(target)
        if c.get('consolidate_on_start') and not state.get('checks_migrated'):
            return Poll([], cursor)  # 启动迁移成功前不确认新游标、不再生成旧粒度的活。

        def event(kind, key, payload, ext):
            events.append(Event('watch_updates', ext, target['name'], key,
                                payload.get('title', key), {'kind': kind, **payload}))

        for key, value in state['outbox'].items():
            if key not in old['outbox']:
                event(value.get('kind', 'wake'), value['object_key'], value, 'feedback:' + key)
                old['outbox'].append(key)
        if old['week'] != str(week):
            object_key = 'reconcile-' + str(week)
            event('reconcile', object_key, {'week': str(week), 'title': '每周盯更新对账 ' + str(week)}, object_key)
            for key, value in state['versions'].items():
                if value.get('status') not in ('done', 'failed'):
                    event('wake', key, {'title': value['software']['name'] + ' 继续等反馈'}, f'week:{week}:{key}')
            old['week'] = str(week)
        if old['day'] != str(day):
            try:
                software = self.io.watchlist(target)['software']
            except ValueError as exc:
                event('failure', 'watchlist', {'error': str(exc)}, digest(str(exc)))
                software = []
            batch = []
            for row in software:
                try:
                    validate_software(row)
                    if upstream(row)['kind'] == 'manual':
                        continue
                    releases = release_range(row, self.io.releases(target, row))
                    if not releases:
                        continue
                    key = row['id'] + '-' + digest(releases[-1]['tag_name'])
                    if key in old['seen']:
                        continue
                    batch.append(key)
                    notes = self.io.save_notes(target, key, releases)
                    event('version', key, {'software': row, 'releases': [slim(r) for r in releases], 'notes': notes,
                          'day': str(day), 'title': row['name'] + ' ' + releases[-1]['tag_name']}, 'version:' + key)
                    old['seen'].append(key)
                except (ValueError, OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
                    error = str(exc)
                    key = 'failure-' + row['id'] + '-' + digest(error)
                    if key not in old['seen']:
                        event('failure', key, {'error': error, 'software_id': row['id'],
                                              'title': row.get('name', row['id']) + ' 版本读取卡住'}, key)
                        old['seen'].append(key)
            for item in events:
                if item.body['kind'] in ('version', 'reconcile'):
                    item.body['batch'] = batch
                    item.body['discovered_at'] = self.io.now()
            old['day'] = str(day)
        return Poll(events, json.dumps(old))
