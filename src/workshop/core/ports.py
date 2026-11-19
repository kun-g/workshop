"""同步短时端口；不等待人或一轮结束。守护进程把每次调用放进独立的作业进程（jobs.py），一拍不等它。字段和恢复时序见接入文档。"""
from typing import Callable, Literal, Protocol
from .models import After, Brief, FlowBrief, Classification, Evidence, Event, Poll, Receipt, Release, Started, RoundUsage, Selection


class Unavailable(Exception):
    """现有订阅不可用，且没有启动副作用，可换下一候选。"""


class Retryable(Exception):
    """暂时故障；启动方法仅在确认没有启动副作用时使用。来源退避至30分钟，其余连续三次升级故障。"""


class TargetBlocked(Exception):
    """当前交付对象缺少动作前提；不重试、不影响其他交付对象。"""


class InputBlocked(Exception):
    """派活输入无法安全送出，尚未启动执行者；只阻塞当前活，修复后显式恢复。"""


class WaitRejected(Exception):
    """被调等待服务明确拒绝业务请求；只阻塞当前活。"""


class WaitUnavailable(Exception):
    """等待服务暂时不可达或未确认结果；保留幂等操作号重查。"""


class Source(Protocol):
    def validate(self, target: dict) -> None:
        """可选，装配时纯本地校验来源配置；无效时抛 ValueError，先于守护进程启动。"""
        ...

    def normalize(self, batch: Poll, target: dict,
                  find_external: Callable[..., dict | None]) -> Poll:
        """可选，核心 intake 调用：整理未 normalized 的批次，保留游标。

        find_external(object_key=..., flow=...) 只读查同目标已配置流程的活，返回 {"item": 快照} 或 None；
        不填 flow 时查默认流程。查找由核心及作业进程提供，来源不打开核心状态库。
        """
        ...

    def wake_paths(self, target: dict | None = None) -> list[str]:
        """可选、纯本地查询（也兼容无参数实现）。返回文件路径；文件变化使本来源在该交付对象的 poll 缓存过期。"""
        ...

    def poll(self, target: dict, cursor: str | None) -> Poll:
        """整批事件和 cursor 同事务；Retryable 退避，其他异常隔离受影响类。"""
        ...


class Flow(Protocol):
    def origin(self, item: dict, target: dict) -> dict[str, str] | None:
        """可选，查看工具总览调用：只读返回 title、url、ref、quote（标题、链接、简称、原话）。

        没有来源摘要时返回 None；不得回查网络或写状态。查看工具失败时退回登记名。
        """
        ...

    def describe(self, event: Event, target: dict) -> str:
        """可选，查看工具调用：只读给出事件的一句话说明。"""
        ...
    def cleanup(self, target: dict, keep: list[str | None]) -> None:
        """可选，核心 workdirs 调用：清理流程自有的临时目录，保留 keep 中的工作目录。"""
        ...
    def configure_target(self, target: dict) -> dict:
        """可选，装配层调用的旧写法兼容；去掉未配置 flows 的旧写法时一并删除。"""
        ...

    def relations(self, objects: list[dict], target: dict) -> dict:
        """可选、只读。按活 id 返回 Relations 完整快照；objects 仅含 id/object_key。

        来源完成状态由插件回查；每件读取错误填 error，不能用空列表代替。
        批量作业五秒过期，不占执行位置；核心在派轮之前强制核对。
        """
        ...
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
    def stage_moves(self, item: dict, target: dict) -> dict[str, str]:
        """可选、只读。活停在规则已去掉的阶段时落到哪一段，例如 {"说清楚": "接单"}；
        没给或给的阶段不在当前流程里，就进去掉阶段之后的第一个。"""
        ...
    def brief(self, item: dict, stage: str, target: dict) -> str | FlowBrief:
        """可信说明；item.history 含每轮结论，gate_reason 含需要修正的关口理由。

        必需；若 automatic 的实现恒为 True，可不实现 brief。
        item.allowed_advances 是本阶段回执可填的阶段名列表，只在 brief 调用的快照里提供。"""
        ...
    def assess(self, item: dict, receipt: Receipt, target: dict) -> Evidence:
        """短时只读核对证据；paths/reviewers 决定需要几家审；未知动作类别是插件错误。

        无下一步的 nothing 可返回 recheck_after；核心定时调用 after，完成后只重跑本阶段。
        handoff 由流程按外部状态判交接，代替回执自报的 advance；awaits_update 让活停在本阶段等来源更新。
        """
        ...
    def validate_receipt(self, item: dict, receipt: Receipt, target: dict) -> None:
        """可选、纯本地协议校验；交回执时调用，ValueError 当轮退回修改，不执行副作用。"""
        ...
    def prepare_recovery(self, item: dict, options: dict, target: dict) -> dict:
        """可选。命令行追加核验前只读验证并固定计划；核心记为恢复事实，由流程消费一次。

        不能写流程状态；失败抛 ValueError。计划是流程自有数据，核心不解释其业务字段。
        """
        ...
    def after(self, item: dict, completed_stage: str, key: str, target: dict, services=None) -> After | list[str] | None:
        """准许推进或核实等待到期后调用，item.result 含本轮回执，key 必须持久幂等。

        After 可绑定别名、仍留本阶段或返回 blocked 停下；回查完成不等于准许推进。
        services(name) 按规则返回 (WaitService, 配置副本)，不能改装配；兼容四参数旧实现。
        restart=True 持久登记原钩子，暂停该钩子的结算，由外部守护重启。
        新进程首拍成功后立即重调同一 key，不经过 pending 退避；仅此次重调的
        target.runtime 有 daemon（与心跳 process 相同，含 pid/start）和 first_ok_at。
        其他调用不带这两个字段；重调须重新核验状态，不可缓存旧的 restart=True。
        """
        ...
    def hold_workdir(self, item: dict, target: dict) -> str:
        """可选。活已收尾完成，执行者开过的工作目录还要不要留：返回留着的原因，空串表示可以收回。

        短时只读；Retryable 稍后再问。核心对留着的活约一小时再问一次。
        """
        ...


