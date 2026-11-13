"""唯一规则文件的读取与校验。"""
from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass(frozen=True)
class Rules:
    data: dict

    @classmethod
    def load(cls, path: Path) -> "Rules":
        return cls.validate(yaml.safe_load(path.read_text()))

    @classmethod
    def validate(cls, data: dict) -> "Rules":
        if not isinstance(data, dict) or not data.get("targets"):
            raise ValueError("规则缺交付对象")
        limits = data.get("limits", {})
        if type(limits.get("concurrent")) is not int or limits["concurrent"] < 1:
            raise ValueError("concurrent 必须为正整数")
        if type(limits.get("chain_depth")) is not int or not 0 < limits["chain_depth"] <= 8:
            raise ValueError("chain_depth 必须在 1 到 8 之间")
        if not data.get("channel") or not data.get("taskhome"):
            raise ValueError("规则缺通道或工单插件")
        for name, target in data["targets"].items():
            if not isinstance(target, dict) or not target.get("plugin"):
                raise ValueError(f"{name} 缺流程插件")
        for role in ("writer", "reviewer", "acceptor"):
            value = data.get("executors", {}).get(role)
            entries = value if isinstance(value, list) else [value]
            if not entries or not all(isinstance(v, str) or isinstance(v, dict) and v.get("use") for v in entries):
                raise ValueError(f"缺执行者 {role}")
        for name in ("ask_first", "skip_ask"):
            if not isinstance(data.get(name), list) or not all(isinstance(v, str) for v in data[name]):
                raise ValueError(f"{name} 必须是列表")
        return cls(data)

    def target(self, name: str) -> dict:
        return {"name": name, **self.data["targets"][name]}

    def candidates(self, stage: str, writer: str | None) -> list[dict]:
        role = {"审查": "reviewer", "验收": "acceptor"}.get(stage, "writer")
        entries = self.data["executors"][role]
        entries = entries if isinstance(entries, list) else [entries]
        result = []
        for entry in entries:
            config = {"use": entry} if isinstance(entry, str) else dict(entry)
            config.setdefault("family", config["use"])
            if role != "writer" and config["family"] == writer:
                continue
            result.append(config)
        return result
