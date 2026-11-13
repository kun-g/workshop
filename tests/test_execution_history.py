"""Kun 11-13 修改轮 2：可核对的执行记录、模型汇总、可替换的挑选逻辑。"""
from dataclasses import asdict
from datetime import timedelta
from copy import deepcopy
import math

import pytest

from workshop.core.models import Usage, Selection
from workshop.core.report import model_metrics, usage, why
from workshop.core.rules import Rules
from workshop.registry import Registry
from conftest import current, event, finish, item, reach


def test_actual_model_not_copied_from_requested_and_full_record(engine):
    config = deepcopy(engine.rules.data)
    config["executors"]["writer"].update(model="requested-model", effort="high")
    engine.rules = Rules.validate(config)
    executor = engine.plugins.get("executor", "writer")
    executor.usage_value = Usage("actual-model", 100, 200, 50, 20, 0.1, "subscription")
    engine.ingest(event())
    engine.tick()
    row = current(engine)
    engine.clock.now += timedelta(seconds=60)
    finish(engine, "说清楚")
    result = engine.log.get("rounds", row["token"])
    assert result["requested"] == {"executor": "writer", "model": "requested-model", "effort": "high"}
    assert result["usage"] == asdict(executor.usage_value)
    assert result["elapsed_seconds"] == 60
    assert result["ended"] and result["outcome"] == "done"
    assert "按规则顺序" in result["selection_reason"]
    text = why(engine.log, "alpha:one")
    for term in ("requested-model", "high", "actual-model", "60.0秒", "100 / 200 / 50 / 20", "0.1", "subscription", "选择理由"):
        assert term in text
    before = engine.log.all("rounds")
    engine.log.rebuild()
    assert engine.log.all("rounds") == before


def test_unknown_is_null_and_separate_model_group(engine):
    engine.plugins.get("executor", "writer").usage_value = None
    reach(engine, "说清楚")
    rows = engine.log.all("rounds")
    assert rows[0]["usage"] == asdict(Usage())
    stats = model_metrics(engine.log)[None]
    assert stats["unreported"] == 1
    assert stats["cost_usd"] is None and all(v is None for v in stats["tokens"].values())
    assert stats["cache_hit_rate"] is None
    assert "未知" in usage(engine.log, engine.clock())


def test_cache_rate_excludes_partial_input_and_keeps_known_zero(engine):
    executor = engine.plugins.get("executor", "writer")
    executor.usage_value = Usage("m", 100, 300, 100, 20, 1.0, "subscription")
    reach(engine, "说清楚")
    engine.clock.now += timedelta(seconds=10)
    executor.usage_value = Usage("m", 100, None, 0, 20, 0.0, "subscription")
    finish(engine, "执行")
    stats = model_metrics(engine.log)["m"]
    assert stats["cache_hit_rate"] == 0.6
    assert stats["cache_missing"] == 1
    assert stats["unreported"] == 1
    assert stats["tokens"]["cache_read"] == 300
    assert stats["cost_usd"] == 1.0
    assert stats["average_seconds"] == 5
    assert None not in model_metrics(engine.log)  # 在跑的轮不进入模型汇总。


def test_first_pass_ratio_does_not_treat_successful_retry_as_first_pass(engine):
    reach(engine, "审查")
    reviewer = engine.plugins.get("executor", "reviewer")
    reviewer.usage_value = Usage("review-model", 10, 0, 0, 2, 0.01, "subscription")
    flow = engine.plugins.get("flow", "flow")
    flow.passed = False
    engine.submit(current(engine)["token"], {"outcome": "failed", "summary": "审查未通过"})
    engine.tick()
    engine.clock.now += timedelta(minutes=5)
    engine.tick()
    flow.passed = True
    finish(engine, "交付")
    stats = model_metrics(engine.log)["review-model"]
    assert stats["rounds"] == 2
    assert stats["rates"]["审查"] == {"passed": 0, "total": 1, "missing": 0}
    assert all(r["passed"] is not None for r in engine.log.all("rounds") if r["stage"] == "审查")


