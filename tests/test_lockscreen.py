"""锁屏 / 唤醒相关的测试。"""

from __future__ import annotations

from gpiao.config import Recipe
from gpiao.flow import build_context, run_steps

from .fake_device import FakeDevice, FakeNode


class LockedDevice(FakeDevice):
    """模拟带锁屏的设备。"""

    def keyguard_showing(self) -> bool:
        return self.locked

    def unlock(self) -> bool:
        if self.locked:
            self.locked = False
            return True
        return True

    def wake_up(self) -> None:
        self.screen_on = True


def _recipe(steps):
    return Recipe.from_dict(
        {
            "name": "t",
            "concert": {"quantity": 1},
            "sale": {"at": "2026-03-14 19:30:00"},
            "steps": steps,
        }
    )


def test_wake_ok_when_unlocked():
    device = LockedDevice(locked=False)
    recipe = _recipe([{"action": "wake"}])
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.results[-1].ok is True
    assert ctx.results[-1].detail == "awake"


def test_wake_unlocks_when_possible():
    device = LockedDevice(locked=True)
    recipe = _recipe([{"action": "wake"}])
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.results[-1].ok is True
    assert ctx.results[-1].detail == "unlocked"
    assert device.locked is False


def test_wake_fails_when_locked_cannot_unlock():
    class Stubborn(LockedDevice):
        def unlock(self) -> bool:
            return False

    device = Stubborn(locked=True)
    recipe = _recipe([{"action": "wake"}])
    ctx = build_context(device, recipe)
    run_steps(ctx, recipe.steps, phase=None)
    assert ctx.results[-1].ok is False
    assert ctx.results[-1].detail == "locked"
    # 失败信息里要给出可执行的修复命令
    assert any("lockscreen" in line for line in ctx.trace)


def test_wake_is_first_step_in_templates():
    from gpiao.platforms import build_recipe

    for platform in ("meituan", "damai"):
        r = build_recipe(platform, "测试", "关键词")
        assert r.steps[0]["action"] == "wake", platform
