"""大麦平台相关测试。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from gpiao.config import CST
from gpiao.platforms import build_recipe
from gpiao.task import Task, apply_params_to_recipe


def test_damai_template_valid():
    r = build_recipe("damai", "测试", "潘玮柏")
    assert r.app.package == "cn.damai"
    assert r.concert.quantity == 1
    # 大麦默认不生成订单
    assert r.submit_order is False
    assert r.auto_pay is False
    # 必须有详情页标志，否则"底部宽按钮"会在别的页面误匹配
    assert "detail_ready" in r.selectors
    # 购买按钮要有坐标兜底（canvas 绘制没有文本）
    buy = r.selectors["buy_button"]["candidates"]
    assert any("bottomWideButton" in c for c in buy)
    assert any("coord" in c for c in buy)


def test_damai_has_reservation_check_step():
    r = build_recipe("damai", "测试", "潘玮柏")
    actions = [s.get("action") for s in r.steps]
    assert "check_reservation" in actions


def test_result_match_syncs_into_selectors():
    """搜索结果要用更精确的匹配串，避免点到搜索框里刚输入的字。"""
    recipe = build_recipe("damai", "测试", "潘玮柏")
    task = Task(
        id="t",
        name="测试",
        target={"keyword": "潘玮柏", "result_match": "MADLOVE:ULTRA"},
        params={"tier_priority": ["¥380"], "quantity": 1, "attendees": 1},
    )
    apply_params_to_recipe(recipe, task)
    assert recipe.concert.result_match == "MADLOVE:ULTRA"
    cands = recipe.selectors["result_show"]["candidates"]
    assert cands[0] == {"textContains": "MADLOVE:ULTRA"}


def test_damai_task_roundtrip(tmp_path: Path):
    task = Task(
        id="dm",
        name="潘玮柏(大麦)",
        platform="damai",
        schedule=__import__("gpiao.task", fromlist=["Schedule"]).Schedule(
            sale_at=dt.datetime(2026, 11, 27, 13, 22, tzinfo=CST)
        ),
        params={"tier_priority": ["¥380"], "quantity": 1, "attendees": 1},
    )
    from gpiao.task import save_task

    save_task(tmp_path, task)
    raw = json.loads((tmp_path / "tasks" / "dm.json").read_text(encoding="utf-8"))
    assert raw["platform"] == "damai"
    assert raw["schedule"]["sale_at"] == "2026-11-27 13:22:00"
