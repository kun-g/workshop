"""唯一规则文件的读取与校验。"""
from dataclasses import dataclass
from pathlib import Path
from .models import STAGES
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
        limits = data.get("limits", {})
        if type(limits.get("concurrent")) is not int or limits["concurrent"] < 1:
            raise ValueError("concurrent 必须为正整数")
        if type(limits.get("chain_depth")) is not int or not 0 < limits["chain_depth"] <= 8:
            raise ValueError("chain_depth 必须在 1 到 8 之间")
        if not data.get("channel"):
            raise ValueError("规则缺通道插件")
        if type(limits.get("round_timeout_seconds", 7200)) is not int or limits.get("round_timeout_seconds", 7200) <= 0:
            raise ValueError("每轮期限必须为正整数秒")
        for name, target in data["targets"].items():
            if not isinstance(target, dict) or not target.get("plugin"):
                raise ValueError(f"{name} 缺流程插件")
            if type(target.get("pending_limit", 5)) is not int or target.get("pending_limit", 5) < 1:
                raise ValueError("pending_limit 必须是正整数")
            for field in ("frozen_paths", "serial_paths"):
                value = target.get(field, [])
                if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                    raise ValueError(f"{name} 的 {field} 必须是非空路径前缀列表")
        for role in ("writer", "reviewer", "acceptor"):
            value = data.get("executors", {}).get(role)
            entries = value if isinstance(value, list) else [value]
            if not entries or not all(isinstance(v, str) or isinstance(v, dict) and v.get("use") for v in entries):
                raise ValueError(f"缺执行者 {role}")
        mappings = [data.get("stage_executors", {})] + [t.get("stage_executors", {}) for t in data["targets"].values()]
        for mapping in mappings:
            if not isinstance(mapping, dict) or any(stage not in STAGES for stage in mapping):
                raise ValueError("阶段执行者配置无效")
            for value in mapping.values():
                entries = value if isinstance(value, list) else [value]
                if not entries or not all(isinstance(v, str) and v or isinstance(v, dict) and v.get("use") for v in entries):
                    raise ValueError("阶段执行者候选不能为空")
        for mapping in [data.get("stage_selectors", {})] + [t.get("stage_selectors", {}) for t in data["targets"].values()]:
            if not isinstance(mapping, dict) or any(k not in STAGES or not isinstance(v, str) or not v for k, v in mapping.items()):
                raise ValueError("阶段挑选逻辑配置无效")
        for entry in [data] + list(data["targets"].values()):
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

    def target(self, name: str) -> dict:
        return {"name": name, **self.data["targets"][name]}

    def candidates(self, stage: str, target: str | None = None, writer: str | None = None) -> list[dict]:
        role = {"审查": "reviewer", "验收": "acceptor"}.get(stage, "writer")
        overrides = {**self.data.get("stage_executors", {}), **(self.target(target).get("stage_executors", {}) if target else {})}
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

    def selector(self, stage: str, target: str) -> str:
        local = self.target(target)
        return local.get("stage_selectors", {}).get(stage, local.get("selector",
                        self.data.get("stage_selectors", {}).get(stage, self.data.get("selector", "ordered"))))
