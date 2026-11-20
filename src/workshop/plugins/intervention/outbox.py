"""发件度量是介入点事实的同事务投影；这里不申请许可、排队或发送。"""
import json


_SCHEMA = '''CREATE TABLE message_outbox (
    id TEXT PRIMARY KEY NOT NULL, point TEXT NOT NULL, category TEXT NOT NULL,
    target TEXT NOT NULL, channel TEXT NOT NULL, sent_at TEXT,
    respondable INTEGER NOT NULL CHECK (respondable IN (0,1)),
    status TEXT NOT NULL CHECK (status IN ('delivered','folded','pending_digest','failed','unknown')),
    responded_at TEXT, resolved_after TEXT, item TEXT,
    items TEXT NOT NULL CHECK (json_type(items)='array'), version TEXT, digest_id TEXT
)'''


def member_items(row):
    # episode 是项目经理事项的一次发生，不是活号。系统故障、空车间可没有活号。
    return [f['value']['item'] for f in row.get('facts', [])
            if isinstance(f, dict) and isinstance(f.get('value'), dict)
            and isinstance(f['value'].get('item'), str) and f['value']['item']]


class Outbox:
    def __init__(self, store, target):
        self.store = store
        self.db = store.db
        self.target = target
        with store.transaction():
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_outbox'").fetchone():
                return
            self.db.execute(_SCHEMA)
            self.db.execute('CREATE INDEX message_outbox_response ON message_outbox(point,item,version)')
            self.db.execute('CREATE INDEX message_outbox_digest ON message_outbox(digest_id) WHERE digest_id IS NOT NULL')
            # 旧库只有 journal。依发生顺序回放，沿用原送达和作答时间、当时卡上的事项。
            members = {}
            for event, body, at in self.db.execute('SELECT kind,body,at FROM journal ORDER BY seq'):
                row = json.loads(body)
                if event == 'manager_updated':
                    members[row['episode']] = member_items(row)
                elif event == 'answered':
                    self.answered(row, at)
                elif row.get('payload') is not None and 'external' in row:
                    self.delivery(row, at, members)

    def delivery(self, row, at, members=None):
        key, state = row['id'], row['state']
        # queued/unsent/cancelled 都还没有成功发出；sending 有崩溃窗口，只能算不明。
        status = {'sent': 'delivered', 'sending': 'unknown', 'uncertain': 'unknown'}.get(state, 'failed')
        sent_at = row.get('sent_at', at) if status == 'delivered' else None
        changed = self.db.execute('''UPDATE message_outbox SET status=?,sent_at=COALESCE(sent_at,?)
                                     WHERE id=?''', (status, sent_at, key)).rowcount
        if not changed:
            payload = row.get('payload', {})
            permit = self.store.get(row['permit'], 'permit') if row.get('permit') else {}
            kind = payload.get('kind') or permit.get('point') or 'unknown'
            point = permit.get('point') or {'visual_receipt': 'visual', 'status_reply': 'manager_help'}.get(kind, kind)
            item = payload.get('item') or permit.get('owner')
            items = [item] if item else []
            if kind == 'manager_help' and 'members' in payload:
                if members is None:
                    members = {r['episode']: member_items(r) for r in self.store.all('manager')}
                items = list(dict.fromkeys(i for episode in payload['members'] for i in members.get(episode, [])))
                item = items[0] if items else None
            version = payload.get('version') or payload.get('report')
            respondable = kind in ('visual', 'update_report', 'manager_help')
            self.db.execute('''INSERT INTO message_outbox
                (id,point,category,target,channel,sent_at,respondable,status,item,items,version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (key, point, kind, self.target, 'feishu', sent_at,
                respondable, status, item, json.dumps(items, ensure_ascii=False), version))
        if status == 'delivered':
            self.db.execute('''UPDATE message_outbox SET status='folded',sent_at=?
                               WHERE digest_id=? AND status='pending_digest' ''', (sent_at, key))

    def answered(self, wait, at):
        item, point, version = wait['owner'], wait['point'], wait['content'].get('version')
        # 普通消息锁定问题版本。R12 卡的任一成员作答，计在整卡的第一条回应上。
        self.db.execute('''UPDATE message_outbox SET responded_at=?
            WHERE respondable=1 AND responded_at IS NULL AND status IN ('delivered','folded')
            AND sent_at<=? AND ((point=? AND item=? AND version=?) OR
              (point='manager_help' AND EXISTS (SELECT 1 FROM json_each(items) WHERE value=?)))''',
            (at, at, point, item, version, item))

    def clicked(self, delivery_id, at):
        self.db.execute('''UPDATE message_outbox SET responded_at=? WHERE id=? AND status='delivered'
                           AND respondable=1 AND responded_at IS NULL''', (at, delivery_id))

    def add_member(self, digest_id, id, *, point, category, item, version, respondable=True):
        """登记一个折叠成员；父送达失败或不明时保留在缓冲，不触发任何发送。"""
        with self.store.transaction():
            parent = self.db.execute('SELECT target,channel,sent_at,status FROM message_outbox WHERE id=? AND digest_id IS NULL',
                                     (digest_id,)).fetchone()
            if parent is None or id == digest_id:
                raise ValueError('摘要父送达无效')
            target, channel, sent, status = parent
            member = (digest_id, point, category, item, version, respondable)
            old = self.db.execute('SELECT digest_id,point,category,item,version,respondable FROM message_outbox WHERE id=?',
                                  (id,)).fetchone()
            if old:
                if old != member:
                    raise ValueError('折叠成员号已用于其他消息')
                return
            self.db.execute('UPDATE message_outbox SET respondable=0 WHERE id=?', (digest_id,))
            self.db.execute('''INSERT INTO message_outbox
                (id,digest_id,point,category,item,version,respondable,target,channel,items,sent_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (id, *member, target, channel, json.dumps([item], ensure_ascii=False),
                sent, 'folded' if status == 'delivered' else 'pending_digest'))
