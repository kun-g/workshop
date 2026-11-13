"""只在核心允许的候选中，按规则顺序取第一个。"""
from workshop.core.models import Selection


class OrderedSelector:
    def select(self, target, stage, round_number, history, candidates):
        if not candidates:
            return None
        config = candidates[0]["config"]
        return Selection(config, f'按规则顺序选首个获准候选 {config["use"]}；阶段 {stage}，第 {round_number} 轮')


def make_selector():
    return OrderedSelector()
