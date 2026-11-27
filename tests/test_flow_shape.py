"""抢票流程结构回归测试。

这些断言锁住今天实战踩出来的关键结构，改坏了要能立刻发现。
"""

from __future__ import annotations

from gpiao.platforms import build_recipe


def _steps(platform):
    return build_recipe(platform, "测试", "关键字").steps


def _actions(steps, phase=None, action=None):
    return [
        s
        for s in steps
        if (phase is None or s.get("phase") == phase)
        and (action is None or s.get("action") == action)
    ]


# --------------------------------------------------------------------------- #
# 大麦：停在票价页面，到点直接点「确定」
# --------------------------------------------------------------------------- #


def test_damai_arms_on_sku_panel():
    """大麦开抢前必须停在票价页面，不能停在详情页。"""
    steps = _steps("damai")
    pre = [s.get("action") for s in steps if s.get("phase") == "pre"]
    # 进票价页面：tap buy_button 之后必须有 wait sku_panel
    idx_tap = pre.index("tap")
    assert "wait" in pre[idx_tap:]
    assert any(s.get("target") == "sku_panel" for s in steps if s.get("phase") == "pre")


def test_damai_fire_targets_sku_confirm():
    """到点时点的是票价页面上的「确定/立即购买」，不是详情页的按钮。"""
    steps = _steps("damai")
    fire = _actions(steps, phase="fire", action="burst")
    assert len(fire) == 1, "fire 阶段只应有一个 burst（重复会浪费开票窗口）"
    assert fire[0]["target"] == "sku_confirm_text"
    assert fire[0]["until"] == "order_ready"


def test_damai_submit_has_blocker_and_bounded_retry():
    """「立即提交」必须处理限流弹窗，且重试有界+退避（否则触发验证码）。"""
    steps = _steps("damai")
    submit = [s for s in steps if s.get("target") == "submit_button"]
    assert len(submit) == 1
    s = submit[0]
    assert "继续尝试" in s.get("blockers", [])
    assert s.get("retry", 0) <= 8, "重试次数必须有界，否则会被判定为异常行为"
    assert s.get("backoff", 0) >= 1.0, "重试必须有退避，否则会触发风控验证码"


# --------------------------------------------------------------------------- #
# 美团
# --------------------------------------------------------------------------- #


def test_meituan_burst_uses_text_button():
    """美团的 burst 用详情页购买按钮（文本候选），不能盲点坐标。"""
    steps = _steps("meituan")
    fire = _actions(steps, phase="fire", action="burst")
    assert len(fire) == 1
    assert fire[0]["target"] == "buy_button"
    # 有文本候选时禁止坐标盲点
    assert fire[0].get("refresh_every", 0) > 0, "预售演出开售前要下拉刷新"


def test_meituan_post_has_attendee_and_pay():
    steps = _steps("meituan")
    acts = [s.get("action") for s in steps if s.get("phase") == "post"]
    assert "select_attendees" in acts
    assert "tap" in acts


def test_no_duplicate_steps():
    """同 phase + 同 action + 同 target 的步骤不应该重复（会导致重复点击）。

    dismiss 步骤例外：它们的 rules 不同（一个清通用弹层、一个清开售公告）。
    """
    for platform in ("meituan", "damai"):
        steps = _steps(platform)
        keys = [
            (s.get("phase"), s.get("action"), s.get("target"), s.get("until"))
            for s in steps
            if s.get("action") != "dismiss"
        ]
        dup = {k for k in keys if keys.count(k) > 1}
        assert not dup, f"{platform} 有重复步骤: {dup}"
