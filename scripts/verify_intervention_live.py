#!/usr/bin/env python3
"""#173 上线后只读检查。只读现役库、服务和本地版本；不访问 GitHub。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sqlite3
import sys

from workshop.core.log import Log
from workshop.core.rules import Rules
from workshop.registry import Registry
from workshop.reports.report import status, overview


def read(path):
    return json.loads(Path(path).read_text())


def resident_path(value, state):
    """检查副本的 HOME 是临时目录；现役规则的 ~ 属于状态目录的拥有者。"""
    if value.startswith('~/'):
        return Path(pwd.getpwuid(state.stat().st_uid).pw_dir) / value[2:]
    return Path(value)


def release_start(state):
    release = read(state / 'release.json')
    if release.get('status') != 'ok' or not release.get('confirmed_at'):
        raise ValueError('本次发版还没确认成功，不能把空窗口算通过')
    at = datetime.fromisoformat(release['at'])
    if at.tzinfo is None or at > datetime.now(timezone.utc) or not release.get('to'):
        raise ValueError('发版窗口或提交号无效')
    return release['at'], release['to']


def inspect_intake(log, target, since):
    problems, checked = [], []
    label = target.get('label', 'Ready for Agent')
    labels = {}
    rows = log.db.execute("SELECT seq,item,json_extract(body,'$.body') FROM facts WHERE kind='event' "
        "AND source='github' AND json_extract(body,'$.body.kind')='label' ORDER BY seq")
    for seq, item_id, raw in rows:
        body = json.loads(raw)
        if body.get('label') == label:
            labels[item_id] = {'seq': seq, 'actor': body.get('actor')}
    for item in log.all('items'):
        if item['target'] != target['name'] or item['created'] < since or not item['object_key'].isdigit():
            continue
        record = labels.get(item['id'])
        checked.append({'item': item['id'], 'label_event': record})
        allowed = [target.get('kun_github', 'kun-g'), *target.get('intake_app_logins', [])]
        if not record or record['actor'] not in allowed:
            problems.append(f"缺用户或指定 App 的 Ready for Agent 标签事件：{item['id']}")
    return {'repo': target['repo'], 'items': checked, 'problems': problems}


def inspect_authorizations(log, rules, since):
    for name, target in rules.data['targets'].items():
        if 'frozen_paths' in target:
            raise ValueError('现役规则仍有冻结路径：' + name)
    checked = []
    for fact in log.selected_facts(('gate_passed',), ('gate_stage', 'actions', 'action_targets', 'authorized_actions')):
        if fact['kind'] != 'gate_passed' or fact['at'] < since:
            continue
        body = fact['body']
        if body.get('gate_stage') not in ('交付', '验收'):
            continue
        required = set(body.get('actions', [])) & set(rules.data['ask_first']) - set(rules.data['skip_ask'])
        for action in required:
            candidates = [r for r in body.get('authorized_actions', []) if r.get('action') == action
                and r.get('request') and r.get('target') == body.get('action_targets', {}).get(action)
                and r.get('target') and r.get('result') == 'approved' and r.get('applied') is True]
            if not candidates:
                raise ValueError('授权关口缺原申请及生效证据：' + fact['item'])
            checked.append({'item':fact['item'], 'action':action, 'verification':candidates})
    return checked


def inspect_deliveries(rules, state, endpoint, since):
    config = rules.data.get('intervention', {})
    if not config.get('transport'):
        raise ValueError('送达命令未接上')
    for point in ('visual', 'update_report'):
        if config.get('points', {}).get(point, {}).get('send') is not True:
            raise ValueError(point + ' 未允许约定送达')
    # socket 的父目录是 run；服务自己的库与其并列，绝不打开成可写连接。
    database = Path(endpoint).parent.parent / 'intervention.db'
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        records = {key: json.loads(body) for key, body in db.execute('SELECT id,body FROM records')}
        attempts = {}
        # 送达命令起不来记 unsent：那一次确定没发出，照原号重发不算第二次发送。
        for kind, key in db.execute("SELECT kind,id FROM journal WHERE at>=? AND kind IN ('sending','unsent')", (since,)):
            attempts[key] = attempts.get(key, 0) + (1 if kind == 'sending' else -1)
        checked = []
        for record in records.values():
            if record.get('permit') is None or record.get('created_at', '') < since:
                continue
            permit = records[record['permit']]
            if permit['point'] not in ('visual', 'update_report'):
                continue
            if attempts.get(record['id'], 0) > 1:
                raise ValueError('同一送达号调用发送超过一次：' + record['id'])
            payload = record['payload']
            if permit['point'] == 'update_report' and (payload.get('kind') != 'update_report'
                    or not str(payload.get('artifact', '')).endswith('.html')
                    or 'text' in payload or 'summary' in payload or not payload.get('sha256')):
                raise ValueError('报告不是单一 HTML 附件：' + record['id'])
            checked.append({'id':record['id'], 'point':permit['point'], 'state':record['state'],
                            'send_calls':attempts.get(record['id'], 0)})
    for target in rules.data['targets'].values():
        watch = target.get('watch_updates') or {}
        if not watch.get('state_dir'):
            continue
        directory = resident_path(watch['state_dir'], state)
        for path in directory.glob('delivery-*.json'):
            journal = read(path)
            began = journal.get('intervention', {}).get('created_at') or journal.get('file', {}).get('started', '')
            if began < since:
                continue
            reference = journal.get('intervention', {}).get('delivery')
            if reference not in records:
                raise ValueError('报告送达未经过介入点服务：' + str(path))
    return {'transport':config['transport'], 'deliveries':checked}


def inspect_busy(log, state):
    from types import SimpleNamespace
    from workshop.core.activity import snapshot
    # 快照被下一拍替换就重读；不把跨拍采样差算成漏计。
    for _ in range(8):
        before = read(state / 'heartbeat.json')
        now = datetime.now(timezone.utc).isoformat()
        actual = snapshot(SimpleNamespace(log=log, active=lambda: log.all('rounds', unless=('status', 'ended'))), before['business'], now)
        after = read(state / 'heartbeat.json')
        if before == after:
            break
    else:
        raise ValueError('业务快照一直变化，尚未取得一致采样')
    for field in ('agents', 'jobs', 'total', 'round_tokens', 'job_ids'):
        observed, expected = actual[field], before['business'][field]
        if isinstance(observed, list):
            observed, expected = sorted(observed), sorted(expected)
        if observed != expected:
            raise ValueError('业务计数不符：' + field)
    return {'heartbeat': before['business'], 'observed': actual}


def inspect_managers(log, state, rules, plugins):
    from workshop.reports.manager_status import read_managers, sections, HEADINGS
    rows = read_managers(state)
    ids = [r['id'] for r in rows]
    tokens = [r['token'] for r in rows if r.get('token')]
    sessions = [(r['executor'], r['started']['session']) for r in rows if r.get('token') and r.get('started')]
    if len(ids) != len(set(ids)) or len(tokens) != len(set(tokens)) or len(sessions) != len(set(sessions)):
        raise ValueError('同事项或会话重复归属')
    normal = {r['token'] for r in log.all('rounds', unless=('status', 'ended'))}
    if normal.intersection(tokens):
        raise ValueError('项目经理占用普通轮次')
    projected = sections(overview(log, rules=rules.data, plugins=plugins))
    if set(projected) != set(HEADINGS):
        raise ValueError('项目经理栏目缺失')
    return {'managers': rows, 'ordinary_tokens': sorted(normal), 'sections': projected}


def manager_deliveries(rules, state):
    endpoint = resident_path(rules.data['wait_services']['intervention']['endpoint'], state)
    path = endpoint.parent.parent / 'intervention.db'
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT body FROM records WHERE kind='delivery'")]
        permits = {r['id']: r for r in (json.loads(x[0]) for x in db.execute("SELECT body FROM records WHERE kind='permit'"))}
    return rows, permits


def inspect_pushes(log, state, rules, since):
    watch = read(state / 'watch.json') if (state / 'watch.json').exists() else {}
    if any(r.get('via') in ('ask-kun', 'attention') and r.get('since', '') >= since for r in watch.get('active', {}).values()):
        raise ValueError('看门仍直接推送')
    source = (Path(__file__).resolve().parents[1] / 'src/workshop/watch/__init__.py').read_text()
    if 'notify' in source or 'attention' in source:
        raise ValueError('看门含旧推送出口')
    for fact in log.selected_facts(('card_created',), ('mode', 'key')):
        if fact['at'] >= since and fact['body'].get('mode') == 'notice' and fact['body'].get('key', '').startswith(('plugin:', 'failing:', 'pending:', 'reconcile:', 'undeliverable:')):
            raise ValueError('仍创建通用故障通知')
    deliveries, permits = manager_deliveries(rules, state)
    scopes = []
    checked = []
    for d in deliveries:
        if d['payload'].get('kind') != 'manager_help' or d['state'] != 'sent':
            continue
        permit = permits[d['permit']]
        if permit['point'] != 'manager_help':
            raise ValueError('求助未走指定介入点')
        scopes.append(permit['scope'])
        checked.append({'id': d['id'], 'scope': permit['scope'], 'external': d['external']})
    if len(scopes) != len(set(scopes)):
        raise ValueError('同范围重复求助')
    return {'since_release': since, 'sent': checked}


def inspect_button(log, state, rules, plugins):
    from workshop.reports.manager_status import sections, HEADINGS
    from workshop.reports.status_page import page
    from workshop.plugins.intervention.manager import reply_card
    view = overview(log, rules=rules.data, plugins=plugins)
    terminal, html = status(log, rules=rules.data, plugins=plugins), page(view)
    for heading in HEADINGS:
        if heading not in terminal.splitlines() or '<h2>' + heading + '</h2>' not in html:
            raise ValueError('终端与网页栏目不一致：' + heading)
    deliveries, _ = manager_deliveries(rules, state)
    for row in deliveries:
        if row['payload'].get('kind') == 'manager_help' and row['payload'].get('section') not in HEADINGS:
            raise ValueError('已发送按钮指向未知栏目')
    cards = [reply_card(s, 'dry-run') for s in sections(view).values()]
    return {'terminal_headings': list(HEADINGS), 'html_headings': list(HEADINGS), 'dry_run_cards': cards}


def verify(args):
    state = args.state_dir.resolve()
    since, version = release_start(state)
    with closing(Log.readonly(state / 'workshop.db')) as log:
        saved = log.get('controls', '@rules')
        rules = Rules.load(Path(saved['path']))
        for config in rules.data.get('wait_services', {}).values():
            if isinstance(config.get('endpoint'), str):
                config['endpoint'] = str(resident_path(config['endpoint'], state))
        plugins = Registry()
        if args.check in ('busy-count', 'managers', 'pushes', 'button'):
            if args.check == 'busy-count':
                value = inspect_busy(log, state)
            elif args.check == 'managers':
                value = inspect_managers(log, state, rules, plugins)
            elif args.check == 'pushes':
                value = inspect_pushes(log, state, rules, since)
            else:
                value = inspect_button(log, state, rules, plugins)
            print(json.dumps(value, ensure_ascii=False))
        elif args.check == 'status':
            text = status(log, rules=rules.data, plugins=plugins)
            print(text)
            if not all(s in text for s in ('优先级', '等你', '推荐', '等了多久')):
                raise ValueError('status 缺栏目')
            view = overview(log, rules=rules.data, plugins=plugins)
            for wait in view['waits']:
                if 'record' not in wait or not wait['question'] or wait['recommend'] == '—' or wait['seconds'] is None:
                    raise ValueError('等待内容或起始时间读不到：' + wait['ref'])
        elif args.check == 'no-cards':
            sent = []
            for fact in log.selected_facts(('card_changed',), ('id', 'sent')):
                if fact['at'] < since or fact['kind'] != 'card_changed' or not fact['body'].get('sent'):
                    continue
                card = log.get('cards', fact['body']['id'])
                if not card:
                    raise ValueError('发送事实缺对应卡记录')
                key = card.get('payload', {}).get('key', '')
                if card['mode'] == 'notice' or not (card.get('core_approval') or card.get('billing_configs') or key == 'visual-acceptance'):
                    sent.append({'at': fact['at'], 'id': card['id'], 'mode': card['mode']})
            print(json.dumps({'since_release': since, 'release': version, 'ordinary_sends': sent}, ensure_ascii=False))
            if sent:
                raise ValueError('本次发版后仍发送了普通问卡或提醒')
        elif args.check == 'no-approval-cards':
            cards = []
            for fact in log.selected_facts(('card_created',), ('id', 'core_approval', 'billing_configs', 'frozen_files')):
                if fact['at'] < since or fact['kind'] != 'card_created':
                    continue
                card = fact['body']
                if card.get('core_approval') or card.get('billing_configs') or card.get('frozen_files'):
                    cards.append(card['id'])
            print(json.dumps({'since_release':since, 'created_approval_cards':cards}, ensure_ascii=False))
            if cards:
                raise ValueError('本次发版后仍建过车间授权卡')
        elif args.check == 'authorization':
            print(json.dumps({'since_release':since, 'authorizations':inspect_authorizations(log, rules, since)}, ensure_ascii=False))
        elif args.check == 'deliveries':
            for fact in log.selected_facts(('card_created',), ('key',)):
                if fact['at'] >= since and 'visual-acceptance' in fact['body'].get('key', ''):
                    raise ValueError('仍用旧亲眼看问卡送达')
            endpoint = rules.data['wait_services']['intervention']['endpoint']
            print(json.dumps(inspect_deliveries(rules, state, endpoint, since), ensure_ascii=False))
        elif args.check == 'intake':
            results = [inspect_intake(log, rules.target(name), since)
                       for name, target in rules.data['targets'].items() if target.get('auto_intake')]
            print(json.dumps(results, ensure_ascii=False, indent=2))
            if not results or any(r['problems'] for r in results):
                raise ValueError('接单检查未通过')
        else:
            config = rules.data['wait_services']['intervention']
            client = plugins.get('wait', 'intervention')
            client.validate(config)
            result = client.invoke('ping', {}, config)
            digest = hashlib.sha256(Path(rules.path).read_bytes()).hexdigest()
            if result.get('rules_sha256') != digest or Path(result.get('rules', '')).resolve() != Path(rules.path).resolve():
                raise ValueError('现役介入点服务未用现役规则通过自检')
            transport = rules.data.get('intervention', {}).get('transport') or []
            lark = transport[transport.index('--lark') + 1] if '--lark' in transport else 'lark-cli'
            found = shutil.which(lark, path=result.get('path') or '')
            if not found:
                raise ValueError(f'介入点服务进程的 PATH 里找不到 {lark}：{result.get("path")!r}')
            result = {**result, 'lark': found}
            heartbeat = read(state / 'heartbeat.json')
            if (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat['at'])).total_seconds() > 300:
                raise ValueError('守护进程心跳已过期')
            process = heartbeat['process']
            os.kill(process['pid'], 0)
            running = process.get('version')
            checkout = resident_path(rules.target('workshop')['checkout'], state)
            checked = subprocess.check_output(['git', '-C', str(Path(__file__).resolve().parents[1]), 'rev-parse', 'HEAD'], text=True).strip()
            command = ['git', '-C', str(checkout), 'merge-base', '--is-ancestor', checked, running or 'missing']
            result_check = subprocess.run(command, capture_output=True, text=True)
            print(json.dumps({'service': result, 'daemon': process, 'checked_commit': checked,
                              'argv': command, 'exit_code': result_check.returncode}, ensure_ascii=False))
            if result_check.returncode:
                raise ValueError('守护进程的版本不包含本次核验提交')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('check', choices=['no-cards', 'no-approval-cards', 'authorization', 'deliveries', 'status', 'intake', 'service', 'busy-count', 'managers', 'pushes', 'button'])
    parser.add_argument('--since-release', action='store_true', help='从本次成功发版起查；四项检查均采用此窗口')
    parser.add_argument('--state-dir', type=Path, default=Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/workshop')
    args = parser.parse_args()
    try:
        verify(args)
        print('检查通过：' + args.check)
        return 0
    except Exception as exc:
        print(type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
