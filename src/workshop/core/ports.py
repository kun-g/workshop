"""同步短时端口；不等待人或一轮结束。字段和恢复时序见接入文档。"""
from typing import Literal, Protocol
from .cards import Question, Notice
from .models import Answer, Brief, Evidence, Event, Poll, Receipt, Started, RoundUsage, Selection


class ChannelRejected(Exception):
    """确定没有发送：本地校验、命令不可执行或上游明确拒收；不得待对账。"""


class Unavailable(Exception):
    """现有订阅不可用，且没有启动副作用，可换下一候选。"""


class Retryable(Exception):
    """暂时故障；启动方法仅在确认没有启动副作用时使用。核心退避，连续三次升级故障。"""


class Source(Protocol):
    def poll(self, target: dict, cursor: str | None) -> Poll:
        """整批事件和 cursor 同事务；Retryable 退避，其他异常隔离受影响类。"""
        ...


class Flow(Protocol):
    def classify(self, event: Event, item: dict, target: dict) -> Literal["update", "ready", "rework"]:
        """只对无轮次令牌的 CLI 入口判定可信来源动作；输入 action 已归 update、depth 已归零。

        回查上游身份与原始内容，不能信正文自称身份，默认 update；异常按流程故障处理。
        来源 poll、带令牌事件和回执子事件不调用此方法；无令牌输入仍可能来自 agent。
        """
        ...
    def stages(self, target: dict) -> tuple[str, ...]: ...
    def brief(self, item: dict, stage: str, target: dict) -> str:
        """可信说明；item.history 含每轮结论，gate_reason 含需要修正的关口理由。"""
        ...
    def assess(self, item: dict, receipt: Receipt, target: dict) -> Evidence:
        """短时只读核对证据；paths/reviewers 决定需要几家审；未知动作类别是插件错误。"""
        ...
    def after(self, item: dict, completed_stage: str, key: str, target: dict) -> None:
        """准许推进后调用，key 必须持久幂等；不得越过核心推进。"""
        ...


class Executor(Protocol):
    def available(self, config: dict) -> bool:
        """仅现有订阅/已付额度，禁止加购。"""
        ...
    def launch(self, token: str, brief: Brief, item: dict, config: dict, target: dict) -> Started:
        """token 持久幂等。target.runtime 含绝对路径、环境变量与可直接执行的 CLI argv。"""
        ...
    def resume(self, token: str, session: str, brief: Brief, item: dict, config: dict, target: dict) -> Started:
        """同 token 找同一轮；必须接回传入 session。"""
        ...
    def poll(self, token: str, started: Started | None) -> Literal["running", "exited"]:
        """查询进程是否仍在；started=None 时按 token 对账未确认的启动，不能猜退出。"""
        ...
    def stop(self, token: str, started: Started | None) -> None:
        """幂等关闭，started=None 时按 token 查找并关闭；确认退出才返回，失败不释放占位。"""
        ...
    def usage(self, token: str, started: Started | None, since: str) -> RoundUsage | None:
        """从 since 起的主会话和按模型合计的子执行增量；未读到子执行须标 children_reported=False。"""
        ...


class Channel(Protocol):
    def ask(self, key: str, card: Question) -> str:
        """返回外部编号。调用前核心已持久记发送中，异常后先 reconcile。"""
        ...
    def notify(self, key: str, card: Notice) -> str: ...
    def reconcile(self, key: str, card: dict) -> str | Literal["absent"] | None:
        """返回已发外部编号；absent 表示确定未发，可重试；None 表示不确定，禁止重发。"""
        ...
    def poll(self, cards: list[dict]) -> list[Answer]:
        """只查未作废卡；value 返回所选 key。核心自己的审批只认 yes，approved 仅供插件问题使用。"""
        ...
    def cancel(self, key: str, external: str | None) -> None:
        """按核心 key 幂等作废旧问题；external 不明时先对账，失败后重试。"""
        ...


class TaskHome(Protocol):
    """可选工单端口，第一版不配置。"""
    def open(self, key: str, item: dict, target: dict) -> str: ...
    def update(self, key: str, ticket: str, item: dict, target: dict) -> None: ...


class Plugins(Protocol):
    def get(self, port: str, name: str) -> object: ...


class Selector(Protocol):
    def select(self, target: dict, stage: str, round_number: int, history: list[dict],
               candidates: list[dict]) -> Selection | None:
        """候选含 config 与 available 快照；返回候选配置和理由，None 表示没有可用候选。"""
        ...
