"""测试用的假设备：模拟 uiautomator2 的选择器接口。

不依赖真机/模拟器，可以在 CI 里跑。用来验证：
  * 选择器候选的降级顺序
  * burst 连点的节奏与提前停止
  * 到点触发的计时精度
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class FakeNode:
    text: str = ""
    desc: str = ""
    resourceId: str = ""
    className: str = "android.widget.TextView"
    clickable: bool = False
    bounds: Tuple[int, int, int, int] = (0, 0, 100, 100)
    enabled: bool = True

    @property
    def info(self) -> Dict[str, Any]:
        x1, y1, x2, y2 = self.bounds
        return {
            "text": self.text,
            "contentDescription": self.desc,
            "desc": self.desc,
            "resourceId": self.resourceId,
            "className": self.className,
            "clickable": self.clickable,
            "enabled": self.enabled,
            "bounds": [x1, y1, x2, y2],
        }


class FakeSelector:
    """模拟 uiautomator2 的 UiObject 集合。"""

    def __init__(self, nodes: List[FakeNode]):
        self._nodes = nodes

    @property
    def count(self) -> int:
        return len(self._nodes)

    def __getitem__(self, idx: int) -> FakeNode:
        return self._nodes[idx]

    def __bool__(self) -> bool:
        return bool(self._nodes)

    def exists(self) -> bool:
        return bool(self._nodes)

    def click(self) -> bool:
        return True


@dataclass
class FakeDevice:
    """假的 uiautomator2 设备。"""

    screen_size: Tuple[int, int] = (1080, 1920)
    nodes: List[FakeNode] = field(default_factory=list)
    clicks: List[Tuple[int, int]] = field(default_factory=list)
    typed: List[str] = field(default_factory=list)
    pressed: List[str] = field(default_factory=list)
    logs: List[str] = field(default_factory=list)
    # 每次查询后要变成的样子（模拟页面跳转）
    on_tap: Optional[Dict[str, Any]] = None
    # 模拟锁屏
    locked: bool = False
    screen_on: bool = True

    def screen(self):
        from gpiao.device import ScreenInfo

        return ScreenInfo(width=self.screen_size[0], height=self.screen_size[1])

    @property
    def u2(self):
        """模拟 uiautomator2 原始对象（选择器构造入口）。"""
        return self

    def tap(self, x: int, y: int) -> None:
        self.click(x, y)

    def dump_hierarchy(self) -> str:
        import xml.sax.saxutils as sax

        parts = []
        for n in self.nodes:
            x1, y1, x2, y2 = n.bounds
            parts.append(
                '<node text="{t}" content-desc="{d}" resource-id="{r}" class="{c}" '
                'clickable="{cl}" enabled="true" '
                'bounds="[{x1},{y1}][{x2},{y2}]" />'.format(
                    t=sax.escape(n.text),
                    d=sax.escape(n.desc),
                    r=n.resourceId,
                    c=n.className,
                    cl="true" if n.clickable else "false",
                    x1=x1,
                    y1=y1,
                    x2=x2,
                    y2=y2,
                )
            )
        return '<hierarchy>' + "".join(parts) + "</hierarchy>"

    def __call__(self, **kwargs) -> FakeSelector:
        matched = []
        for node in self.nodes:
            if "text" in kwargs and node.text != kwargs["text"]:
                continue
            if "textContains" in kwargs and kwargs["textContains"] not in node.text:
                continue
            if "description" in kwargs and node.desc != kwargs["description"]:
                continue
            if "descriptionContains" in kwargs and kwargs["descriptionContains"] not in node.desc:
                continue
            if "resourceId" in kwargs and node.resourceId != kwargs["resourceId"]:
                continue
            if "className" in kwargs and node.className != kwargs["className"]:
                continue
            matched.append(node)
        return FakeSelector(matched)

    def xpath(self, expr: str) -> FakeSelector:
        # 仅支持最简单的 //*[@text='xxx']
        if "[@text=" in expr:
            target = expr.split("[@text=")[1].split("]")[0].strip("'\"")
            return FakeSelector([n for n in self.nodes if n.text == target])
        return FakeSelector(list(self.nodes))

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))
        self.logs.append(f"click({x},{y})")
        if self.on_tap:
            self.nodes = list(self.on_tap.get("nodes", []))
            self.on_tap = None

    def swipe(self, *args, **kwargs) -> None:
        self.logs.append("swipe")

    def send_keys(self, text: str) -> None:
        self.typed.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)

    def shell(self, cmd: str):
        class R:
            output = ""
            exit_code = 0

        return R()
