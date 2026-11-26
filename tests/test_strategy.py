"""抢票策略相关测试：票档优先级、换档重试、参数问询。"""

from __future__ import annotations

from gpiao.ask import parse_prices
from gpiao.config import Recipe
from gpiao.flow import build_context, run_steps

from .fake_device import FakeDevice, FakeNode


def _recipe(steps, selectors=None, tier_priority=None):
    return Recipe.from_dict(
        {
            "name": "t",
            "concert": {
                "quantity": 1,
                "tier_priority": tier_priority or ["¥680", "¥480", "¥880"],
            },
            "sale": {"at": "2026-03-14 19:30:00"},
            "selectors": selectors or {},
            "steps": steps,
        }
    )


# --------------------------------------------------------------------------- #
# 参数问询
# --------------------------------------------------------------------------- #


def test_parse_prices_variants():
    assert parse_prices("680-480-880") == ["¥680", "¥480", "¥880"]
    assert parse_prices("¥680，¥480、¥1180") == ["¥680", "¥480", "¥1180"]
    assert parse_prices("680, 480") == ["¥680", "¥480"]
    assert parse_prices("680元") == ["¥680"]
    assert parse_prices("") == []


# --------------------------------------------------------------------------- #
# 票档优先级
# --------------------------------------------------------------------------- #


def test_select_tier_picks_first_available():
    """¥680 能选就直接选 680，不浪费时间看后面的档。"""
    device = FakeDevice(
        nodes=[
            FakeNode(text="¥680", clickable=True, bounds=(660, 1206, 775, 1283)),
            FakeNode(text="¥480", clickable=True, bounds=(920, 1206, 1036, 1283)),
            FakeNode(text="数量", clickable=False, bounds=(70, 2409, 162, 2471)),
        ]
    )
    recipe = _recipe([{"action": "select_tier"}])
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    # 点了 680 就停，480 不该被点
    assert len(device.clicks) == 1
    assert ctx.tier_cursor == 1


def test_select_tier_falls_back_to_next():
    """¥680 点不动（售罄），自动换 ¥480。"""
    device = FakeDevice(
        nodes=[
            FakeNode(text="¥680", clickable=True, bounds=(660, 1206, 775, 1283)),
            FakeNode(text="¥480", clickable=True, bounds=(920, 1206, 1036, 1283)),
        ]
    )
    # 第一次点 ¥680 时页面没有"数量"，点了 ¥480 之后才有
    original_click = device.click
    state = {"clicks": 0}

    def click(x, y):
        original_click(x, y)
        state["clicks"] += 1
        if state["clicks"] >= 3:  # 680 点两次都没中，第三次(480)成功
            device.nodes = device.nodes + [
                FakeNode(text="数量", clickable=False, bounds=(70, 2409, 162, 2471))
            ]

    device.click = click  # type: ignore[method-assign]
    recipe = _recipe([{"action": "select_tier"}])
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.tier_cursor == 2


def test_select_tier_cursor_advances_across_rounds():
    """loop 里第二轮要接着上一轮的游标往下走，不重复试同一档。"""
    device = FakeDevice(
        nodes=[
            FakeNode(text="¥680", clickable=True, bounds=(660, 1206, 775, 1283)),
            FakeNode(text="¥480", clickable=True, bounds=(920, 1206, 1036, 1283)),
            FakeNode(text="数量", clickable=False, bounds=(70, 2409, 162, 2471)),
        ]
    )
    recipe = _recipe([{"action": "select_tier"}])
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.tier_cursor == 1
    # 第二轮从 ¥480 开始
    recipe2 = _recipe([{"action": "select_tier"}])
    ctx2 = build_context(device, recipe2)
    ctx2.tier_cursor = ctx.tier_cursor
    before = len(device.clicks)
    run_steps(ctx2, recipe2.steps, phase=None)
    assert ctx2.tier_cursor == 2
    assert len(device.clicks) > before


# --------------------------------------------------------------------------- #
# loop 动作
# --------------------------------------------------------------------------- #


def test_loop_stops_when_until_satisfied():
    device = FakeDevice(
        nodes=[
            FakeNode(text="¥680", clickable=True, bounds=(660, 1206, 775, 1283)),
            FakeNode(text="提交订单", clickable=True, bounds=(830, 2579, 1070, 2660)),
        ]
    )
    recipe = _recipe(
        [
            {
                "action": "loop",
                "until": "ready",
                "max_rounds": 3,
                "budget": 5,
                "steps": [{"action": "tap", "target": "buy"}],
            }
        ],
        {"buy": [{"text": "¥680"}], "ready": [{"text": "提交订单"}]},
    )
    ctx = build_context(device, recipe)
    ok = run_steps(ctx, recipe.steps, phase=None)
    assert ok is True
    # 条件一开始就满足，只跑一轮
    assert len(device.clicks) == 1


def test_loop_gives_up_after_max_rounds():
    device = FakeDevice(nodes=[])
    recipe = _recipe(
        [
            {
                "action": "loop",
                "until": "ready",
                "max_rounds": 2,
                "budget": 5,
                "steps": [{"action": "tap", "target": "buy"}],
            }
        ],
        {"buy": [{"text": "¥680"}], "ready": [{"text": "提交订单"}]},
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.results[-1].ok is False


# --------------------------------------------------------------------------- #
# 付款开关
# --------------------------------------------------------------------------- #


def test_requires_submit_gated_by_recipe_flag():
    device = FakeDevice(
        nodes=[FakeNode(text="立即支付", clickable=True, bounds=(830, 2579, 1070, 2660))]
    )
    recipe = _recipe(
        [{"action": "tap", "target": "pay", "requires_submit": True}],
        {"pay": [{"text": "立即支付"}]},
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert device.clicks == []  # submit_order 默认 false，被拦下

    recipe.submit_order = True
    ctx2 = build_context(device, recipe)
    run_steps(ctx2, recipe.steps, phase=None)
    assert len(device.clicks) == 1


def test_requires_pay_gated_by_auto_pay_flag():
    device = FakeDevice(
        nodes=[FakeNode(text="确认支付", clickable=True, bounds=(38, 2506, 1225, 2674))]
    )
    recipe = _recipe(
        [{"action": "tap", "target": "pay", "requires_pay": True}],
        {"pay": [{"text": "确认支付"}]},
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert device.clicks == []
    recipe.auto_pay = True
    ctx2 = build_context(device, recipe)
    run_steps(ctx2, recipe.steps, phase=None)
    assert len(device.clicks) == 1
