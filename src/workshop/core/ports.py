"""同步短时端口；不等待人或一轮结束。守护进程把每次调用放进独立的作业进程（jobs.py），一拍不等它。字段和恢复时序见接入文档。"""
from typing import Callable, Literal, Protocol
from .cards import Question, Notice
from .models import After, Answer, Brief, ChannelPoll, FlowBrief, Classification, Evidence, Event, Poll, Receipt, Release, Started, RoundUsage, Selection


class ChannelRejected(Exception):
    """发送命令明确拒收卡片；启动或查询故障不得使用此异常。"""


class Unavailable(Exception):
    """现有订阅不可用，且没有启动副作用，可换下一候选。"""


class Retryable(Exception):
    """暂时故障；启动方法仅在确认没有启动副作用时使用。来源退避至30分钟，其余连续三次升级故障。"""


class TargetBlocked(Exception):
    """当前交付对象缺少动作前提；不重试、不影响其他交付对象。"""


class InputBlocked(Exception):
    """派活输入无法安全送出，尚未启动执行者；只阻塞当前活，修复后显式恢复。"""


class Source(Protocol):
    def wake_paths(self, target: dict | None = None) -> list[str]:
        """可选、纯本地查询（也兼容无参数实现）。返回文件路径；文件变化使本来源在该交付对象的 poll 缓存过期。"""
        ...

    def poll(self, target: dict, cursor: str | None) -> Poll:
        """整批事件和 cursor 同事务；Retryable 退避，其他异常隔离受影响类。"""
        ...


class Flow(Protocol):
    def classify(self, event: Event, item: dict | None, target: dict,
                 find_external: Callable[..., dict | None]) -> Classification | Literal["update", "ready", "rework"]:
        """只对无轮次令牌的 CLI 入口判定动作和归件；尚未创建活，输入 action=update。

        回查上游身份与原始内容，不能信正文自称身份；find_external 只读返回同目标的卡和活。
        Classification 可指定既有对象键，discard=True 记事实后丢弃；返回 None 是协议错误。
        find_external(object_key=...) 可查同目标既有活（先解析别名）。异常按流程故障处理。
        来源 poll、带令牌事件和回执子事件不调用此方法；无令牌输入仍可能来自 agent。
        """
        ...
    def stages(self, target: dict) -> tuple[str, ...]: ...
    def automatic(self, item: dict, stage: str, target: dict) -> bool:
        """可选、只读。为真时由程序完成本阶段，仍经 assess、核心关口和 after。

        不挑执行者、不启动会话、不记录执行轮；未实现或返回 False 保持原路径。
        """
        ...
    def brief(self, item: dict, stage: str, target: dict) -> str | FlowBrief:
        """可信说明；item.history 含每轮结论，gate_reason 含需要修正的关口理由。"""
        ...
    def assess(self, item: dict, receipt: Receipt, target: dict) -> Evidence:
        """短时只读核对证据；paths/reviewers 决定需要几家审；未知动作类别是插件错误。

        无下一步的 nothing 可返回 recheck_after；核心定时调用 after，完成后只重跑本阶段。
        """
        ...
    def prepare_recovery(self, item: dict, options: dict, target: dict) -> dict:
        """可选。命令行追加核验前只读验证并固定计划；核心记为恢复事实，由流程消费一次。

        不能写流程状态；失败抛 ValueError。计划是流程自有数据，核心不解释其业务字段。
        """
        ...
    def after(self, item: dict, completed_stage: str, key: str, target: dict) -> After | list[str] | None:
        """准许推进或核实等待到期后调用，item.result 含本轮回执，key 必须持久幂等。

        After 可绑定别名、仍留本阶段或返回 blocked 停下；回查完成不等于准许推进。
        """
        ...
    def hold_workdir(self, item: dict, target: dict) -> str:
        """可选。活已收尾完成，执行者开过的工作目录还要不要留：返回留着的原因，空串表示可以收回。

        短时只读；Retryable 稍后再问。核心对留着的活约一小时再问一次。
        """
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
    def release(self, item: dict, target: dict) -> Release | None:
        """可选。收回本执行者为这件已收尾的活开的工作目录。

        不是自己开的、或已经不在，返回 None；收回了返回 Release(True)；
        有没交出去的改动等原因不能收，返回 Release(False, 原因)，核心通知 Kun 一次，不再重试。
        暂时故障抛 Retryable。只在这件活没有在跑的轮次时调用。
        """
        ...


class Channel(Protocol):
    def wake_paths(self, target: dict | None = None) -> list[str]:
        """可选、纯本地查询（也兼容无参数实现）。逐交付对象收集路径；变化使通道的 poll 缓存过期。"""
        ...

    def ask(self, key: str, card: Question) -> str:
        """返回外部编号。调用前核心已持久记发送中，异常后先 reconcile。"""
        ...
    def notify(self, key: str, card: Notice) -> str: ...
    def reconcile(self, key: str, card: dict) -> str | Literal["absent"] | None:
        """返回已发外部编号；absent 表示确定未发，可重试；None 表示不确定，禁止重发。"""
        ...
    def poll(self, cards: list[dict]) -> ChannelPoll | list[Answer]:
        """逐卡读取结果带唯一 token；兼容旧通道的回答列表。value 返回所选 key；核心审批只认 yes。"""
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


class WaitService(Protocol):
    """独立服务端口。content/read 的内容只由流程解释，核心只用引用和完成标识。"""
    def invoke(self, action: str, content: dict, config: dict) -> dict:
        """流程调用服务的业务操作；核心不解释或保存 content。"""
        ...
    def create(self, operation: str, content: dict, config: dict) -> str:
        """稳定操作号，先落盘再返回引用；同号改内容必须拒绝。"""
        ...
    def read(self, reference: str, config: dict) -> dict: ...
    def poll(self, reference: str, config: dict) -> dict:
        """只返回 ref、ready(bool)、token；ready 只代表有结果，不代表批准。"""
        ...
    def acknowledge(self, operation: str, reference: str, token: str, config: dict) -> dict:
        """核心持久记录恢复后才确认；重试同号幂等。"""
        ...
