"""按交付对象展开流程配置和匹配来源事实。"""
import re


def entries(target):
    return target.get('flows') or {target['plugin']: {}}


def default_flow(target):
    return target.get('default_flow') or target.get('plugin') or next(iter(entries(target)))


def configured(name, target, flow=''):
    flow = flow or default_flow(target)
    if flow not in entries(target):
        raise ValueError(f'{name} 未配置流程 {flow}')
    local = entries(target)[flow]
    return {**target, **local, 'name': name, 'flow': flow,
            'plugin': local.get('plugin', flow), 'flow_default': flow == default_flow(target)}


def matches(body, when):
    if not when:
        return True
    if not isinstance(body, dict):
        return False
    def equal(actual, expected):
        if isinstance(expected, list):
            return any(equal(actual, value) for value in expected)
        if isinstance(expected, str) and expected.startswith('^'):
            return isinstance(actual, str) and re.search(expected, actual) is not None
        return type(actual) is type(expected) and actual == expected
    return all(key in body and equal(body[key], value) for key, value in when.items())


def validate(name, target):
    if 'flows' not in target:
        if not isinstance(target.get('plugin'), str) or not target['plugin']:
            raise ValueError(f'{name} 缺流程插件')
        return
    flows = target['flows']
    if not isinstance(flows, dict) or not flows:
        raise ValueError(f'{name} flows 须为非空对象')
    for flow, config in flows.items():
        prefix = f'{name} 流程 {flow}'
        if not isinstance(flow, str) or not flow or ':' in flow or not isinstance(config, dict):
            raise ValueError(f'{prefix} flows 名字或配置无效')
        if not isinstance(config.get('plugin', flow), str) or not config.get('plugin', flow):
            raise ValueError(f'{prefix} plugin 无效')
        triggers = config.get('triggers')
        if not isinstance(triggers, list):
            raise ValueError(f'{prefix} triggers 须为列表')
        for trigger in triggers:
            if (not isinstance(trigger, dict) or set(trigger) - {'source', 'when', 'action'}
                    or not isinstance(trigger.get('source'), str) or not trigger['source']
                    or trigger.get('action') not in ('ready', 'rework', 'update')):
                raise ValueError(f'{prefix} triggers 的 source/action/字段无效')
            when = trigger.get('when', {})
            if not isinstance(when, dict) or not all(isinstance(k, str) and k for k in when):
                raise ValueError(f'{prefix} when 须为顶层字段对象')
            for field, expected in when.items():
                values = expected if isinstance(expected, list) else [expected]
                for value in values:
                    if value is not None and type(value) not in (str, bool, int, float):
                        raise ValueError(f'{prefix} when.{field} 须为标量或标量列表')
                    if isinstance(value, str) and value.startswith('^'):
                        try:
                            re.compile(value)
                        except re.error as exc:
                            raise ValueError(f'{prefix} when.{field} 正则无效：{exc}') from exc
    if default_flow(target) not in flows:
        raise ValueError(f'{name} default_flow 不在 flows 中')
