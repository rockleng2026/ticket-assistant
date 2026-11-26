"""核心逻辑测试（不依赖真机）。"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from gpiao.clock import CST, build_plan, sleep_until
from gpiao.config import ConfigError, Recipe, _parse_datetime
from gpiao.flow import build_context, run_steps
from gpiao.selectors import Target

from .fake_device import FakeDevice, FakeNode


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def test_parse_sale_time_variants():
    expected = dt.datetime(2026, 3, 14, 19, 30, 0, tzinfo=CST)
    for text in (
        "2026-03-14 19:30:00",
        "2026-03-14 19:30",
        "2026/03/14 19:30:00",
        "2026-03-14T19:30:00",
    ):
        assert _parse_datetime(text, CST) == expected


def test_parse_sale_time_with_offset():
    got = _parse_datetime("2026-03-14 11:30:00+00:00", CST)
    assert got.astimezone(CST).hour == 19


def test_recipe_roundtrip(tmp_path):
    raw = {
        "name": "测试",
        "concert": {"name": "x", "search_keyword": "石凯", "quantity": 2},
        "sale": {"at": "2026-03-14 19:30:00"},
        "selectors": {"buy": {"candidates": [{"text": "立即购买"}]}},
        "steps": [{"action": "tap", "target": "buy"}],
    }
    recipe = Recipe.from_dict(raw)
    assert recipe.concert.quantity == 2
    assert recipe.sale.at is not None
    assert recipe.sale.at.hour == 19
    assert "buy" in recipe.selectors


def test_recipe_rejects_bad_quantity():
    with pytest.raises(ConfigError):
        Recipe.from_dict({"steps": [{"action": "stop"}], "concert": {"quantity": 0}})


def test_recipe_rejects_missing_steps():
    with pytest.raises(ConfigError):
        Recipe.from_dict({"name": "x"})


# --------------------------------------------------------------------------- #
# 计时
# --------------------------------------------------------------------------- #


def test_sleep_until_precision():
    target = time.monotonic() + 0.25
    sleep_until(target)
    assert time.monotonic() >= target - 0.01


def test_trigger_plan_lead():
    target = dt.datetime(2026, 3, 14, 19, 30, 0, tzinfo=CST)
    plan = build_plan(target, lead_seconds=1.5)
    assert plan.fire_epoch == plan.target_epoch - 1.5


# --------------------------------------------------------------------------- #
# 选择器
# --------------------------------------------------------------------------- #


def test_target_falls_back_through_candidates():
    device = FakeDevice(nodes=[FakeNode(text="马上开抢", clickable=True, bounds=(100, 900, 300, 980))])
    target = Target.from_dict("buy", {"candidates": [{"text": "立即购买"}, {"textContains": "开抢"}]})
    match = target.find(device, device.screen().size)
    assert match is not None
    assert match.text == "马上开抢"


def test_target_prefers_clickable():
    device = FakeDevice(
        nodes=[
            FakeNode(text="购买", clickable=False, bounds=(0, 0, 10, 10)),
            FakeNode(text="购买", clickable=True, bounds=(500, 1000, 700, 1100)),
        ]
    )
    target = Target.from_dict("buy", [{"text": "购买"}])
    match = target.find(device, device.screen().size)
    assert match is not None
    assert match.clickable is True


def test_target_coord_fallback():
    device = FakeDevice(nodes=[])
    target = Target.from_dict("buy", {"candidates": [{"coord": [0.5, 0.9]}]})
    assert target.exists(device, device.screen().size) is True
    assert target.click(device, device.screen().size) is True
    assert device.clicks == [(540, 1728)]


def test_target_alias_first():
    device = FakeDevice(nodes=[FakeNode(text="立即购买", clickable=True)])
    target = Target.from_dict("buy", {"candidates": [{"text": "立即购买"}], "aliases": ["立即购买"]})
    assert target.exists(device, device.screen().size) is True


# --------------------------------------------------------------------------- #
# 流程
# --------------------------------------------------------------------------- #


def _recipe_with_steps(steps, selectors=None):
    return Recipe.from_dict(
        {
            "name": "t",
            "concert": {"quantity": 1},
            "sale": {"at": "2026-03-14 19:30:00"},
            "selectors": selectors or {},
            "steps": steps,
        }
    )


def test_flow_tap_steps():
    device = FakeDevice(nodes=[FakeNode(text="立即购买", clickable=True, bounds=(500, 1800, 600, 1900))])
    recipe = _recipe_with_steps([{"action": "tap", "target": "buy"}], {"buy": [{"text": "立即购买"}]})
    ctx = build_context(device, recipe)
    ok = run_steps(ctx, recipe.steps, phase=None)
    assert ok is True
    assert len(device.clicks) == 1


def test_flow_burst_stops_when_page_changes():
    buy = FakeNode(text="立即购买", clickable=True, bounds=(500, 1800, 600, 1900))
    order = FakeNode(text="提交订单", clickable=True, bounds=(500, 1800, 600, 1900))
    device = FakeDevice(nodes=[buy])
    device.on_tap = {"nodes": [order]}
    recipe = _recipe_with_steps(
        [
            {
                "action": "burst",
                "target": "buy",
                "until": "confirm",
                "window": 5.0,
                "interval_ms": 10,
            }
        ],
        {"buy": [{"text": "立即购买"}], "confirm": [{"text": "提交订单"}]},
    )
    ctx = build_context(device, recipe)
    start = time.monotonic()
    ok = run_steps(ctx, recipe.steps, phase=None)
    elapsed = time.monotonic() - start
    assert ok is True
    # 第一次点击后就跳转到订单页，应立刻停止，不应连点多次
    assert len(device.clicks) == 1
    assert elapsed < 3.0


def test_flow_burst_gives_up_after_window():
    device = FakeDevice(nodes=[FakeNode(text="立即购买", clickable=True)])
    recipe = _recipe_with_steps(
        [{"action": "burst", "target": "buy", "until": "confirm", "window": 0.3, "interval_ms": 20}],
        {"buy": [{"text": "立即购买"}], "confirm": [{"text": "提交订单"}]},
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert len(device.clicks) >= 3


def test_flow_optional_step_failure_does_not_break():
    device = FakeDevice(nodes=[])
    recipe = _recipe_with_steps(
        [
            {"action": "tap", "target": "missing", "optional": True},
            {"action": "stop", "message": "结束"},
        ],
        {},
    )
    ctx = build_context(device, recipe)
    ok = run_steps(ctx, recipe.steps, phase=None)
    assert ok is True


def test_flow_phase_filtering():
    device = FakeDevice(nodes=[FakeNode(text="立即购买", clickable=True)])
    recipe = _recipe_with_steps(
        [
            {"action": "tap", "target": "buy", "phase": "pre"},
            {"action": "tap", "target": "buy", "phase": "fire"},
        ],
        {"buy": [{"text": "立即购买"}]},
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase="pre")
    assert len(device.clicks) == 1
    run_steps(ctx, recipe.steps, phase="fire")
    assert len(device.clicks) == 2
