"""执行轮把原申请和检查点交回后，程序查原申请，核实后再唤起执行轮。"""
from workshop.authorization import read, validate, target_key
from workshop.core.models import After


def register(io, item, target, spec):
    validate(spec)
    with io.locked(item, target) as state:
        records = state.setdefault('authorizations', {})
        old = records.get(spec['checkpoint'])
        if old and old['spec'] != spec:
            raise ValueError('原授权检查点不能换申请、动作或目标')
        records.setdefault(spec['checkpoint'], {'spec': spec})
        io.save(item, target, state)
        return records[spec['checkpoint']].get('verification', {}).get('applied') is not True


def after(io, item, target, spec):
    register(io, item, target, spec)
    client = target.get('authorization_client')
    if not client:
        return After(blocked='未配置授权中心只读客户端', next='补齐该批次授权核实前置')
    with io.locked(item, target) as state:
        record = state['authorizations'][spec['checkpoint']]
        if record.get('verification', {}).get('applied') is True:
            return After(pending=True, rerun=True, reason='原申请已核实生效', next='从检查点继续；已代执行的动作不得重做')
        result = read(record['spec'], client, url=target.get('authorization_url'))
        record.update(result)
        io.save(item, target, state)
    outcome = result['verification']['result']
    reason = f"授权中心 {spec['request']}：{outcome}；检查点 {spec['checkpoint']}"
    if outcome == 'approved':
        return After(pending=True, rerun=True, reason=reason, next='从持久检查点继续；核实执行记录，不重复已执行动作')
    if outcome == 'unknown' and (spec['kind'] != 'exec' or spec['provider_action'] != 'argocd/sync_app'
            or spec['action'] not in ('集群同步', '有别人在用的交付对象上线')):
        return After(blocked='授权中心核实器缺该动作类别的生效验证能力：' + spec['action'],
                     next='补齐对应授权类别；不得借其它动作申请放行')
    if outcome in ('unknown', 'pending'):
        return After(pending=True, reason=reason, next=f"只查询原申请 {spec['request']}，不重新申请", recheck_after='+5m')
    return After(blocked=reason, next='原申请未获有效授权；不执行，不重新申请或扩大权限')


def evidence(state, target, revision):
    records = list(state.get('authorizations', {}).values())
    verified = [r['verification'] for r in records if r.get('verification')]
    # 当前目标来自规则和当前交付版本，不能从旧授权记录复制目标再与自身比较。
    deployment = target.get('deployment') or {}
    action = target.get('delivery_action')
    expected = {}
    if action == '有别人在用的交付对象上线' and deployment.get('app') and revision:
        expected[action] = target_key({'instance':target.get('grant_instance', 'central'),
                                      'app':deployment['app'], 'revision':revision})
    return {'action_targets': expected, 'authorized_actions': verified}
