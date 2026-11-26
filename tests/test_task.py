"""任务层测试：创建 / 持久化 / 参数同步 / 平台模板。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from gpiao.config import CST, load_recipe
from gpiao.platforms import build_recipe
from gpiao.task import (
    Schedule,
    Task,
    apply_params_to_recipe,
    list_tasks,
    load_task,
    save_task,
    slugify,
)


def _task(**kw) -> Task:
    base = dict(
        id="demo",
        name="演示任务",
        platform="meituan",
        schedule=Schedule(
            sale_at=dt.datetime(2026, 11, 27, 13, 22, tzinfo=CST),
            prepare_lead_minutes=60,
            lead_seconds=0.5,
        ),
        target={"keyword": "潘玮柏", "show_name": "潘玮柏演唱会"},
        params={
            "tier_priority": ["¥680"],
            "quantity": 1,
            "attendees": 1,
            "attendees_filled": True,
        },
        recipe="recipes/demo.yaml",
    )
    base.update(kw)
    return Task(**base)


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #


def test_slugify():
    assert slugify("潘玮柏MADLOVE:ULTRA巡回演唱会-深圳站") == "潘玮柏MADLOVE-ULTRA巡回演唱会-深圳站"
    assert slugify("a/b c") == "a-b-c"
    assert slugify("") == "task"


def test_task_roundtrip(tmp_path: Path):
    t = _task()
    p = save_task(tmp_path, t)
    assert p.exists()
    loaded = load_task(tmp_path, "demo")
    assert loaded.name == t.name
    assert loaded.params["tier_priority"] == ["¥680"]
    assert loaded.schedule.sale_at == t.schedule.sale_at
    assert loaded.schedule.prepare_lead_minutes == 60


def test_task_list(tmp_path: Path):
    save_task(tmp_path, _task(id="a"))
    save_task(tmp_path, _task(id="b"))
    ids = {t.id for t in list_tasks(tmp_path)}
    assert ids == {"a", "b"}


def test_schedule_serialization():
    s = Schedule(sale_at=dt.datetime(2026, 11, 27, 13, 22, tzinfo=CST))
    d = s.to_dict()
    assert d["sale_at"] == "2026-11-27 13:22:00"
    back = Schedule.from_dict(d)
    assert back.sale_at == s.sale_at


def test_countdown_text():
    t = _task()
    t.schedule.sale_at = dt.datetime.now(CST) + dt.timedelta(days=1, hours=2)
    assert "1天" in t.countdown_text()
    t.schedule.sale_at = dt.datetime.now(CST) - dt.timedelta(minutes=1)
    assert t.countdown_text() == "已开售"


def test_history_log():
    t = _task()
    t.log("armed", note="x")
    assert t.history[-1]["event"] == "armed"


# --------------------------------------------------------------------------- #
# 参数同步到配方
# --------------------------------------------------------------------------- #


def test_apply_params_to_recipe():
    recipe = build_recipe("meituan", "潘玮柏演唱会", "潘玮柏")
    t = _task()
    t.params["tier_priority"] = ["¥680", "¥480"]
    t.params["quantity"] = 2
    t.params["attendees"] = 2
    apply_params_to_recipe(recipe, t)

    assert recipe.concert.tier_priority == ["¥680", "¥480"]
    assert recipe.concert.quantity == 2
    assert recipe.concert.attendees == 2
    assert recipe.concert.search_keyword == "潘玮柏"
    assert recipe.sale.lead_seconds == 0.5
    assert recipe.sale.at == t.schedule.sale_at

    # 价格优先级要同步进 loop 里的 select_tier
    found = None
    for step in recipe.steps:
        if step.get("action") == "loop":
            for inner in step.get("steps", []):
                if inner.get("action") == "select_tier":
                    found = inner["priority"]
    assert found == ["¥680", "¥480"]


def test_recipe_to_dict_writes_updated_sale_time(tmp_path: Path):
    """改了 sale.at 之后要真的写进 YAML（曾经只写了旧的 at_text）。"""
    from gpiao.config import save_recipe

    recipe = build_recipe("meituan", "测试", "关键词")
    recipe.sale.at = dt.datetime(2026, 11, 27, 13, 22, tzinfo=CST)
    p = tmp_path / "r.yaml"
    save_recipe(recipe, p)
    again = load_recipe(p)
    assert again.sale.at == dt.datetime(2026, 11, 27, 13, 22, tzinfo=CST)
    assert "2026-11-27 13:22:00" in p.read_text(encoding="utf-8")


def test_attendee_count_not_hardcoded():
    """观演人数必须跟着 concert.attendees 走，不能写死在步骤里。"""
    r = build_recipe("meituan", "测试", "关键词")
    for step in r.steps:
        if step.get("action") == "select_attendees":
            assert "count" not in step


# --------------------------------------------------------------------------- #
# 平台模板
# --------------------------------------------------------------------------- #


def test_meituan_template_valid():
    r = build_recipe("meituan", "测试", "关键词")
    assert r.app.package == "com.sankuai.meituan"
    assert r.steps
    names = {s.get("target") for s in r.steps if s.get("target")}
    assert "buy_button" in names
    assert r.selectors["buy_button"]["candidates"]


def test_damai_template_valid():
    r = build_recipe("damai", "测试", "关键词")
    assert r.app.package == "cn.damai"
    assert r.steps
    # 大麦模板里必须留了「待标定」的可点坐标兜底
    assert any(
        "coord" in c for c in r.selectors["buy_button"]["candidates"]
    )


def test_unknown_platform():
    import pytest

    with pytest.raises(ValueError):
        build_recipe("unknown", "x", "y")
