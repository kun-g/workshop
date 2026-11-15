"""阶段区间，以及这个入口要不要放行。"""
from .models import STAGES


def gate(ingress):
    """来源轮询的事件要放行（ready）才能建活；外部入口经流程判定、agent 连锁事件不受此限。
    标记写进事实，旧事实没有标记，重建视图时照旧投影。"""
    return {"release_gate": True} if ingress == "source" else {}


def serial_stage(stage):
    """串行路径只保护执行至交付，含两个端点及中间启用的阶段。"""
    return STAGES.index("执行") <= STAGES.index(stage) <= STAGES.index("交付")
