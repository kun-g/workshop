"""做梦的生成器和等价判定入口；测试里换成 mock，真实路径换成执行者。

等价判定失败关闭：超时、抛异常或返回非法格式都按「是」处理，也就是拒绝写入。
"""


class Dreamer:
    def __init__(self, generate, equivalent):
        self.generate = generate
        self.equivalent = equivalent

    def propose(self, facts, current):
        proposals = self.generate(facts, current) or []
        if not isinstance(proposals, list) or not all(isinstance(item, dict) for item in proposals):
            raise ValueError("做梦生成器须返回候选对象列表")
        return proposals

    def same(self, left, right):
        try:
            value = self.equivalent(left, right)
        except TimeoutError:
            return True
        except Exception:
            return True
        return value is not False
