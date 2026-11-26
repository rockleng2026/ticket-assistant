"""抢票流程引擎：数据驱动的步骤序列 + 分阶段触发。

配方里的 steps 每一步形如：

    {"action": "tap", "target": "buy_button", "phase": "pre"}
    {"action": "burst", "target": "buy_button", "until": "order_confirm", "phase": "fire"}
    {"action": "submit_order", "phase": "post"}

phase 含义：
  pre  —— 开售前完成所有准备工作（选场次、选票档、选数量、停在购买页）
  fire —— 到点瞬间执行（连点购买按钮，直到页面跳转）
  post —— 抢购成功后立刻执行（提交订单），绝不触碰支付
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .clock import TriggerPlan, fire_now
from .config import Recipe
from .device import Device
from .selectors import Target, build_targets

log = logging.getLogger("gpiao.flow")


class FlowError(Exception):
    """流程中断。"""


class StepResult:
    def __init__(self, action: str, ok: bool, detail: str = ""):
        self.action = action
        self.ok = ok
        self.detail = detail


@dataclass
class FlowContext:
    device: Device
    recipe: Recipe
    targets: Dict[str, Target] = field(default_factory=dict)
    fired: bool = False
    results: List[StepResult] = field(default_factory=list)
    trace: List[str] = field(default_factory=list)
    # 票档游标：select_tier 每成功一档就往后推进，配合 loop 动作实现
    # 「这一档没票 → 换下一档 → 重新走一遍下单流程」
    tier_cursor: int = 0

    def target(self, name: str) -> Target:
        t = self.targets.get(name)
        if t is None:
            raise FlowError(f"配方中未定义控件: {name}")
        return t

    def has_target(self, name: str) -> bool:
        return name in self.targets

    def screen_size(self):
        return self.device.screen().size


def _log_step(ctx: FlowContext, msg: str) -> None:
    ctx.trace.append(f"{time.strftime('%H:%M:%S')} {msg}")
    log.info(msg)


def _safe_event(ctx: FlowContext, name: str, **fields) -> None:
    fn = getattr(ctx.device, "event", None)
    if callable(fn):
        try:
            fn(name, **fields)
        except Exception:  # noqa: BLE001
            pass


def _safe_shot(ctx: FlowContext, prefix: str) -> None:
    fn = getattr(ctx.device, "screenshot", None)
    if callable(fn):
        try:
            fn(prefix)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 各类动作实现
# --------------------------------------------------------------------------- #


def _act_launch(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    app = ctx.recipe.app
    if step.get("restart", True):
        ctx.device.app_stop(app.package)
        time.sleep(step.get("stop_settle", 1.2))
    ctx.device.app_start(app.package, app.main_activity)
    time.sleep(step.get("settle", 2.5))
    if app.deep_link:
        ctx.device.open_deep_link(app.deep_link)
        time.sleep(step.get("settle", 2.5))
    pkg, act = ctx.device.app_current()
    _log_step(ctx, f"启动 App: {pkg}/{act}")
    return StepResult("launch", True, pkg)


def _act_deep_link(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    url = step["url"]
    ctx.device.open_deep_link(url)
    time.sleep(step.get("settle", 3.0))
    _log_step(ctx, f"打开链接: {url}")
    return StepResult("deep_link", True, url)


def _act_search(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """在 App 内搜索：点搜索框 -> 输入关键词 -> 回车。"""
    keyword = step.get("text") or ctx.recipe.concert.search_keyword
    entry = ctx.target(step.get("entry", "search_entry"))
    ok = entry.click(ctx.device, ctx.screen_size())
    if not ok:
        return StepResult("search", False, "未找到搜索入口")
    time.sleep(step.get("settle", 0.8))
    ctx.device.type_text(keyword)
    time.sleep(step.get("settle", 0.5))
    ctx.device.press("enter")
    time.sleep(step.get("settle", 2.5))
    _log_step(ctx, f"搜索: {keyword}")
    return StepResult("search", True, keyword)


def _act_tap(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    name = step["target"]
    t = ctx.target(name)
    ok = t.click(ctx.device, ctx.screen_size())
    time.sleep(step.get("settle", 0.6))
    if ok:
        _log_step(ctx, f"点击 {name} 成功")
    else:
        _log_step(ctx, f"点击 {name} 失败（候选全部未命中）")
        _safe_shot(ctx, f"fail-{name}")
    return StepResult("tap", ok, name)


def _act_tap_text(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    text = step["text"]
    target = Target.from_dict("inline", {"text": text})
    ok = target.click(ctx.device, ctx.screen_size())
    time.sleep(step.get("settle", 0.6))
    _log_step(ctx, f"点击文本 {text!r}: {'成功' if ok else '失败'}")
    return StepResult("tap_text", ok, text)


def _act_wait(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    name = step["target"]
    t = ctx.target(name)
    timeout = float(step.get("timeout", t.timeout))
    m = t.wait(ctx.device, timeout, ctx.screen_size())
    ok = m is not None
    if not ok:
        ctx.device.screenshot(f"wait-timeout-{name}")
    _log_step(ctx, f"等待 {name}: {'出现' if ok else '超时'}")
    return StepResult("wait", ok, name)


def _act_wait_text(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    text = step["text"]
    target = Target.from_dict("inline", {"text": text})
    timeout = float(step.get("timeout", 10))
    m = target.wait(ctx.device, timeout, ctx.screen_size())
    ok = m is not None
    if not ok:
        _safe_shot(ctx, "wait-text-timeout")
    _log_step(ctx, f"等待文本 {text!r}: {'出现' if ok else '超时'}")
    return StepResult("wait_text", ok, text)


def _act_wait_gone(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    name = step["target"]
    t = ctx.target(name)
    timeout = float(step.get("timeout", 15))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not t.exists(ctx.device, ctx.screen_size()):
            _log_step(ctx, f"{name} 已消失")
            return StepResult("wait_gone", True, name)
        time.sleep(0.15)
    _log_step(ctx, f"{name} 未在 {timeout}s 内消失")
    return StepResult("wait_gone", False, name)


def _act_scroll_to(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """滚动直到目标出现。"""
    name = step["target"]
    t = ctx.target(name)
    w, h = ctx.screen_size()
    direction = step.get("direction", "down")
    times = int(step.get("times", 6))
    for i in range(times):
        if t.exists(ctx.device, (w, h)):
            _log_step(ctx, f"滚动 {i} 次后找到 {name}")
            return StepResult("scroll_to", True, name)
        if direction == "down":
            ctx.device.swipe(w // 2, int(h * 0.72), w // 2, int(h * 0.32), 0.25)
        else:
            ctx.device.swipe(w // 2, int(h * 0.32), w // 2, int(h * 0.72), 0.25)
        time.sleep(0.35)
    _log_step(ctx, f"滚动 {times} 次仍未找到 {name}")
    return StepResult("scroll_to", False, name)


def _act_select(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """选择一个选项（场次 / 票档）。既支持命名控件，也支持字面文本。

    timeout > 0 时会先等元素出现再点——购票面板弹出后有动画，
    立刻点往往还点不到。
    """
    if step.get("target"):
        name = step["target"]
        target = ctx.target(name)
        label = name
    else:
        label = step.get("text", "")
        target = Target.from_dict("inline", {"text": label})

    timeout = float(step.get("timeout", 0))
    if timeout > 0:
        target.wait(ctx.device, timeout, ctx.screen_size())

    ok = target.click(ctx.device, ctx.screen_size())
    time.sleep(step.get("settle", 0.8))
    _log_step(ctx, f"选择 {label!r}: {'成功' if ok else '失败'}")
    if not ok:
        _safe_shot(ctx, f"select-fail-{label}")
    return StepResult("select", ok, label)


def _act_select_quantity(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """通过「+」按钮增加数量；数量已是目标值时跳过。"""
    count = int(step.get("count", ctx.recipe.concert.quantity))
    plus_name = step.get("plus_target", "qty_plus")
    if not ctx.has_target(plus_name):
        _log_step(ctx, f"未定义 {plus_name}，跳过数量设置")
        return StepResult("select_quantity", True, "skipped")
    plus = ctx.target(plus_name)
    current = int(step.get("current", 1))
    for _ in range(max(0, count - current)):
        if not plus.click(ctx.device, ctx.screen_size()):
            break
        time.sleep(0.35)
    _log_step(ctx, f"数量设置目标 {count} 张")
    return StepResult("select_quantity", True, str(count))


def _act_burst(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """核心动作：连点目标，直到出现「成功标志」控件或超出时间窗。

    mode:
      query —— 每次都重新查 UI 定位（稳，但每次 ~150-300ms）
      coord —— 直接用标定好的坐标盲点（快，零查询延迟），适合到点那一瞬间
      auto  —— 先用 coord 连点，同时低频检查跳转标志

    blockers: 列表。若这些文案出现（例如开售公告），说明弹层挡住了购买按钮，
              连点前先把弹层清掉再继续点。这是开售瞬间最常见的「点了没反应」原因。

    每次点击间隔尽量短（默认 180ms），一旦检测到页面跳转立刻停止，
    避免重复下单。
    """
    name = step["target"]
    t = ctx.target(name)
    until_name = step.get("until")
    until_target = ctx.target(until_name) if until_name and ctx.has_target(until_name) else None
    window = float(step.get("window", ctx.recipe.sale.burst_seconds))
    interval = float(step.get("interval_ms", ctx.recipe.sale.tap_interval_ms)) / 1000.0
    mode = step.get("mode", "auto")
    size = ctx.screen_size()

    blockers = [Target.from_dict("inline", {"text": b}) for b in step.get("blockers", [])]
    blocker_expand = step.get("blocker_expand", [90, 130, 60, 180])

    deadline = time.monotonic() + window
    clicks = 0
    hit = False
    last_check = 0.0
    check_every = float(step.get("check_every_ms", 400)) / 1000.0

    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_check >= check_every:
            last_check = now
            if until_target is not None and until_target.exists(ctx.device, size):
                hit = True
                break
            # 弹层挡路：先清弹层，清完继续点
            for blk in blockers:
                if blk.exists(ctx.device, size):
                    _log_step(ctx, f"检测到遮挡弹层，先清除")
                    _expand_dialog(ctx, blk, size, blocker_expand)
                    blk.click(ctx.device, size)
                    time.sleep(0.8)
                    break

        if mode == "query":
            ok = t.click(ctx.device, size)
        else:
            ok = t.click_fast(ctx.device, size)
            if not ok:
                ok = t.click(ctx.device, size)
        if ok:
            clicks += 1
        else:
            time.sleep(0.08)
            continue
        time.sleep(interval)

    if until_target is not None:
        hit = hit or until_target.exists(ctx.device, size)
    _log_step(ctx, f"burst {name}({mode}): 点击 {clicks} 次, 命中跳转={hit}")
    _safe_event(ctx, "burst", target=name, clicks=clicks, transitioned=hit, mode=mode)
    return StepResult("burst", hit or clicks > 0, f"{clicks} clicks")


def _act_stop(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    msg = step.get("message", "流程在此停止")
    _log_step(ctx, f"⛔ {msg}")
    _safe_shot(ctx, "stop")
    return StepResult("stop", True, msg)


def _act_dump(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    path = ctx.device.dump(step.get("prefix", "state"))
    _log_step(ctx, f"UI dump -> {path}")
    return StepResult("dump", True, str(path))


def _dump_text_len(ctx: FlowContext) -> int:
    """当前页所有文本的总字符数——用来判断弹层正文是否被「展开」。"""
    try:
        import re

        xml = ctx.device.u2.dump_hierarchy()
        return sum(len(t) for t in re.findall(r'text="([^"]*)"', xml))
    except Exception:  # noqa: BLE001
        return 0


def _expand_dialog(ctx: FlowContext, target: Target, size, offsets) -> bool:
    """点击弹层里的「展开」箭头。

    美团的开售公告正文默认截断，正文末尾有一个画出来的 ⌄ 箭头（没有无障碍节点，
    uiautomator 查不到）。它位于正文最后一行和按钮之间，横向居中。
    这里按「按钮上移若干像素」逐个试，点完对比文本长度——展开了就停。
    """
    m = target.find(ctx.device, size)
    if not m or not m.bounds:
        return False
    cx = m.center[0] if m.center else size[0] // 2
    top = m.bounds[1]
    before = _dump_text_len(ctx)
    for off in offsets:
        y = top - off
        if y <= 0:
            continue
        ctx.device.click(cx, y)
        time.sleep(0.7)
        after = _dump_text_len(ctx)
        if after > before + 20:
            _log_step(ctx, f"弹层正文已展开（偏移 {off}px）")
            return True
    return False


def _act_dismiss(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """按顺序处理开售公告 / 实名制须知 / 登录引导这类一次性弹层。

    每条规则形如：
        {"text": "我已阅读并同意", "expand": [90, 130, 60], "timeout": 12}
        {"back_when": "登录体验完整功能"}      # 出现该文案时按返回键收起半屏弹层
        {"text": "知道了", "trigger": "buy_button"}   # 先点触发按钮，逼弹层出来

    expand 表示点按钮前先尝试点开正文末尾的「展开」箭头（否则按钮是灰的）。
    trigger 表示弹层不一定会自己冒出来，先点一下触发它的按钮把它逼出来。
    """
    rules = step.get("rules", [])
    handled = []
    for rule in rules:
        text = rule.get("text")
        marker = rule.get("back_when")
        size = ctx.screen_size()
        deadline = time.monotonic() + float(rule.get("timeout", 8))

        if marker:
            m = Target.from_dict("inline", {"textContains": marker})
            while time.monotonic() < deadline:
                if m.exists(ctx.device, size):
                    ctx.device.press("back")
                    handled.append(f"back({marker})")
                    time.sleep(float(rule.get("settle", 1.5)))
                    break
                time.sleep(0.3)
            continue

        if not text:
            continue
        target = Target.from_dict("inline", {"text": text})

        # 弹层可能不主动出现，先点触发按钮把它逼出来
        trigger_name = rule.get("trigger")
        if trigger_name and ctx.has_target(trigger_name) and not target.exists(ctx.device, size):
            trig = ctx.target(trigger_name)
            _log_step(ctx, f"触发弹层: 点击 {trigger_name}")
            trig.click(ctx.device, size)
            time.sleep(float(rule.get("trigger_settle", 2.5)))

        clicked = False
        while time.monotonic() < deadline:
            if target.exists(ctx.device, size):
                offsets = rule.get("expand")
                if offsets:
                    _expand_dialog(ctx, target, size, offsets)
                if target.click(ctx.device, size):
                    time.sleep(float(rule.get("settle", 2.0)))
                    if not target.exists(ctx.device, size):
                        clicked = True
                        handled.append(text)
                        break
                time.sleep(0.4)
                continue
            time.sleep(0.3)
        if not clicked and not rule.get("optional"):
            _log_step(ctx, f"弹层未关闭: {text}")
            return StepResult("dismiss", False, text)
    _log_step(ctx, f"处理弹层: {', '.join(handled) if handled else '无'}")
    return StepResult("dismiss", True, ", ".join(handled))


def _act_require(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """前置条件断言。

    expect: present（默认，控件必须存在）| absent（控件必须不存在）
    例如登录态：定义 logged_out_marker = 文本「去登录」，
    然后 {"action":"require","target":"logged_out_marker","expect":"absent"}。
    """
    name = step["target"]
    expect = step.get("expect", "present")
    if not ctx.has_target(name):
        _log_step(ctx, f"require 跳过（未定义 {name}）")
        return StepResult("require", True, "skipped")
    t = ctx.target(name)
    exists = t.exists(ctx.device, ctx.screen_size())
    ok = exists if expect == "present" else not exists
    if ok:
        _log_step(ctx, f"require {name}({expect}): 满足")
    else:
        _log_step(ctx, f"require {name}({expect}): 不满足 -> {step.get('message', '请先在设备上完成登录')}")
        _safe_shot(ctx, f"require-{name}")
    return StepResult("require", ok, name)


def _act_search_submit(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """输入关键词并提交搜索（比 search 稳：先等输入框真正拿到焦点再输入）。"""
    keyword = step.get("text") or ctx.recipe.concert.search_keyword
    size = ctx.screen_size()
    box = Target.from_dict("inline", {"textContains": step.get("box_hint", "搜索明星")})
    if not box.exists(ctx.device, size):
        box = Target.from_dict("inline", {"textContains": step.get("fallback_hint", "搜索")})
    if not box.click(ctx.device, size):
        _log_step(ctx, "未找到搜索输入框")
        return StepResult("search_submit", False, keyword)
    time.sleep(1.2)
    try:
        ctx.device.u2.set_fastinput_ime(True)
    except Exception:  # noqa: BLE001
        pass
    typed = False
    for _ in range(3):
        try:
            ctx.device.type_text(keyword)
        except Exception as exc:  # noqa: BLE001
            _log_step(ctx, f"输入异常: {exc}")
        time.sleep(1.2)
        typed = Target.from_dict("inline", {"text": keyword}).exists(ctx.device, size)
        if typed:
            break
    ctx.device.press("enter")
    time.sleep(float(step.get("settle", 4.0)))
    _log_step(ctx, f"搜索提交: {keyword} (输入{'成功' if typed else '未确认'})")
    return StepResult("search_submit", typed, keyword)


def _act_back(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    times = int(step.get("times", 1))
    for _ in range(times):
        ctx.device.press("back")
        time.sleep(float(step.get("settle", 1.5)))
    _log_step(ctx, f"返回 {times} 次")
    return StepResult("back", True, str(times))


def _page_has(ctx: FlowContext, text: str) -> bool:
    """页面上是否出现某段文字。

    用选择器 API（~45ms）而不是整树 dump（~150ms）——热路径上每 100ms 都是钱。
    """
    try:
        return ctx.device.u2(textContains=text).count > 0
    except Exception:  # noqa: BLE001
        try:
            import re

            xml = ctx.device.u2.dump_hierarchy()
            return any(text in t for t in re.findall(r'text="([^"]*)"', xml))
        except Exception:  # noqa: BLE001
            return False


def _wait_stable(target: Target, device, max_wait: float = 0.6) -> None:
    """等目标的位置稳定下来再点。

    购票面板是底部弹层，弹出时有个上滑动画。动画没结束就按坐标点，
    会打在旧位置上——表现就是"点了没反应"，很容易被误判成"没票"。
    """
    deadline = time.monotonic() + max_wait
    last = None
    stable = 0
    while time.monotonic() < deadline:
        m = target.find(device)
        cur = m.bounds if m else None
        if cur is not None and cur == last:
            stable += 1
            if stable >= 2:
                return
        else:
            stable = 0
        last = cur
        time.sleep(0.1)


def _act_select_tier(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """按优先级逐个尝试票档：680 → 480 → 880。

    **抢票语义**：优先级是一个「可接受价格清单」，不是「只买第一档」。
      * 优先点最靠前的那一档（最快锁定最想要的价格）
      * 这一档点了没反应（售罄/不可售）→ 立刻换下一档重试确认流程
      * 每一档都要**快速决断**：点完只做一次轻量校验（选中才会出现「数量」行），
        校验通过立刻进入下单流程，不做任何多余等待
      * 总耗时受 total_budget 约束，避免在抢票窗口里耗光时间
    """
    priority = step.get("priority") or ctx.recipe.concert.tier_priority
    size = ctx.screen_size()
    wait = float(step.get("wait", 1.2))          # 只等首个候选出现
    settle = float(step.get("settle", 0.35))
    budget = float(step.get("total_budget", 5.0))

    # 从游标继续：上一轮已经试过并且失败的档不再重复浪费时间
    start = min(ctx.tier_cursor, len(priority) - 1) if priority else 0
    deadline = time.monotonic() + budget
    for idx in range(start, len(priority)):
        price = str(priority[idx]).strip()
        if not price:
            continue
        if time.monotonic() > deadline:
            _log_step(ctx, "票档: 已超出总时间预算，停止尝试")
            break
        candidates = [{"text": price}, {"textContains": price.lstrip("¥￥")}]
        target = Target.from_dict("inline", {"candidates": candidates})
        # 只有第一档给足等待；后面的档只有在"压根没有这个档"时才用到
        found = target.wait(ctx.device, wait if idx == start else 0.35, size)
        if found is None:
            _log_step(ctx, f"票档 {price}: 本场次没有该档，换下一档")
            continue

        # 等弹层动画结束，位置稳定了再点
        _wait_stable(target, ctx.device, float(step.get("stable_wait", 0.6)))

        target.click(ctx.device, size)
        time.sleep(settle)
        if _page_has(ctx, "数量"):
            ctx.tier_cursor = idx + 1
            _log_step(ctx, f"票档 {price}: 选中成功")
            return StepResult("select_tier", True, price)

        # 可能是动画没赶上（点空了），再点一次再判定
        time.sleep(0.15)
        target.click(ctx.device, size)
        time.sleep(settle)
        if _page_has(ctx, "数量"):
            ctx.tier_cursor = idx + 1
            _log_step(ctx, f"票档 {price}: 选中成功（补点一次）")
            return StepResult("select_tier", True, price)

        _log_step(ctx, f"票档 {price}: 无票/点不动，立即换下一档")
    _safe_shot(ctx, "select-tier-fail")
    return StepResult("select_tier", False, ",".join(str(p) for p in priority))


def _act_select_attendees(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """校验/补选实名观演人。

    订单页会显示「需选择 N 位；入场需携带对应证件」。N 必须等于票数，
    否则「立即支付」点不动。

    快路径（抢票时至关重要）：一次查询判断是否已满足，满足就直接走，
    整个校验 ~150ms。只有真的不满足才走慢路径去补选。
    """
    import re

    required = int(step.get("count", ctx.recipe.concert.attendees))
    size = ctx.screen_size()

    def pending() -> int:
        """还需选择的位数；-1 表示页面还没加载出来；0 表示已满足。"""
        try:
            xml = ctx.device.u2.dump_hierarchy()
        except Exception:  # noqa: BLE001
            return -1
        for t in re.findall(r'text="([^"]*)"', xml):
            if "需选择" in t:
                m = re.search(r"(\d+)", t)
                if m:
                    return int(m.group(1))
        return 0

    need = pending()
    if need == 0:
        _log_step(ctx, "实名观演人: 已满足，直接下单")
        return StepResult("select_attendees", True, "0")
    if need < 0:
        _log_step(ctx, "实名观演人: 页面未加载，跳过校验")
        return StepResult("select_attendees", True, "unknown")
    if need != required:
        _log_step(ctx, f"实名观演人: 需 {need} 位，但票数 {required} 位 —— 数量不匹配")
        _safe_shot(ctx, "attendee-mismatch")
        return StepResult("select_attendees", False, f"need={need} want={required}")

    # 慢路径：进「确认购票」页挑观演人
    _log_step(ctx, f"实名观演人: 还需 {need} 位，进入选择流程")
    entry = Target.from_dict("inline", {"text": step.get("entry", "需选择")})
    if entry.exists(ctx.device, size):
        entry.click(ctx.device, size)
        time.sleep(1.2)
    else:
        add = Target.from_dict("inline", {"text": "新增"})
        if add.exists(ctx.device, size):
            add.click(ctx.device, size)
            time.sleep(1.2)
        else:
            _log_step(ctx, "实名观演人: 找不到入口")
            return StepResult("select_attendees", False, "no-entry")

    _select_unchecked_boxes(ctx, size)

    confirm = Target.from_dict("inline", {"text": step.get("confirm", "确认选择")})
    if confirm.exists(ctx.device, size):
        confirm.click(ctx.device, size)
        time.sleep(1.8)

    need_after = pending()
    ok = need_after <= 0
    _log_step(ctx, f"实名观演人: 选择后仍需 {need_after} 位")
    _safe_shot(ctx, "attendee-result")
    return StepResult("select_attendees", ok, str(need_after))


def _select_unchecked_boxes(ctx: FlowContext, size) -> None:
    """在「确认购票」页把没勾上的观演人勾上。

    勾选圈没有 resource-id / text / checked 属性，只能靠：
      1. 用观演人姓名节点的 y 定位同一行的勾选圈（固定在最右侧 x）
      2. 截图像素判断圈里有没有红色（勾上的圈是红色实心）
    """
    import re

    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return

    try:
        xml = ctx.device.u2.dump_hierarchy()
    except Exception:  # noqa: BLE001
        return

    # 观演人行：左侧姓名（x < 400） + 右侧勾选圈（x ≈ 1150）
    rows = []
    for m in re.finditer(r"<node[^>]*>", xml):
        v = m.group(0)
        t = re.search(r'text="([^"]*)"', v)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', v)
        if not (t and b):
            continue
        x1, y1, x2, y2 = map(int, b.groups())
        text = t.group(1)
        # 身份证号那行最稳：格式 身份证 xxx
        if "身份证" in text and x1 < 400:
            rows.append(((y1 + y2) // 2, text))
    if not rows:
        return

    try:
        shot = ctx.device.u2.screenshot().convert("RGB")
    except Exception:  # noqa: BLE001
        return

    right_x = int(size[0] * 0.88)
    for cy, text in rows:
        box = (right_x - 60, cy - 45, right_x + 60, cy + 45)
        try:
            crop = shot.crop(box)
        except Exception:  # noqa: BLE001
            continue
        px = list(crop.getdata())
        if not px:
            continue
        red = sum(1 for r, g_, b in px if r > 140 and g_ < 90 and b < 90)
        if red / len(px) < 0.04:
            ctx.device.click(right_x, cy)
            time.sleep(0.6)
            _log_step(ctx, f"勾选观演人行 @y={cy}")


def _act_scroll_top(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """把页面滚回顶部。

    美团演出详情页是整页滚动的，返回页面后滚动位置会残留——底部「立即购票」
    的坐标会随滚动位置变化，坐标兜底就会点错东西。所以每次上膛前先归位。
    """
    size = ctx.screen_size()
    times = int(step.get("times", 12))
    for _ in range(times):
        ctx.device.swipe(
            size[0] // 2,
            int(size[1] * float(step.get("from_y", 0.30))),
            size[0] // 2,
            int(size[1] * float(step.get("to_y", 0.85))),
            0.22,
        )
        time.sleep(float(step.get("settle", 0.35)))
    _log_step(ctx, f"页面已回顶（{times} 次下滑）")
    return StepResult("scroll_top", True, str(times))


def _act_scroll_until(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """持续滑动直到目标出现（用于激活弹层按钮 / 露出购买入口）。"""
    name = step["target"]
    t = ctx.target(name)
    size = ctx.screen_size()
    times = int(step.get("times", 8))
    for i in range(times):
        if t.exists(ctx.device, size):
            _log_step(ctx, f"滑动 {i} 次后找到 {name}")
            return StepResult("scroll_until", True, name)
        ctx.device.swipe(
            size[0] // 2,
            int(size[1] * float(step.get("from_y", 0.68))),
            size[0] // 2,
            int(size[1] * float(step.get("to_y", 0.40))),
            0.28,
        )
        time.sleep(float(step.get("settle", 0.45)))
    ok = t.exists(ctx.device, size)
    _log_step(ctx, f"滑动 {times} 次后仍未找到 {name}")
    return StepResult("scroll_until", ok, name)


def _act_loop(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """重复执行一组步骤，直到满足条件或用完轮数/时间预算。

    典型用法（抢票换档重试）：
        {"action": "loop", "until": "order_ready", "max_rounds": 3, "budget": 10,
         "steps": [{"action": "select_tier"}, {"action": "tap", "target": "confirm_button"}]}
    select_tier 内部带游标，所以第 2 轮会自动尝试优先级里的下一档。
    """
    until_name = step.get("until")
    until_target = ctx.target(until_name) if until_name and ctx.has_target(until_name) else None
    max_rounds = int(step.get("max_rounds", 3))
    budget = float(step.get("budget", 10.0))
    inner = step.get("steps", [])
    deadline = time.monotonic() + budget

    for rnd in range(1, max_rounds + 1):
        if time.monotonic() > deadline:
            _log_step(ctx, f"loop: 超出时间预算，停止（第 {rnd} 轮）")
            break
        _log_step(ctx, f"loop: 第 {rnd}/{max_rounds} 轮")
        run_steps(ctx, inner, phase=None)
        if until_target is not None and until_target.exists(ctx.device, ctx.screen_size()):
            _log_step(ctx, f"loop: 条件 {until_name} 满足，结束")
            return StepResult("loop", True, f"round {rnd}")
        if time.monotonic() > deadline:
            break
    ok = until_target is not None and until_target.exists(ctx.device, ctx.screen_size())
    _log_step(ctx, f"loop: 结束，条件满足={ok}")
    return StepResult("loop", ok, "")


ACTIONS = {
    "launch": _act_launch,
    "deep_link": _act_deep_link,
    "search": _act_search,
    "search_submit": _act_search_submit,
    "tap": _act_tap,
    "tap_text": _act_tap_text,
    "wait": _act_wait,
    "wait_text": _act_wait_text,
    "wait_gone": _act_wait_gone,
    "scroll_to": _act_scroll_to,
    "scroll_until": _act_scroll_until,
    "scroll_top": _act_scroll_top,
    "back": _act_back,
    "select": _act_select,
    "select_tier": _act_select_tier,
    "select_attendees": _act_select_attendees,
    "select_quantity": _act_select_quantity,
    "loop": _act_loop,
    "burst": _act_burst,
    "dismiss": _act_dismiss,
    "require": _act_require,
    "stop": _act_stop,
    "dump": _act_dump,
}


def run_steps(ctx: FlowContext, steps: List[Dict[str, Any]], phase: Optional[str] = None) -> bool:
    """执行一组步骤。返回是否全部成功（optional 步骤失败不算失败）。"""
    all_ok = True
    for step in steps:
        action = step.get("action")
        if phase and step.get("phase") and step["phase"] != phase:
            continue
        # 需要真实下单/付款的步骤受配方开关控制
        if step.get("requires_submit") and not ctx.recipe.submit_order:
            _log_step(ctx, f"跳过 {action}（recipe.submit_order = false，不真实下单）")
            continue
        if step.get("requires_pay") and not ctx.recipe.auto_pay:
            _log_step(ctx, f"跳过 {action}（recipe.auto_pay = false，不真实付款）")
            continue
        handler = ACTIONS.get(action)
        if handler is None:
            _log_step(ctx, f"未知动作 {action!r}，跳过")
            all_ok = False
            continue
        try:
            result = handler(ctx, step)
        except FlowError as exc:
            if step.get("optional"):
                _log_step(ctx, f"动作 {action} 跳过（可选）: {exc}")
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            _log_step(ctx, f"动作 {action} 异常: {exc}")
            _safe_shot(ctx, f"error-{action}")
            result = StepResult(action, False, str(exc))
        ctx.results.append(result)
        if not result.ok and not step.get("optional"):
            all_ok = False
            if step.get("fatal", False):
                break
        if action == "stop":
            break
    return all_ok


def run_fire(ctx: FlowContext, plan: TriggerPlan) -> bool:
    """等待到点，然后执行 phase=fire 的步骤。"""
    fire_steps = [s for s in ctx.recipe.steps if s.get("phase") == "fire"]
    if not fire_steps:
        log.warning("配方中没有 phase=fire 的步骤，无事可做")
        return False
    deviation = fire_now(plan, log.info)
    ctx.device.event("fire", deviation_ms=round(deviation * 1000, 1))
    ctx.fired = True
    ok = run_steps(ctx, fire_steps, phase="fire")
    post_steps = [s for s in ctx.recipe.steps if s.get("phase") == "post"]
    if post_steps:
        run_steps(ctx, post_steps, phase="post")
    return ok


def build_context(
    device: Device,
    recipe: Recipe,
    targets: Optional[Dict[str, Target]] = None,
) -> FlowContext:
    return FlowContext(
        device=device,
        recipe=recipe,
        targets=targets if targets is not None else build_targets(recipe.selectors),
    )
