"""检查更新整体一件活；判断逐项做，升级按软件另开活。"""
from copy import deepcopy
from dataclasses import replace
import re

from workshop.core.models import After, Classification, Evidence, Receipt, FlowBrief
from .brief import judgment_brief
from .checks import candidate, destination, hydrate, incoming_feedback
from .entries import Entries, result, request
from .feedback import SOURCES, feedback, read_message
from .model import digest, validate_judgment
from .calendar import period


class Flow(Entries):
    @staticmethod
    def legacy(item):
        return not re.fullmatch(r'check(?::\d+)?', item['object_key']) and not Flow.upgrade(item)

    def snapshot(self, item, target):
        state = self.io.state(target)
        return state, hydrate(state, item, self.io.file(target, 'state_dir') / 'events')

    @staticmethod
    def upgrade(item):
        return item['object_key'].startswith('upgrade:')

    def automatic(self, item, stage, target):
        if self.legacy(item) or self.upgrade(item) or stage != '执行':
            return True
        state, check = self.snapshot(item, target)
        return incoming_feedback(item, state) or candidate(state, check) is None

    def classify(self, event, item, target, find_external):
        if event.source not in SOURCES:
            return Classification(discard=True)
        text, _ = read_message(self.io, target, event.source, event.ext_id, event.body)
        if text is None:
            return Classification(discard=True, summary='反馈无法回查或发送者不是 Kun')
        try:
            state = self.io.state(target)
            if request(text):
                anchor, _ = destination(state)
            else:
                value = feedback(text, state)
                anchor = state['reports'][value['report']]['anchor']
                if state.get('checks', {}).get(anchor, {}).get('closed'):
                    anchor, _ = destination(state)
            found = find_external(object_key=anchor)
            return Classification('rework' if found else 'ready', anchor, summary='Kun 的盯更新反馈')
        except (ValueError, TypeError, KeyError):
            return Classification(discard=True, summary='反馈格式或归属无效；正文只当资料')

    def brief(self, item, stage, target):
        state, check = self.snapshot(item, target)
        selected = candidate(state, check)
        if not selected:
            raise ValueError('没有待判断或待对账的资料')
        key, data = selected
        entry = {**item, 'object_key': key, 'materials': [{'source': 'watch_updates', 'body': data}]}
        if data['kind'] == 'reconcile':
            brief = super().brief(entry, '说清楚', target)
        else:
            instructions, materials = judgment_brief(state['versions'][key], self.io.needs(target), self.io.directions(target))
            brief = FlowBrief(instructions, workdir=self.io.workdir(target, 'reader'), readonly=True,
                              materials=[{'source': 'watch_updates_snapshot', 'body': materials}])
        instructions = brief.instructions.replace('advance=执行', 'advance=证据')
        instructions += ('\n本轮只处理下面指定的一项，整件检查活由程序继续。'
                         f'回执 evidence 的 watch-updates/v1 JSON 必须带 task_key="{key}"；advance=证据。'
                         '取不到所需资料时仍交 outcome=done、advance=证据，并在 evidence JSON 写 '
                         'blocked={reason:原因,next:建议动作}，程序将原因记在本项并继续其他项。')
        return replace(brief, instructions=instructions)

    def assess(self, item, receipt, target):
        if self.legacy(item):
            return Evidence(blocked='旧盯更新活尚未迁移', next='核对旧活与迁移备份，由项目经理处理')
        if self.upgrade(item):
            return Evidence(passed=True, checks=self.checks(target), reason='已登记选中的软件、部署、版本与原话')
        try:
            state, check = self.snapshot(item, target)
            if not self.automatic(item, item['stage'], target):
                supplied = result(receipt)
                key = supplied.get('task_key')
                if key not in check['entries'] or check['entries'][key]['kind'] not in ('version', 'reconcile'):
                    raise ValueError('回执 task_key 不匹配本轮待处理项')
                if supplied.get('blocked'):
                    if not all(isinstance(supplied['blocked'].get(k), str) and supplied['blocked'][k].strip()
                               for k in ('reason', 'next')):
                        raise ValueError('本项阻塞须有原因和下一步')
                elif check['entries'][key]['kind'] == 'reconcile':
                    self.inventory(supplied['inventory'], target)
                else:
                    current = state['versions'][key]
                    validate_judgment(supplied['judgment'], current['software'], current['releases'],
                                      self.io.needs(target), self.io.directions(target))
            return Evidence(passed=True, checks=self.checks(target), reason='检查活逐项资料与反馈已核对')
        except (ValueError, KeyError, TypeError) as exc:
            return Evidence(reason=str(exc))

    def apply_task(self, item, target, state, check):
        try:
            supplied = result(Receipt(**item.get('result', {})))
        except (ValueError, TypeError):
            return
        key = supplied.get('task_key')
        if key not in check['entries']:
            return
        entry = check['entries'][key]
        if supplied.get('blocked'):
            entry['blocked'] = supplied['blocked']
        elif entry['kind'] == 'reconcile' and not state['weeks'].get(key, {}).get('reconciled'):
            self.apply_inventory({**item, 'object_key': key}, target, state)
            # 新对账提供了补充资料，可以重新判断先前失败的项。
            for row in check['entries'].values():
                if row['kind'] == 'version':
                    row.pop('blocked', None)
        elif entry['kind'] == 'version' and supplied.get('judgment'):
            current = state['versions'][key]
            current['judgment'] = validate_judgment(supplied['judgment'], current['software'], current['releases'],
                                                  self.io.needs(target), self.io.directions(target))
            current['needs_judgment'] = False
            entry.pop('reported', None)
            entry['day'] = str(period(self.io.config(target), self.io.clock())[0])
            current['feedback'] = {p: v for p, v in current['feedback'].items() if v['decision'] != '判错了'}
            current['status'] = 'waiting'

    def settle_choices(self, state, check):
        """升级请求有持久去重键；先落入 outbox，再由来源送进核心。"""
        for key, data in check['entries'].items():
            if data['kind'] != 'version':
                continue
            current = state['versions'][key]
            choices = current.get('feedback', {})
            for place, choice in choices.items():
                if choice['decision'] != '升':
                    continue
                software = current['software']['id']
                code = 'upgrade-' + digest([key, place, choice])
                state['outbox'].setdefault(code, {
                    'kind': 'upgrade', 'object_key': 'upgrade:' + software,
                    'title': current['software']['name'] + ' 待升级', 'software': software,
                    'version_key': key, 'version': current['releases'][-1]['tag_name'],
                    'instance': deepcopy(next(p for p in current['software']['instances'] if p['id'] == place)),
                    'choice': deepcopy(choice), 'check': current['check']})
            if not current.get('needs_judgment'):
                expected = {p['id'] for p in current['software']['instances']}
                if expected and all(choices.get(p, {}).get('decision') in ('升', '没问题', '不用盯') for p in expected):
                    pending_features = any(t['kind'] == 'feature' and t['key'] == key
                        and t['instance'] + ':' + t['feature'] not in current.get('feature_feedback', {})
                        for report in state['reports'].values() for t in report.get('feedback_targets', {}).values())
                    current['status'] = 'waiting' if pending_features else 'done'

    def adopt_feedback(self, item, target, state, check):
        """旧报告的新反馈进当前活动检查活，不能复活另一件检查活。"""
        for material in item.get('materials', []):
            source, ext = material.get('source'), material.get('ext_id')
            if source not in SOURCES or not ext or source + ':' + ext in state['consumed']:
                continue
            text, _ = read_message(self.io, target, source, ext, material.get('body'))
            if text is None or request(text):
                continue
            for row in feedback(text, state)['items']:
                key = row['key']
                current = state['versions'][key]
                check['entries'].setdefault(key, {k: deepcopy(v) for k, v in current.items()
                                                  if k in ('kind', 'software', 'releases', 'day', 'notes', 'batch', 'discovered_at')})
                current['check'] = item['object_key']

    def reports(self, item, target, state, check):
        for key, data in check['entries'].items():
            if data['kind'] not in ('version', 'reconcile') or data.get('reported'):
                continue
            payload = {**data, 'week_key': key}
            outcome = self.report(item, target, state, payload)
            if outcome.pending:
                return outcome
            data['reported'] = True
            if data['kind'] == 'reconcile':
                for failure in check['failures'].values():
                    failure['reported'] = True
        return After()

    def after(self, item, completed_stage, key, target, services=None):
        if self.upgrade(item):
            if completed_stage == '验收':
                return After(pending=True, reason='升级执行未实现；保留全部选择与原话',
                             next='待升级', recheck_after='+7d')
            return After()
        with self.io.locked(target):
            state, check = self.snapshot(item, target)
            try:
                if completed_stage == '执行':
                    self.apply_task(item, target, state, check)
                    self.adopt_feedback(item, target, state, check)
                    self.consume_feedback(item, target, state)
                    self.settle_choices(state, check)
                    if candidate(state, check) or incoming_feedback(item, state):
                        # 达到原有报告截止时间时先发已判断项；等待报告不挡下一项判断。
                        self.reports(item, target, state, check)
                        return After(pending=True, rerun=True, reason='检查活还有待处理项', next='继续本件检查活的下一项')
                    if any(v.get('kind') == 'reconcile' and v.get('check') == item['object_key']
                           and v['object_key'] not in check['entries'] for v in state['outbox'].values()):
                        return After(pending=True, reason='清单调整已登记为对账事件', next='等对账事件进入本件检查活', recheck_after='+5m')
                if completed_stage == '交付':
                    return self.reports(item, target, state, check)
                if completed_stage == '验收':
                    waiting = [k for k, d in check['entries'].items() if d['kind'] == 'version'
                               and state['versions'][k].get('status') != 'done']
                    needs = {n['id']: n for n in self.io.needs(target)}
                    pending_needs = {n for report in state['reports'].values() if report.get('anchor') == item['object_key']
                                     for n in report.get('needs', []) if not needs.get(n, {}).get('confirmed')}
                    pending_features = {(t['key'], t['instance'], t['feature'])
                        for report in state['reports'].values() if report.get('anchor') == item['object_key']
                        for t in report.get('feedback_targets', {}).values() if t['kind'] == 'feature'
                        and t['instance'] + ':' + t['feature'] not in state['versions'][t['key']].get('feature_feedback', {})}
                    unreported = any(not v.get('reported') for v in check['failures'].values())
                    if waiting or pending_needs or pending_features or unreported:
                        return After(pending=True, reason='报告项须有明确反馈，未反馈跨周保留',
                                     next=f'等反馈：{len(waiting)} 个版本、{len(pending_needs)} 条需要、{len(pending_features)} 项新功能',
                                     recheck_after='+7d')
                if completed_stage == '收尾':
                    if candidate(state, check) or incoming_feedback(item, state):
                        state['outbox']['close-race-' + digest([item['object_key'], item.get('cause')])] = {
                            'object_key': item['object_key'], 'title': '收尾前收到新资料，继续检查'}
                        return After(pending=True, reason='收尾前有新资料', next='继续处理新资料', recheck_after='+5m')
                    check['closed'] = True
                return After()
            except (ValueError, OSError) as exc:
                return After(blocked=str(exc), next='核对本件检查活的资料与文件事务后恢复')
            finally:
                self.io.save(target, state)
