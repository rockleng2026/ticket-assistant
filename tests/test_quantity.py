"""数量选择的回归测试：不能多买票。

大麦的「预约」机制会把票张数预先设好（比如 2 张），
如果脚本无条件点一次「+」就会变成 3 张 —— 多买一张！
"""

from __future__ import annotations

from gpiao.config import Recipe
from gpiao.flow import build_context, run_steps

from .fake_device import FakeDevice, FakeNode


class QtyDevice(FakeDevice):
    """模拟带「N张」文本的数量行。"""

    def __init__(self, current_qty: int):
        super().__init__(screen_size=(1264, 2780))
        self.current_qty = current_qty
        self.plus_clicks = 0

    def dump_hierarchy(self) -> str:
        return (
            '<hierarchy>'
            f'<node text="数量" class="android.widget.TextView" clickable="false" enabled="true" bounds="[74,2354][158,2403]" />'
            f'<node text="{self.current_qty}张" class="android.widget.TextView" clickable="false" enabled="true" bounds="[940,2370][1018,2436]" />'
            '<node text="" class="android.widget.ImageView" clickable="true" enabled="true" bounds="[1071,2354][1169,2452]" />'
            '</hierarchy>'
        )

    def click(self, x: int, y: int) -> None:
        super().click(x, y)
        if 2350 <= y <= 2460 and x > 1000:
            self.plus_clicks += 1
            self.current_qty += 1


def _recipe(quantity: int):
    return Recipe.from_dict(
        {
            "name": "t",
            "concert": {"quantity": quantity, "attendees": quantity},
            "selectors": {
                "qty_plus": {
                    "candidates": [
                        {"qtyPlus": True},
                        {"descContains": "增加"},
                        {"text": "+"},
                        {"coord": [0.8861, 0.8644]},
                    ]
                }
            },
            "steps": [{"action": "select_quantity"}],
        }
    )


def test_no_click_when_qty_already_matches():
    """预约已设好 2 张 -> 不能再点「+」。"""
    device = QtyDevice(current_qty=2)
    ctx = build_context(device, _recipe(2))
    run_steps(ctx, ctx.recipe.steps, phase=None)
    assert device.plus_clicks == 0
    assert device.current_qty == 2


def test_clicks_only_the_missing_tickets():
    """当前 1 张、目标 2 张 -> 只点一次「+」。"""
    device = QtyDevice(current_qty=1)
    ctx = build_context(device, _recipe(2))
    run_steps(ctx, ctx.recipe.steps, phase=None)
    assert device.plus_clicks == 1
    assert device.current_qty == 2


def test_never_exceeds_target():
    """当前已经 3 张、目标 2 张 -> 绝对不能再加。"""
    device = QtyDevice(current_qty=3)
    ctx = build_context(device, _recipe(2))
    run_steps(ctx, ctx.recipe.steps, phase=None)
    assert device.plus_clicks == 0
    assert device.current_qty == 3
