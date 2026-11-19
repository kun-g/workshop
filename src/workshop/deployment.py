"""装配规则：读仓库规则、叠部署目录、交核心校验（核心 R16、R18、R25，#293）。

部署目录默认 ~/.config/workshop，环境变量 WORKSHOP_DEPLOYMENT 可改，设成空或 none 就不叠加：

    deployment.yaml          可选；orca（不写按 true）、roles（写了的角色以它为准）、
                             ladder（执行者阶梯：做不下来的活逐级换更强的接着做，ws#301）
    executors/<名字>/
        executor.yaml        配置档定义，字段和规则里的配置档一样
        settings.json        可选；和 rules.md 有一个就隔离启动，读这个目录
        rules.md             可选

目录名就是执行者名，和仓库里同名的整个替换。这里只装配数据，不配置驱动；
驱动配置由 registry.load_rules 或各入口自己的 Registry 在返回结果上做。
"""
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path

import yaml

from .core import ladder
from .core.rules import Rules

ENVIRONMENT = 'WORKSHOP_DEPLOYMENT'
DEFAULT = '~/.config/workshop'
OFF = ('', 'none')
REPOSITORY, DEPLOYMENT = '仓库', '部署'
SETTINGS_FILE = 'deployment.yaml'
DEFINITION = 'executor.yaml'
ISOLATION_FILES = ('settings.json', 'rules.md')
# 部署目录里的 roles 名对应仓库规则里的位置：writer/reviewer/acceptor 是 executors 下的候选，managers 只换候选名单。
ROLES = ('writer', 'reviewer', 'acceptor', 'managers')
# 执行环境：不写按 Orca 的终端；部署关掉 Orca 时一律后台。
ORCA, HEADLESS = 'orca', 'headless'
# 装配函数自己写进配置档的字段，executor.yaml 里不许写。
RESERVED = ('executor_dir', 'revision')


@dataclass(frozen=True)
class Assembly:
    rules: Rules
    # 每个配置档来自哪里：仓库或部署（同名替换算部署）。不写进配置档，免得改变会话接回和对比表分版。
    origins: dict = field(default_factory=dict)
    # 实际叠上去的部署目录；没有叠加时为 None。
    directory: Path | None = None
    # 同名替换后从隔离变成不隔离的执行者。
    unisolated: tuple = ()


def directory(value=None):
    """要叠加的部署目录；value 不给时看 WORKSHOP_DEPLOYMENT，再不给用默认位置。空或 none 表示不叠加。"""
    if value is None:
        value = os.environ.get(ENVIRONMENT, DEFAULT)
    if str(value).strip().lower() in OFF:
        return None
    return Path(value).expanduser().resolve()


def revision(path):
    """部署目录里一个执行者的内容版本：executor.yaml、settings.json、rules.md 任何一个变了就不同。"""
    digest = hashlib.sha256()
    for name in (DEFINITION, *ISOLATION_FILES):
        file = Path(path) / name
        if file.is_file():
            content = file.read_bytes()
            digest.update(f'{name}\0{len(content)}\0'.encode())
            digest.update(content)
    return digest.hexdigest()[:16]


def _driver(name):
    from .registry import Registry
    return Registry().get('driver', name)


def _settings(root):
    file = root / SETTINGS_FILE
    if not file.exists():
        return {'orca': True, 'roles': {}, 'ladder': None}
    where = f'部署目录的 {file}'
    try:
        value = yaml.safe_load(file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f'{where}：格式不对：{exc}') from exc
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError(f'{where}：顶层必须是对象')
    extra = sorted(str(k) for k in value if k not in ('orca', 'roles', 'ladder'))
    if extra:
        raise ValueError(f'{where}：不认的字段 {"、".join(extra)}；只认 orca、roles、ladder')
    orca = value.get('orca', True)
    if type(orca) is not bool:
        raise ValueError(f'{where}：orca 只能是 true 或 false')
    roles = value.get('roles') or {}
    if not isinstance(roles, dict):
        raise ValueError(f'{where}：roles 必须是对象')
    for role, names in roles.items():
        if role not in ROLES:
            raise ValueError(f'{where}：roles 不认 {role}；只认 {"、".join(ROLES)}')
        if (not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names)
                or len(set(names)) != len(names)):
            raise ValueError(f'{where}：roles.{role} 必须是不重复的执行者名列表')
    return {'orca': orca, 'roles': roles, 'ladder': value.get('ladder')}


