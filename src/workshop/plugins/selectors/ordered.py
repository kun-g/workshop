"""只在核心允许的候选中，按规则顺序取第一个。"""
from workshop.core.models import Selection


class OrderedSelector:
    def select(self, target, stage, round_number, history, candidates):
        if not candidates:
            return None
        config = candidates[0]["config"]
        if target.get("selection", {}).get("takeover"):
            # 阶梯指定了接手者：核心只交来它一个（ws#301）。
            return Selection(config, f'选阶梯指定的接手者 {config["use"]}；阶段 {stage}，第 {round_number} 轮')
        return Selection(config, f'按规则顺序选首个获准候选 {config["use"]}；阶段 {stage}，第 {round_number} 轮')


def make_selector():
    return OrderedSelector()