def test_selector_receives_history_availability_and_target(engine):
    captured = []
    original = engine.plugins.get("selector", "ordered")
    class RecordingSelector:
        def select(self, target, stage, round_number, history, candidates):
            captured.append((target, stage, round_number, history, candidates))
            result = original.select(target, stage, round_number, history, candidates)
            return Selection(result.config, "测试选择理由") if result else None
    config = deepcopy(engine.rules.data)
    config["selector"] = "recording"
    engine.rules = Rules.validate(config)
    engine.plugins.register("selector", "recording", RecordingSelector())
    reach(engine, "说清楚")
    target, stage, number, history, candidates = captured[-1]
    assert target["name"] == "alpha" and stage == "说清楚" and number == 2
    assert history[0]["usage"]["model_actual"] == "model-writer"
    assert history[0]["outcome"] == "done"
    assert candidates[0]["available"] is True
    assert current(engine)["selection_reason"] == "测试选择理由"


def test_selector_cannot_choose_unknown_or_writer_family_for_review(engine):
    reach(engine, "证据")
    class WrongSelector:
        def select(self, *args):
            return Selection({"use": "writer", "family": "a", "billing": "subscription"}, "故意选错")
    engine.plugins.register("selector", "ordered", WrongSelector())
    finish(engine, "审查")
    assert item(engine)["state"] == "定时"
    assert "挑选逻辑起轮失败" in why(engine.log, "alpha:one")
    assert not engine.active()


def test_ordered_is_installed_as_normal_plugin():
    selector = Registry().get("selector", "ordered")
    assert callable(selector.select)


@pytest.mark.parametrize("value", [
    Usage(input=-1), Usage(cache_read=True), Usage(cache_write=1.5), Usage(output=-2),
    Usage(cost_usd=float("nan")), Usage(model_actual=""), Usage(billing="unknown"),
])
def test_usage_rejects_invalid_known_values(value):
    with pytest.raises(ValueError):
        value.validate()


def test_child_usage_is_accounted_by_actual_model_and_total(engine):
    from workshop.core.models import RoundUsage
    executor = engine.plugins.get("executor", "writer")
    executor.usage_value = RoundUsage(
        Usage("main-model", 100, 20, 0, 10, 0.3, "subscription"),
        [Usage("child-model", 200, 50, 0, 30, 0.5, "subscription")], True)
    reach(engine, "说清楚")
    stats = model_metrics(engine.log)
    assert stats["main-model"]["tokens"]["input"] == 100
    assert stats["child-model"]["tokens"]["input"] == 200
    assert stats["child-model"]["cost_usd"] == 0.5
    assert stats["main-model"]["rounds"] == stats["child-model"]["rounds"] == 1
    text = usage(engine.log, engine.clock())
    assert "$0.8000" in text
    assert "300 / 70 / 0 / 40" in text
    assert "子 agent 模型=child-model" in why(engine.log, "alpha:one")


def test_same_model_main_and_children_do_not_double_count_rounds(engine):
    from workshop.core.models import RoundUsage
    engine.plugins.get("executor", "writer").usage_value = RoundUsage(
        Usage("same", 100, 0, 0, 10, 0.2, "subscription"),
        [Usage("same", 50, 0, 0, 5, 0.1, "subscription")], True)
    reach(engine, "说清楚")
    stats = model_metrics(engine.log)["same"]
    assert stats["rounds"] == 1 and stats["tokens"]["input"] == 150
    assert stats["cost_usd"] == pytest.approx(0.3)