def _executor(path, name, orca):
    file = path / DEFINITION
    where = f'执行者 {name} 的 {file}'
    try:
        value = yaml.safe_load(file.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f'{where}：格式不对：{exc}') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{where}：顶层必须是对象')
    for key in RESERVED:
        if key in value:
            raise ValueError(f'{where}：不许写 {key}；执行者目录就是这个目录，版本由内容算出')
    driver_name = value.get('driver')
    if not isinstance(driver_name, str) or not driver_name:
        raise ValueError(f'{where}：driver 必须写明')
    try:
        driver = _driver(driver_name)
    except (LookupError, ImportError) as exc:
        raise ValueError(f'{where}：driver {driver_name}：{exc}') from exc
    profile = {'transport': ORCA, **value}
    if not orca and profile['transport'] == ORCA:
        profile['transport'] = HEADLESS
    present = [n for n in ISOLATION_FILES if (path / n).is_file()]
    if present:
        if not getattr(driver, 'isolation', False):
            raise ValueError(f'执行者 {name} 的目录放了 {"、".join(present)}，但 {driver_name} 壳不支持隔离；'
                             f'删掉这些文件或换壳（{file}）')
        if 'config_dir' in value:
            raise ValueError(f'{where}：目录里有 {"、".join(present)} 时不许再写 config_dir；隔离启动沿用本机登录')
        profile['executor_dir'] = str(path.resolve())
    profile['revision'] = revision(path)
    try:
        driver.validate(name, profile)
    except ValueError as exc:
        raise ValueError(f'{exc}（{file}）') from exc
    return profile


def assemble(path, deployment=None):
    """读仓库规则、叠部署目录、交核心校验；返回还没配置驱动的规则和每个配置档的来源。

    deployment 不给时按 WORKSHOP_DEPLOYMENT 或默认位置；目录不存在就不叠加。
    """
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ValueError(f"规则 YAML 无效：{exc}") from exc
    origins, unisolated, applied = {}, [], None
    if isinstance(data, dict) and isinstance(data.get('executor_profiles'), dict):
        data = dict(data)
        # 执行环境不写按 Orca（核心 R25）；仓库规则和部署目录一样。
        profiles = {name: ({'transport': ORCA, **profile} if isinstance(profile, dict) and 'transport' not in profile
                           else profile) for name, profile in data['executor_profiles'].items()}
        data['executor_profiles'] = profiles
        origins = {name: REPOSITORY for name in profiles}
        root = directory(deployment)
        if root is not None and root.is_dir():
            applied = root
            settings = _settings(root)
            added = {}
            executors = root / 'executors'
            if executors.is_dir():
                for child in sorted(executors.iterdir()):
                    if child.is_dir() and (child / DEFINITION).is_file():
                        added[child.name] = _executor(child, child.name, settings['orca'])
            for name, profile in added.items():
                old = profiles.get(name)
                if isinstance(old, dict) and old.get('executor_dir') and not profile.get('executor_dir'):
                    unisolated.append(name)
                profiles[name] = profile
                origins[name] = DEPLOYMENT
            if not settings['orca']:
                for name, profile in profiles.items():
                    if isinstance(profile, dict) and profile.get('transport') == ORCA:
                        profiles[name] = {**profile, 'transport': HEADLESS}
            _roles(data, settings['roles'], profiles, root / SETTINGS_FILE)
            _managers(data, profiles, origins, path, root)
            if settings['ladder'] is not None:
                try:
                    # 接手的执行者不要求在写代码的候选里，只要有定义。
                    data['ladder'] = ladder.validate(settings['ladder'], set(profiles))
                except ValueError as exc:
                    raise ValueError(f'部署目录的 {root / SETTINGS_FILE}：{exc}') from exc
    rules = Rules.validate(data)
    return Assembly(Rules(rules.data, str(path.expanduser().resolve())), origins, applied, tuple(unisolated))


def _roles(data, roles, profiles, file):
    for role, names in roles.items():
        for name in names:
            if name not in profiles:
                raise ValueError(f'{file} 的 roles.{role}：执行者 {name} 在仓库规则和部署目录里都没定义')
        if role == 'managers':
            if not isinstance(data.get('managers'), dict):
                raise ValueError(f'{file} 的 roles.managers：仓库规则没有 managers 一节')
            data['managers'] = {**data['managers'], 'candidates': list(names)}
        else:
            data['executors'] = {**(data.get('executors') or {}), role: [{'use': name} for name in names]}


def _managers(data, profiles, origins, path, root):
    managers = data.get('managers')
    if not isinstance(managers, dict) or not isinstance(managers.get('candidates'), list):
        return
    for name in managers['candidates']:
        profile = profiles.get(name)
        if isinstance(profile, dict) and profile.get('billing') != 'subscription':
            source = root / 'executors' / name / DEFINITION if origins.get(name) == DEPLOYMENT else path
            raise ValueError(f'项目经理候选 {name} 不是订阅计费（{source}）')