class Executor(Protocol):
    def inform(self, token: str, started: Started, operation: str, text: str) -> bool:
        """可选。项目经理协调器调用：按稳定 operation 送新事实到正在运行的会话。

        True 代表已确认接受；False 代表不支持或未确认，调用方保留事实，退出后 resume。
        同号改正文拒绝；不向身份不明或已退出的终端输入，不把不确定当成功。
        """
        ...
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


class TaskHome(Protocol):
    """可选工单端口，第一版不配置。"""
    def open(self, key: str, item: dict, target: dict) -> str: ...
    def update(self, key: str, ticket: str, item: dict, target: dict) -> None: ...


class Driver(Protocol):
    """装配层 Registry 调用；这里的三个成员均为必需。驱动内部方法不属于装配端口。"""
    family: str

    def validate(self, name: str, config: dict) -> None:
        """必需，装配层在启动任何会话前核对配置档。"""
        ...
    def executor(self, name: str, config: dict) -> Executor:
        """必需，装配层按配置档创建执行者。"""
        ...


class Plugins(Protocol):
    def configure(self, rules):
        """可选，核心 Engine 初始化时调用：装配层核对并补全规则。"""
        ...

    def get(self, port: str, name: str) -> object: ...


class Selector(Protocol):
    def select(self, target: dict, stage: str, round_number: int, history: list[dict],
               candidates: list[dict]) -> Selection | None:
        """候选含 config 与 available 快照；返回候选配置和理由，None 表示没有可用候选。"""
        ...


class WaitService(Protocol):
    """独立服务端口。content/read 由流程和查看工具解释，核心只用引用和完成标识。

    直接调用五条条件：被调方持久化且操作号幂等；核心不用分清谁坏了；
    不开会话、不占并发或串行名额；对象由核心按规则经端口交出；只在 after 短时调用。
    明确拒绝用 WaitRejected，调用方返回 After(blocked=...)；暂时不通/结果不明
    用 WaitUnavailable，调用方返回 After(pending=True)，原号重查，到 pending_limit
    只卡本活；调用方自己出错按流程故障隔离，不能伪装业务拒绝。
    """
    def validate(self, config: dict) -> None:
        """可选，装配层启动时验证 wait_services.<名字>，不读取业务许可目录。"""
        ...
    def wake_paths(self, config: dict) -> list[str]:
        """可选，核心 waking 调用；收等待服务的配置，返回可叫醒核心的本地路径。"""
        ...
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
