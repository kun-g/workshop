"""唯一规则文件的读取与校验。"""
from dataclasses import dataclass
from pathlib import Path
from .models import STAGES, DEFAULT_BRIEF_MAX_CHARS
from .flows import configured, entries as flow_entries, validate as validate_flows
import yaml


@dataclass(frozen=True)
class Rules:
    data: dict
    path: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Rules":
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ValueError(f"规则 YAML 无效：{exc}") from exc
        rule = cls.validate(data)
        return cls(rule.data, str(path.expanduser().resolve()))

    @classmethod
    def validate(cls, data: dict) -> "Rules":
        if not isinstance(data, dict) or not data.get("targets"):
            raise ValueError("规则缺交付对象")
        services = data.get('wait_services', {})
        if not isinstance(services, dict):
            raise ValueError('wait_services 必须是对象')
        for name, service in services.items():
            if not isinstance(name, str) or not name or not isinstance(service, dict):
                raise ValueError('等待端口须为名字到配置对象的映射')
        limits = data.get("limits", {})
        budget = limits.get("brief_max_chars", DEFAULT_BRIEF_MAX_CHARS)
        if type(budget) is not int or budget <= 0:
            raise ValueError("brief_max_chars 必须为正整数字符数")
        if type(limits.get("concurrent")) is not int or limits["concurrent"] < 1:
            raise ValueError("concurrent 必须为正整数")
        if type(limits.get("chain_depth")) is not int or not 0 < limits["chain_depth"] <= 8:
            raise ValueError("chain_depth 必须在 1 到 8 之间")
        retired = [key for key in ("channel", "notices") if key in data]
        if retired:
            # 介入点 R13：消息只经介入点服务的送达命令，不再有核心通道和通知开关。
            raise ValueError(f"{'、'.join(retired)} 已退役：消息只经介入点服务送达")
        if type(limits.get("round_timeout_seconds", 7200)) is not int or limits.get("round_timeout_seconds", 7200) <= 0:
            raise ValueError("每轮期限必须为正整数秒")
        for name, target in data["targets"].items():
            if not isinstance(target, dict):
                raise ValueError(f"{name} 交付对象配置无效")
            validate_flows(name, target)
            if type(target.get("pending_limit", 5)) is not int or target.get("pending_limit", 5) < 1:
                raise ValueError("pending_limit 必须是正整数")
            for field in ("serial_paths",):
                value = target.get(field, [])
                if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                    raise ValueError(f"{name} 的 {field} 必须是非空路径前缀列表")
        locals_ = [configured(name, target, flow) for name, target in data["targets"].items()
                   for flow in flow_entries(target)]
        for role in ("writer", "reviewer", "acceptor"):
            value = data.get("executors", {}).get(role)
            entries = value if isinstance(value, list) else [value]
            if not entries or not all(isinstance(v, str) or isinstance(v, dict) and v.get("use") for v in entries):
                raise ValueError(f"缺执行者 {role}")
        mappings = [data.get("stage_executors", {})] + [t.get("stage_executors", {}) for t in locals_]
        for mapping in mappings:
            if not isinstance(mapping, dict) or any(stage not in STAGES for stage in mapping):
                raise ValueError("阶段执行者配置无效")
            for value in mapping.values():
                entries = value if isinstance(value, list) else [value]
                if not entries or not all(isinstance(v, str) and v or isinstance(v, dict) and v.get("use") for v in entries):
                    raise ValueError("阶段执行者候选不能为空")
        for mapping in [data.get("stage_selectors", {})] + [t.get("stage_selectors", {}) for t in locals_]:
            if not isinstance(mapping, dict) or any(k not in STAGES or not isinstance(v, str) or not v for k, v in mapping.items()):
                raise ValueError("阶段挑选逻辑配置无效")
        for entry in [data] + locals_:
            if "selector" in entry and (not isinstance(entry["selector"], str) or not entry["selector"]):
                raise ValueError("挑选逻辑名字不能为空")
        profiles = data.get("executor_profiles", {})
        if not isinstance(profiles, dict):
            raise ValueError("executor_profiles 必须是对象")
        for name, profile in profiles.items():
            if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
                raise ValueError(f"配置档名 {name!r} 无效")
            if not isinstance(profile, dict):
                raise ValueError(f"配置档 {name} 必须是对象")
            for field in ("driver", "transport", "billing"):
                if not isinstance(profile.get(field), str) or not profile[field]:
                    raise ValueError(f"配置档 {name} 的 {field} 必须写明")
        managers = data.get('managers')
        if managers is not None:
            if not isinstance(managers, dict) or set(managers) != {'target', 'service', 'manual', 'candidates'}:
                raise ValueError('managers 须指定 target/service/manual/candidates')
            if managers['target'] not in data['targets'] or managers['service'] not in services:
                raise ValueError('项目经理目标或服务未登记')
            if not isinstance(managers['manual'], str) or not managers['manual'].strip():
                raise ValueError('项目经理手册须是非空路径')
            candidates = managers['candidates']
            if not isinstance(candidates, list) or not candidates or not all(isinstance(n, str) and n for n in candidates) or len(set(candidates)) != len(candidates):
                raise ValueError('项目经理候选须是非空不重复列表')
            for name in candidates:
                if name not in profiles or profiles[name].get('billing') != 'subscription':
                    raise ValueError('项目经理只能使用已配置的订阅候选')
        configs = []
        for entries in data["executors"].values():
            configs.extend(entries if isinstance(entries, list) else [entries])
        for mapping in mappings:
            for entries in mapping.values():
                configs.extend(entries if isinstance(entries, list) else [entries])
        if profiles:
            resolved = []
            for config in configs:
                ref = {"use": config} if isinstance(config, str) else config
                name = ref.get("use")
                if name not in profiles:
                    raise ValueError(f"配置档 {name} 未定义")
                extra = set(ref) - {"use"}
                if extra:
                    raise ValueError(f"配置档 {name} 的候选重复配置 {'、'.join(sorted(extra))}；只在 executor_profiles 定义")
                resolved.append(profiles[name])
            configs = resolved
        if any(not isinstance(c, dict) or c.get("billing") not in ("subscription", "metered") for c in configs):
            raise ValueError("每个执行者必须写明 billing=subscription 或 metered")
        for name in ("ask_first", "skip_ask"):
            if not isinstance(data.get(name), list) or not all(isinstance(v, str) for v in data[name]):
                raise ValueError(f"{name} 必须是列表")
        return cls(data)

    def target(self, name: str, flow: str = "") -> dict:
        return configured(name, self.data["targets"][name], flow)

    def candidates(self, stage: str, target: str | None = None, writer: str | None = None, flow: str = "") -> list[dict]:
        role = {"审查": "reviewer", "验收": "acceptor"}.get(stage, "writer")
        overrides = {**self.data.get("stage_executors", {}), **(self.target(target, flow).get("stage_executors", {}) if target else {})}
        entries = overrides.get(stage, self.data["executors"][role])
        entries = entries if isinstance(entries, list) else [entries]
        result = []
        for entry in entries:
            config = {"use": entry} if isinstance(entry, str) else dict(entry)
            profile = self.data.get("executor_profiles", {}).get(config["use"])
            if profile is not None:
                config = {**profile, **config}
            config.setdefault("family", config.get("driver", config["use"]))
            if stage in ("审查", "验收") and config["family"] == writer:
                continue
            result.append(config)
        return result

    def selector(self, stage: str, target: str, flow: str = "") -> str:
        local = self.target(target, flow)
        return local.get("stage_selectors", {}).get(stage, local.get("selector",
                        self.data.get("stage_selectors", {}).get(stage, self.data.get("selector", "ordered"))))
