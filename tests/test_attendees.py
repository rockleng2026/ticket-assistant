"""实名观演人解析：美团(ImageView+像素) 与 大麦(CheckBox+checked 属性) 两种形态。"""

from __future__ import annotations

from gpiao.config import Recipe
from gpiao.flow import build_context, run_steps

from .fake_device import FakeDevice, FakeNode

DAMAI_ORDER_XML = """<hierarchy>
  <node text="确认购买" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[196,142][1264,294]" />
  <node text="实名观演人" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[74,988][284,1045]" />
  <node text="冷志勇" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[74,1177][242,1243]" />
  <node text="身份证" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[74,1257][200,1306]" />
  <node text="513************191" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[232,1257][1029,1306]" />
  <node text="" resource-id="" class="android.widget.CheckBox" content-desc="" clickable="true" checked="true" enabled="true" bounds="[1113,1177][1190,1254]" />
  <node text="李洪霞" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[74,1348][242,1414]" />
  <node text="身份证" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[74,1428][200,1477]" />
  <node text="421************147" resource-id="" class="android.widget.TextView" content-desc="" clickable="false" enabled="true" bounds="[232,1428][1029,1477]" />
  <node text="" resource-id="" class="android.widget.CheckBox" content-desc="" clickable="true" checked="false" enabled="true" bounds="[1113,1348][1190,1425]" />
</hierarchy>"""


class DamaiOrderDevice(FakeDevice):
    """模拟大麦订单页：CheckBox 节点带 checked 属性。"""

    def __init__(self):
        super().__init__(screen_size=(1264, 2780))
        self.checked_state = {"李洪霞": False}
        self.clicked = []

    def dump_hierarchy(self) -> str:
        xml = DAMAI_ORDER_XML
        # 已勾选的第二个观演人 -> checked=true
        if self.checked_state.get("李洪霞"):
            xml = xml.replace(
                '<node text="" resource-id="" class="android.widget.CheckBox" content-desc="" clickable="true" checked="false" enabled="true" bounds="[1113,1348][1190,1425]" />',
                '<node text="" resource-id="" class="android.widget.CheckBox" content-desc="" clickable="true" checked="true" enabled="true" bounds="[1113,1348][1190,1425]" />',
            )
        return xml

    def click(self, x: int, y: int) -> None:
        super().click(x, y)
        # 点到第二个勾选框 -> 勾上
        if 1340 <= y <= 1430 and x > 1000:
            self.checked_state["李洪霞"] = True


def test_damai_attendee_checkbox_selected():
    """大麦：CheckBox 的 checked 属性直接可读，应该认出已选/未选并补选。"""
    device = DamaiOrderDevice()
    recipe = Recipe.from_dict(
        {
            "name": "t",
            "concert": {"quantity": 2, "attendees": 2},
            "steps": [{"action": "select_attendees", "count": 2}],
        }
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.results[-1].ok is True
    assert ctx.results[-1].detail == "2"
    # 第二个观演人被点开了
    assert device.checked_state["李洪霞"] is True
    # 日志里能看到名字
    assert any("李洪霞" in line for line in ctx.trace)
    assert any("冷志勇" in line for line in ctx.trace)


def test_damai_attendee_already_satisfied():
    """两个都勾好时不应该有多余点击。"""
    device = DamaiOrderDevice()
    device.checked_state["李洪霞"] = True
    recipe = Recipe.from_dict(
        {
            "name": "t",
            "concert": {"quantity": 2, "attendees": 2},
            "steps": [{"action": "select_attendees", "count": 2}],
        }
    )
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.results[-1].ok is True
    assert device.clicks == []


def test_attendee_count_must_match_quantity():
    """票数和观演人数不一致时，配方校验要拦下。"""
    import pytest

    from gpiao.config import ConfigError

    with pytest.raises(ConfigError):
        Recipe.from_dict(
            {
                "name": "t",
                "concert": {"quantity": 2, "attendees": 1},
                "steps": [{"action": "stop"}],
            }
        )