def test_children_not_read_are_explicit_in_why_and_usage(engine):
    from workshop.core.models import RoundUsage
    engine.plugins.get("executor", "writer").usage_value = RoundUsage(
        Usage("main-only", 10, 0, 0, 2, 0.1, "subscription"), children_reported=False)
    reach(engine, "说清楚")
    row = engine.log.all("rounds")[0]
    assert row["children_reported"] is False
    assert "子 agent 未计" in why(engine.log, "alpha:one")
    assert "子 agent 未计" in usage(engine.log, engine.clock())
    assert model_metrics(engine.log)["main-only"]["children_unreported"] == 1


def test_selector_override_precedence(engine):
    config = deepcopy(engine.rules.data)
    config["selector"] = "global"
    config["stage_selectors"] = {"执行": "global-stage"}
    config["targets"]["alpha"]["selector"] = "target"
    config["targets"]["alpha"]["stage_selectors"] = {"执行": "target-stage"}
    rules = Rules.validate(config)
    assert rules.selector("执行", "alpha") == "target-stage"
    assert rules.selector("接单", "alpha") == "target"
    assert rules.selector("执行", "beta") == "global-stage"
    assert rules.selector("接单", "beta") == "global"


def test_selector_override_is_loaded_for_target_stage(engine):
    original = engine.plugins.get("selector", "ordered")
    called = []
    class NamedSelector:
        def select(self, *args):
            called.append(1)
            return original.select(*args)
    config = deepcopy(engine.rules.data)
    config["targets"]["alpha"]["stage_selectors"] = {"接单": "special"}
    engine.rules = Rules.validate(config)
    engine.plugins.register("selector", "special", NamedSelector())
    engine.ingest(event())
    engine.tick()
    assert called == [1] and current(engine)["selector"] == "special"


def test_core_filters_writer_family_and_no_quota_before_selector(engine):
    config = deepcopy(engine.rules.data)
    config["executors"]["reviewer"].append({"use": "acceptor", "family": "c", "billing": "subscription"})
    engine.rules = Rules.validate(config)
    engine.plugins.get("executor", "reviewer").enabled = False
    original = engine.plugins.get("selector", "ordered")
    seen = []
    class RecordingSelector:
        def select(self, target, stage, number, history, candidates):
            if stage == "审查":
                seen.extend(c["config"]["use"] for c in candidates)
            return original.select(target, stage, number, history, candidates)
    engine.plugins.register("selector", "ordered", RecordingSelector())
    reach(engine, "审查")
    assert seen == ["acceptor"]


def test_metered_executor_requires_approval_before_selection(engine):
    from workshop.core.models import Answer
    config = deepcopy(engine.rules.data)
    config["executors"]["writer"]["billing"] = "metered"
    engine.rules = Rules.validate(config)
    engine.ingest(event())
    engine.tick()
    assert item(engine)["state"] == "等你" and not engine.active()
    card = engine.log.all("cards")[0]
    assert card["billing_configs"] and card["payload"]["kind"] == "approval"
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "yes", True)]
    engine.tick()
    assert current(engine)["config"]["billing"] == "metered"
    assert item(engine)["billing_approved"]


def test_subscription_candidate_avoids_unneeded_metered_question(engine):
    config = deepcopy(engine.rules.data)
    config["executors"]["writer"] = [
        {"use": "writer", "family": "a", "billing": "metered"},
        {"use": "reviewer", "family": "b", "billing": "subscription"}]
    engine.rules = Rules.validate(config)
    engine.ingest(event())
    engine.tick()
    assert current(engine)["executor"] == "reviewer"
    assert not engine.log.all("cards")


def test_metered_refusal_does_not_repeat_question_or_select(engine):
    from workshop.core.models import Answer
    config = deepcopy(engine.rules.data)
    config["executors"]["writer"]["billing"] = "metered"
    engine.rules = Rules.validate(config)
    engine.ingest(event())
    engine.tick()
    card = engine.log.all("cards")[0]
    engine.plugins.get("channel", "channel").answers = [Answer(card["id"], "no", False)]
    engine.tick()
    engine.tick()
    assert not engine.active() and item(engine)["wait_for_ready"]
    assert len(engine.log.all("cards")) == 1
