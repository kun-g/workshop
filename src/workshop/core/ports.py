"""插件端口。方法同步、短时返回；不在端口内等待人或等待一轮结束。

核心只传 JSON 可序列化的活快照；插件不得写数据库。所有外部副作用必须以
key/token 幂等：超时或进程退出后可能重放。普通异常由核心记事实并隔离受影响
的类；通道异常只留待发记录，不能阻塞执行。Unavailable 表示可试下一执行者。
完整时序、字段与恢复协议见 docs/implementation/ports.md。
"""
from typing import Protocol
from .models import Answer, Brief, Evidence, Poll, Receipt, Started, Usage


class Unavailable(Exception):
    """现有订阅/额度不可用，没有启动副作用；允许按规则选下一家。"""


class Source(Protocol):
    def poll(self, target: dict, cursor: str | None) -> Poll:
        """只读拉事件。cursor 在整批事实提交后更新；重放靠 source/ext_id 去重。"""
        ...


class Flow(Protocol):
    def stages(self, target: dict) -> tuple[str, ...]:
        """有序阶段子集；必须含接单、说清楚、执行、验收、收尾。"""
        ...

    def brief(self, item: dict, stage: str, target: dict) -> str:
        """可信派活说明。外部正文由核心另放 Brief.materials，不拼为指令。"""
        ...

    def assess(self, item: dict, receipt: Receipt, target: dict) -> Evidence:
        """核对证据：可执行检查 argv、是否通过、拟交付动作类别。只读。"""
        ...

    def after(self, item: dict, completed_stage: str, key: str, target: dict) -> None:
        """核心准许推进后调用，key 幂等；不得越过核心再推进或私自执行受控动作。"""
        ...


class Executor(Protocol):
    def available(self, config: dict) -> bool:
        """仅检查现有订阅/已付额度，禁止自动加购或开按量计费。"""
        ...

    def launch(self, token: str, brief: Brief, item: dict, config: dict) -> Started:
        """token 幂等：已启动返回同一 Started；不确定是否启动时抛普通异常。"""
        ...

    def resume(self, token: str, session: str, brief: Brief, item: dict, config: dict) -> Started:
        """接回给定 session，token 幂等；返回值必须仍为该 session。"""
        ...

    def stop(self, token: str, started: Started) -> None:
        """幂等关闭；仅确认进程/终端已退出才返回。异常时核心不释放并发位。"""
        ...

    def usage(self, token: str, started: Started) -> Usage | None:
        """整轮累计用量，不是增量；None 表示没上报，异常不妨碍收尾。"""
        ...


class Channel(Protocol):
    def ask(self, key: str, card: dict) -> str:
        """幂等提交问题，立刻返回外部卡号；不等回答。"""
        ...

    def notify(self, key: str, card: dict) -> str:
        """幂等通知，返回外部编号。"""
        ...

    def poll(self, cards: list[dict]) -> list[Answer]:
        """短时查回答，Answer.card_id 是核心卡号；approved 仅来自明确授权。"""
        ...


class TaskHome(Protocol):
    def open(self, key: str, item: dict, target: dict) -> str:
        """key 幂等发工单，返回单号。核心不冒充负责会话领单或收单。"""
        ...

    def update(self, key: str, ticket: str, item: dict, target: dict) -> None:
        """幂等同步状态；需会话身份的领单/收单由执行者在那轮完成。"""
        ...


class Plugins(Protocol):
    def get(self, port: str, name: str) -> object:
        """按 source/flow/executor/channel/taskhome 与名字取插件，缺失抛 LookupError。"""
        ...
