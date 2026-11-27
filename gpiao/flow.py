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
import re
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


CAPTCHA_MARKERS = ("验证码拦截", "操作过于频繁", "完成验证后可继续", "安全验证", "滑动验证")


def _act_tap(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    name = step["target"]
    t = ctx.target(name)
    size = ctx.screen_size()
    until_name = step.get("until")
    until_t = ctx.target(until_name) if until_name and ctx.has_target(until_name) else None
    retries = int(step.get("retry", 0))
    timeout = float(step.get("until_timeout", 2.5))
    settle = float(step.get("settle", 0.6))
    # 挡路弹层（大麦限流时弹「抢票人数太多啦，继续尝试别放弃」）
    blockers = [Target.from_dict("inline", {"text": b}) for b in step.get("blockers", [])]

    for attempt in range(retries + 1):
        m = t.find(ctx.device, size)
        if m is None:
            _log_step(ctx, f"点击 {name} 失败（候选全部未命中）")
            _safe_shot(ctx, f"fail-{name}")
            return StepResult("tap", False, name)
        t.click_match(m, ctx.device, size)
        time.sleep(settle)

        # 平台风控验证码：只能人工过，脚本必须停下而不是无脑重试
        try:
            import re as _re

            xml = ctx.device.u2.dump_hierarchy()
            page_texts = _re.findall(r'text="([^"]*)"', xml)
            hit = [w for w in CAPTCHA_MARKERS if any(w in t2 for t2 in page_texts)]
            if hit:
                _log_step(ctx, f"⚠ 触发平台风控验证码（{hit[0]}），需人工完成验证，脚本停止重试")
                _safe_shot(ctx, "captcha")
                return StepResult("tap", False, "captcha")
        except Exception:  # noqa: BLE001
            pass

        # 清挡路弹层（限流），清完再点一次目标
        blocked = False
        for blk in blockers:
            if blk.exists(ctx.device, size):
                _log_step(ctx, "遇到限流弹层，点「继续尝试」后重试")
                blk.click(ctx.device, size)
                # **有界退避**：无脑连打会被判定为异常行为触发验证码
                time.sleep(float(step.get("backoff", 1.5)))
                blocked = True
                break
        if blocked and attempt < retries:
            continue

        if until_t is not None:
            ok, took = until_t.poll(ctx.device, timeout, size)
            if ok:
                _log_step(ctx, f"点击 {name} 成功，等待 {until_name} 出现（{took:.2f}s）")
                time.sleep(0.1)
                return StepResult("tap", True, name)
            if blocked and attempt < retries:
                continue
            _log_step(ctx, f"点击 {name} 后 {until_name} 未出现（超时 {timeout}s）")
            return StepResult("tap", False, name)

        _log_step(ctx, f"点击 {name} 成功")
        return StepResult("tap", True, name)
    return StepResult("tap", False, name)



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


def _read_qty(ctx: FlowContext):
    """读出当前票数（「N张」文本节点）。"""
    import re as _re

    try:
        from lxml import etree

        xml = ctx.device.u2.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    for node in root.iter("node"):
        t = (node.get("text", "") or "").strip()
        m = _re.fullmatch(r"(\d+)\s*张", t)
        if m:
            return int(m.group(1))
    return None


def _act_select_quantity(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """把数量调到目标张数。

    **先读当前数量，不够才点「+」**：
    大麦的「预约」机制已经把票张数设好了（这次是 2 张），
    如果无条件点一次「+」就会变成 3 张，多买一张！
    """
    count = int(step.get("count", ctx.recipe.concert.quantity))
    plus_name = step.get("plus_target", "qty_plus")
    size = ctx.screen_size()

    current = _read_qty(ctx)
    if current is None:
        current = int(step.get("current", 1))
    _log_step(ctx, f"数量: 当前 {current} 张，目标 {count} 张")

    if current >= count:
        _log_step(ctx, f"数量已是 {current} 张，不用点「+」")
        return StepResult("select_quantity", True, str(current))
    if not ctx.has_target(plus_name):
        _log_step(ctx, f"未定义 {plus_name}，跳过数量设置")
        return StepResult("select_quantity", True, "skipped")
    plus = ctx.target(plus_name)

    for _ in range(count - current):
        m = plus.find(ctx.device, size)
        if m is None:
            break
        plus.click_match(m, ctx.device, size)
        time.sleep(float(step.get("settle", 0.12)))

    after = _read_qty(ctx)
    _log_step(ctx, f"数量设置后: {after} 张")
    return StepResult("select_quantity", True, str(after))



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
    failed = 0
    refresh_every = int(step.get("refresh_every", 0))
    last_check = 0.0
    check_every = float(step.get("check_every_ms", 150)) / 1000.0

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
                    _log_step(ctx, "检测到遮挡弹层，先清除")
                    _expand_dialog(ctx, blk, size, blocker_expand)
                    blk.click(ctx.device, size)
                    time.sleep(0.6)
                    break

        if mode == "query":
            ok = t.click(ctx.device, size)
        elif t.has_text_candidate():
            # 按钮有文本 -> 只按文本点，绝不盲点坐标
            ok = t.click_real(ctx.device, size)
        else:
            ok = t.click_fast(ctx.device, size)
            if not ok:
                ok = t.click(ctx.device, size)
        if ok:
            clicks += 1
            failed = 0
        else:
            failed += 1
            # 预售演出开售前详情页没有购买按钮，要下拉刷新才会出现
            if refresh_every and failed % refresh_every == 0:
                _log_step(ctx, "按钮还没出现，下拉刷新页面")
                ctx.device.swipe(
                    size[0] // 2, int(size[1] * 0.28), size[0] // 2, int(size[1] * 0.80), 0.25
                )
                time.sleep(1.2)
            time.sleep(0.05)
            continue
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))



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
    """输入关键词并提交搜索（更稳：先等输入框真正拿到焦点再打字）。

    搜索框的 hint 各家不同（美团「搜索明星…」，大麦「话剧音乐剧都在这哦」），
    所以找不到指定 hint 时会退化成"页面上的第一个 EditText"。
    """
    keyword = step.get("text") or ctx.recipe.concert.search_keyword
    size = ctx.screen_size()
    box = Target.from_dict("inline", {"textContains": step.get("box_hint", "搜索明星")})
    if not box.exists(ctx.device, size):
        box = Target.from_dict("inline", {"textContains": step.get("fallback_hint", "搜索")})
    if not box.exists(ctx.device, size):
        # 退化成任意 EditText（大麦的搜索框 hint 不是「搜索…」）
        try:
            if ctx.device.u2(className="android.widget.EditText").count:
                box = Target.from_dict("inline", {"className": "android.widget.EditText"})
        except Exception:  # noqa: BLE001
            pass
    if not box.click(ctx.device, size):
        _log_step(ctx, "未找到搜索输入框")
        return StepResult("search_submit", False, keyword)
    time.sleep(1.2)
    try:
        ctx.device.u2.set_fastinput_ime(True)
    except Exception:  # noqa: BLE001
        pass

    # 先清空：搜索框里常预填了热搜词（大麦首页是「黎明深圳演唱会」），
    # 不清会拼成「黎明深圳演唱会潘玮柏」，搜出来的结果就不对了
    cleared = False
    try:
        edits = ctx.device.u2(className="android.widget.EditText")
        if edits.count:
            edits[0].set_text("")
            cleared = True
    except Exception:  # noqa: BLE001
        pass
    if cleared:
        time.sleep(0.5)

    typed = False
    for attempt in range(3):
        try:
            ctx.device.type_text(keyword)
        except Exception as exc:  # noqa: BLE001
            _log_step(ctx, f"输入异常: {exc}")
        time.sleep(1.0)
        # 校验输入框内容是不是正好等于关键词（防止反复叠加）
        try:
            cur = ctx.device.u2(className="android.widget.EditText")
            got = (cur[0].info.get("text") or "") if cur.count else ""
        except Exception:  # noqa: BLE001
            got = ""
        if got.strip() == keyword:
            typed = True
            break
        if attempt < 2:
            try:
                ctx.device.u2(className="android.widget.EditText")[0].set_text("")
                time.sleep(0.4)
            except Exception:  # noqa: BLE001
                pass
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


def _wait_stable(target: Target, device, max_wait: float = 0.35):
    """等目标的位置稳定下来再点，并返回稳定后的节点（省一次查询）。

    购票面板是底部弹层，弹出时有个上滑动画。动画没结束就按坐标点，
    会打在旧位置上——表现就是"点了没反应"，很容易被误判成"没票"。
    """
    deadline = time.monotonic() + max_wait
    last = None
    stable = 0
    match = None
    while time.monotonic() < deadline:
        match = target.find(device)
        cur = match.bounds if match else None
        if cur is not None and cur == last:
            stable += 1
            if stable >= 2:
                return match
        else:
            stable = 0
        last = cur
        time.sleep(0.06)
    return match


def _re_price(price: str) -> str:
    """把 '¥680' / '680元' 归一成 '680'。"""
    import re as _re

    m = _re.search(r"\d+(?:\.\d+)?", str(price))
    return m.group(0) if m else ""


def _read_sku_price(ctx: FlowContext, size):
    """读出选票档页底部显示的价格（大麦票档是 canvas 画的，只能靠底部金额反查）。

    底部结构： [¥] [680]  —— 「¥」右边紧邻的数字就是价格。
    """
    import re as _re

    try:
        from lxml import etree

        xml = ctx.device.u2.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    w, h = size
    yuan = None
    for node in root.iter("node"):
        if (node.get("text", "") or "").strip() == "¥":
            b = node.get("bounds", "") or ""
            m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
            if m:
                x1, y1, x2, y2 = (int(g) for g in m.groups())
                if y1 > h * 0.85:
                    yuan = (x2, y1, y2)
                    break
    if yuan is None:
        return None

    for node in root.iter("node"):
        t = (node.get("text", "") or "").strip()
        if not _re.fullmatch(r"\d+(?:\.\d+)?", t):
            continue
        b = node.get("bounds", "") or ""
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if y1 < h * 0.85:
            continue
        if x1 - yuan[0] <= 40:
            return t
    return None


def _tier_row_coords(ctx: FlowContext, size):
    """从票档区的「可预约 / 缺货登记 / 缺货」标记反推各档的价格文字位置。

    大麦的票档价格是 canvas 画的（没有文本节点），但每个档位右边/下边
    会有「可预约」「缺货登记」这类**可读**的小标签。
    用它们的 y 定位行，再用「标签左边一点」推算出价格文字的 x。

    这样不同演出（1 列 7 档 / 2 列 2 档）都能自动适配，不用手标坐标。
    """
    import re as _re

    try:
        from lxml import etree

        xml = ctx.device.u2.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return []

    w, h = size
    marks = ("可预约", "缺货登记", "缺货", "已预约", "不可售")
    rows = []
    for node in root.iter("node"):
        t = (node.get("text", "") or "").strip()
        if t not in marks:
            continue
        b = node.get("bounds", "") or ""
        m = _re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        rows.append(((y1 + y2) // 2, x1))
    rows.sort()

    coords = []
    for cy, mx1 in rows:
        px = max(70, mx1 - 150)
        coords.append([round(px / w, 4), round(cy / h, 4)])
    return coords


def _act_select_tier_grid(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """大麦专用：网格扫描选票档。

    大麦票档是 canvas 画的（没有文本节点），但每个档位旁边有
    「可预约」「缺货登记」这类可读标签，可以据此推算出每一档的位置。

    做法：**从前往后扫一遍**，点一档就读底部金额；
    第一个「金额在优先级列表里」的档位就是它 —— 售罄的档点了没反应（金额不变），
    自然会被跳过，不会浪费后续档位的点击。

    priority: 可接受的档位，按优先级从高到低（通常是价格从低到高）。
    """
    priority = [str(p).strip() for p in (step.get("priority") or ctx.recipe.concert.tier_priority)]
    want = {_re_price(p) for p in priority if _re_price(p)}
    size = ctx.screen_size()
    grid = list(step.get("coords") or [])
    auto_grid = _tier_row_coords(ctx, size)
    if auto_grid:
        grid = auto_grid + grid
        _log_step(ctx, f"票档位置: 自动推算 {len(auto_grid)} 行")
    if not grid:
        _log_step(ctx, "票档: 推算不出档位位置")
        return StepResult("select_tier_grid", False, "no-grid")

    settle = float(step.get("grid_settle", 0.4))

    # 大麦的「预约」机制会在开抢后自动勾好票档 —— 先看是不是已经选中了，
    # 是的话一格都不用点（多点的风险是反而把档位改掉）
    already = _read_sku_price(ctx, size)
    if already and already in want:
        _log_step(ctx, f"票档 {already}: 已被自动勾选（预约机制），直接用")
        return StepResult("select_tier_grid", True, already)

    for pos in grid:
        gx = int(size[0] * float(pos[0]))
        gy = int(size[1] * float(pos[1]))
        ctx.device.click(gx, gy)
        time.sleep(settle)
        got = _read_sku_price(ctx, size)
        _log_step(ctx, f"票档网格试点 ({gx},{gy}) -> 底部金额 {got}")
        if got and got in want:
            _log_step(ctx, f"票档 {got}: 选中成功（网格扫描）")
            return StepResult("select_tier_grid", True, got)
    _safe_shot(ctx, "tier-grid-fail")
    _log_step(ctx, "票档: 扫描完所有档位都没命中优先级")
    return StepResult("select_tier_grid", False, ",".join(priority))


def _act_select_tier(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """按优先级逐个尝试票档：380 → 480 → ...

    **抢票语义**：优先级是一个「可接受价格清单」，不是「只买第一档」。
      * 优先点最靠前的那一档（最快锁定最想要的价格）
      * 这一档点了没反应（售罄/不可售）→ 立刻换下一档重试确认流程
      * 每一档都要**快速决断**：点完只做一次轻量校验（选中才会出现「数量」行）
      * 总耗时受 total_budget 约束，避免在抢票窗口里耗光时间

    速度：全程只查 2 次（定位 + 校验），点击复用已找到的节点，不重复查询。
    """
    priority = step.get("priority") or ctx.recipe.concert.tier_priority
    size = ctx.screen_size()
    wait = float(step.get("wait", 0.8))          # 只等首个候选出现
    settle = float(step.get("settle", 0.15))
    budget = float(step.get("total_budget", 5.0))
    # 网格坐标（大麦票档是 canvas 绘制的，只能按位置点）：
    #   [[x比例,y比例], ...] 按顺序尝试，点完从底部金额反查选中的是哪一档
    grid = list(step.get("coords") or [])
    # 优先用「可预约/缺货登记」标记自动推算出来的档位位置（适配不同排布）
    auto_grid = _tier_row_coords(ctx, size)
    if auto_grid:
        grid = auto_grid + grid
        _log_step(ctx, f"票档位置: 自动推算 {len(auto_grid)} 行")

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
        found = target.wait(ctx.device, wait if idx == start else 0.3, size)
        if found is None:
            # 票档可能是 canvas 绘制的（没有文本节点），改用网格坐标逐个试，
            # 点完从底部金额反查选中的到底是哪一档
            if grid:
                want = _re_price(price)
                for pos in grid:
                    gx = int(size[0] * float(pos[0]))
                    gy = int(size[1] * float(pos[1]))
                    ctx.device.click(gx, gy)
                    time.sleep(float(step.get("grid_settle", 0.45)))
                    got = _read_sku_price(ctx, size)
                    _log_step(ctx, f"票档网格试点 ({gx},{gy}) -> 底部金额 {got}")
                    if got and want and got.startswith(want):
                        ctx.tier_cursor = idx + 1
                        _log_step(ctx, f"票档 {price}: 选中成功（网格坐标定位）")
                        return StepResult("select_tier", True, price)
                _log_step(ctx, f"票档 {price}: 网格坐标里没找到")
                continue
            _log_step(ctx, f"票档 {price}: 本场次没有该档，换下一档")
            continue

        # 等弹层动画位置稳定，再点（否则容易打在旧位置）
        found = _wait_stable(target, ctx.device, float(step.get("stable_wait", 0.35))) or found

        target.click_match(found, ctx.device, size)
        time.sleep(settle)
        # 轻量校验：选中后才会出现「数量」行
        if _page_has(ctx, "数量"):
            ctx.tier_cursor = idx + 1
            _log_step(ctx, f"票档 {price}: 选中成功")
            return StepResult("select_tier", True, price)

        # 可能是动画没赶上（点空了），再点一次再判定
        time.sleep(0.1)
        found = target.find(ctx.device, size)
        if found is not None:
            target.click_match(found, ctx.device, size)
            time.sleep(settle)
            if _page_has(ctx, "数量"):
                ctx.tier_cursor = idx + 1
                _log_step(ctx, f"票档 {price}: 选中成功（补点一次）")
                return StepResult("select_tier", True, price)

        _log_step(ctx, f"票档 {price}: 无票/点不动，立即换下一档")
    _safe_shot(ctx, "select-tier-fail")
    return StepResult("select_tier", False, ",".join(str(p) for p in priority))



def _is_red(ctx: FlowContext, point, radius: int = 45) -> bool:
    """像素判断勾选圈是不是「已勾选」（红色实心）。"""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return False
    try:
        shot = ctx.device.u2.screenshot().convert("RGB")
        x, y = point
        box = (
            max(0, x - radius),
            max(0, y - radius),
            min(shot.size[0], x + radius),
            min(shot.size[1], y + radius),
        )
        px = list(shot.crop(box).getdata())
    except Exception:  # noqa: BLE001
        return False
    if not px:
        return False
    red = sum(1 for r, g_, b in px if r > 140 and g_ < 90 and b < 90)
    return red / len(px) >= 0.02


def _parse_attendees(ctx: FlowContext, size):
    """解析订单页上的实名观演人列表。

    返回 [{"name": 名字, "circle": (x, y), "checked": bool}, ...]
    观演人行的锚点是「身份证 xxx」文本（每个观演人必有一行），
    勾选圈在同一行右侧；名字是该行左侧的短文本。
    """
    try:
        from lxml import etree
    except Exception:  # noqa: BLE001
        return []

    try:
        xml = ctx.device.u2.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return []

    w, h = size
    rows = []
    for node in root.iter("node"):
        t = (node.get("text", "") or "").strip()
        if "身份证" not in t:
            continue
        b = node.get("bounds", "") or ""
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x1 > w * 0.4:
            continue
        rows.append({"y": (y1 + y2) // 2, "texts": []})

    if not rows:
        return []

    # 收集左侧可能的姓名文本
    for node in root.iter("node"):
        t = (node.get("text", "") or "").strip()
        if not t or "身份证" in t:
            continue
        b = node.get("bounds", "") or ""
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x1 > w * 0.35 or len(t) > 12:
            continue
        cy = (y1 + y2) // 2
        for row in rows:
            if abs(cy - row["y"]) <= 85:
                row["texts"].append(t)
                break

    # 收集勾选圈 / 复选框
    #   美团：ImageView（canvas 绘制，无 checked 属性）-> 靠像素颜色判断
    #   大麦：CheckBox（有 checked 属性）            -> 直接读 checked
    circles = []
    for node in root.iter("node"):
        cls = node.get("class", "") or ""
        is_box = cls.endswith("CheckBox")
        is_img = cls.endswith("ImageView") or cls.endswith("Image")
        if not (is_box or is_img):
            continue
        b = node.get("bounds", "") or ""
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", b)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if x1 < w * 0.70:
            continue
        if not (30 <= (x2 - x1) <= 180 and 30 <= (y2 - y1) <= 180):
            continue
        cy = (y1 + y2) // 2
        # 同一行（跟「身份证」那行对齐）里最贴近的那个
        best_row = None
        for row in rows:
            diff = abs(cy - row["y"])
            if diff <= 85 and (best_row is None or diff < best_row[0]):
                best_row = (diff, row["y"])
        if best_row is None:
            continue
        circles.append(
            {
                "x": (x1 + x2) // 2,
                "y": cy,
                "checked_attr": (node.get("checked", "false") == "true") if is_box else None,
            }
        )

    out = []
    for row in rows:
        circle = None
        best = None
        for c in circles:
            diff = abs(c["y"] - row["y"])
            if diff <= 85 and (best is None or diff < best[0]):
                best = (diff, c)
        if best is not None:
            circle = best[1]
        if circle is None:
            continue
        # 名字取该行最靠上的短文本（默认 tag「默认」不算名字）
        name = ""
        for t in row["texts"]:
            if t in ("默认", "已选", "常用"):
                continue
            name = t.lstrip("*").strip()
            break
        if circle["checked_attr"] is not None:
            checked = circle["checked_attr"]
        else:
            checked = _is_red(ctx, (circle["x"], circle["y"]))
        out.append(
            {
                "name": name,
                "circle": (circle["x"], circle["y"]),
                "checked": checked,
            }
        )
    return out


def _act_select_attendees(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """勾选实名观演人（按数量，不按名字）。

    票数 N 就必须勾满 N 个，否则「立即支付」点不动。
    App 的观演人列表由用户自己维护（买几票就只留那几个人），
    所以这里**不按名字/证件号匹配**，只管把空圈勾满。

    名字只用于日志报告，方便肉眼核对勾的是不是对的人。
    """
    required = int(step.get("count", ctx.recipe.concert.attendees))
    size = ctx.screen_size()

    def state():
        return _parse_attendees(ctx, size)

    people = state()
    if not people:
        _log_step(ctx, "实名观演人: 没解析到观演人列表（页面可能还没到订单页）")
        return StepResult("select_attendees", True, "no-people")

    def desc(pls):
        return "，".join(f"{p['name'] or '?'}{'✓' if p['checked'] else '✗'}" for p in pls)

    _log_step(ctx, f"实名观演人: {desc(people)}（需要 {required} 位）")

    people = state()
    checked = [p for p in people if p["checked"]]
    for p in people:
        if len(checked) >= required:
            break
        if p["checked"]:
            continue
        _log_step(ctx, f"勾选 {p['name'] or '?'} @({p['circle'][0]},{p['circle'][1]})")
        ctx.device.click(*p["circle"])
        time.sleep(0.45)
        checked.append(p)

    people = state()
    checked = [p for p in people if p["checked"]]
    ok = len(checked) >= required
    _log_step(ctx, f"实名观演人: 最终勾选 {len(checked)}/{required} 位 -> {desc(checked)}")
    _safe_shot(ctx, "attendee-result")
    if not ok:
        _log_step(ctx, "实名观演人: 数量不足 —— App 里存的人不够，检查观演人列表")
    return StepResult("select_attendees", ok, str(len(checked)))




def _act_wake(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """点亮屏幕并确保没有被锁屏挡住。

    锁屏（PIN/指纹）时 uiautomator 只能看到「输入密码」，所有查找都会失败，
    所以这一项必须在流程最前面做；解锁不了就直接失败，别等到开票才发现。
    """
    device = ctx.device
    device.wake_up()
    locked = False
    if hasattr(device, "keyguard_showing"):
        locked = device.keyguard_showing()
    if not locked:
        _log_step(ctx, "屏幕已点亮，无锁屏阻挡")
        return StepResult("wake", True, "awake")

    unlocked = False
    if hasattr(device, "unlock") and step.get("try_unlock", True):
        unlocked = device.unlock()
    if unlocked:
        _log_step(ctx, "已自动解除锁屏")
        return StepResult("wake", True, "unlocked")

    msg = step.get(
        "message",
        "手机被锁屏挡住了，脚本无法操作。请解锁手机；"
        "或执行 `python -m gpiao lockscreen --disable` 关闭系统锁屏（用完记得恢复）。",
    )
    _log_step(ctx, f"⚠ {msg}")
    _safe_shot(ctx, "keyguard-blocked")
    return StepResult("wake", False, "locked")


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


def _act_check_reservation(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """大麦专属：核对「去设置」里的抢票预约信息。

    大麦的预约信息（观演人 / 场次 / 票档 / 票张数）是**开抢瞬间自动套用**的，
    配错了开抢就会买错票或买不成，所以上膛前必须核对一次。

    做法：点「去设置」进 H5 页，把「票档：¥ 380」「票张数：1张」读出来比对。
    """
    size = ctx.screen_size()
    want_tier = step.get("tier")
    want_qty = step.get("quantity")

    entry = Target.from_dict("inline", {"text": "去设置"})
    if not entry.exists(ctx.device, size):
        _log_step(ctx, "预约核对: 页面没有「去设置」（可能还没开售/不可预约），跳过")
        return StepResult("check_reservation", True, "no-entry")
    entry.click(ctx.device, size)
    time.sleep(3.5)

    try:
        import re as _re

        xml = ctx.device.u2.dump_hierarchy()
        texts = _re.findall(r'text="([^"]*)"', xml)
    except Exception:  # noqa: BLE001
        texts = []

    tier_line = next((t for t in texts if t.startswith("票档")), "")
    qty_line = next((t for t in texts if t.startswith("票张数")), "")
    people = [t for t in texts if t.startswith("观演人")]
    session_line = next((t for t in texts if "周" in t and ":" in t), "")

    _log_step(ctx, f"预约核对: {session_line} | {tier_line} | {qty_line} | {'、'.join(people)}")

    ok = True
    if want_tier:
        if not tier_line:
            # 本场次没有票档预约（已在售的演出只有观演人预选）——跳过而不是报错
            _log_step(ctx, "预约核对: 本场次没有票档预约（已在售），跳过票档校验")
        else:
            norm_want = str(want_tier).replace("￥", "¥").replace(" ", "")
            norm_got = tier_line.replace(" ", "")
            if norm_want not in norm_got:
                _log_step(ctx, f"预约核对: 票档不符！期望 {want_tier}，实际「{tier_line}」")
                ok = False
    if want_qty:
        if not qty_line:
            _log_step(ctx, "预约核对: 本场次没有票张数预约，跳过票张数校验")
        else:
            m = _re.search(r"(\d+)\s*张", qty_line)
            got = int(m.group(1)) if m else None
            if got != int(want_qty):
                _log_step(ctx, f"预约核对: 票张数不符！期望 {want_qty}，实际「{qty_line}」")
                ok = False

    # 返回详情页
    ctx.device.press("back")
    time.sleep(2.5)
    _safe_shot(ctx, "reservation-check")
    if ok:
        _log_step(ctx, "预约核对: 通过")
    else:
        _log_step(ctx, "预约核对: 不通过 —— 请在「去设置」里改好观演人/票档/票张数")
    return StepResult("check_reservation", ok, tier_line + " " + qty_line)


def _act_verify_price(ctx: FlowContext, step: Dict[str, Any]) -> StepResult:
    """下单前核对实际价格。

    大麦的票档是 canvas 绘制的（没有无障碍节点），脚本可能点错档位。
    但**选中后价格会显示在底部的金额文本里**（￥680.00），
    所以在点「立即提交」之前必须核对一次，对不上就中止，绝不提交错价。
    """
    import re as _re

    want = step.get("price") or (ctx.recipe.concert.tier_priority or [None])[0]
    if not want:
        _log_step(ctx, "价格校验: 未配置期望价格，跳过")
        return StepResult("verify_price", True, "no-expect")

    try:
        xml = ctx.device.u2.dump_hierarchy()
        texts = _re.findall(r'text="([^"]*)"', xml)
    except Exception:  # noqa: BLE001
        texts = []

    # 拼出页面上所有金额文本，例如 ￥ + 680.00
    joined = " ".join(texts)
    prices = _re.findall(r"(\d+(?:\.\d+)?)", joined)
    norm_want = _re.sub(r"[^\d.]", "", str(want))

    found = [p for p in prices if p.startswith(norm_want)]
    _log_step(ctx, f"价格校验: 期望 {want}，页面金额 {prices[:8]}")
    if found:
        _log_step(ctx, "价格校验: 通过")
        return StepResult("verify_price", True, found[0])
    _log_step(ctx, f"价格校验: 不通过！期望 {want}，页面上没找到 —— 不会提交订单")
    _safe_shot(ctx, "price-mismatch")
    return StepResult("verify_price", False, norm_want)


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
    "wake": _act_wake,
    "back": _act_back,
    "select": _act_select,
    "select_tier": _act_select_tier,
    "select_tier_grid": _act_select_tier_grid,
    "select_attendees": _act_select_attendees,
    "select_quantity": _act_select_quantity,
    "loop": _act_loop,
    "burst": _act_burst,
    "check_reservation": _act_check_reservation,
    "verify_price": _act_verify_price,
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
        # 下单前必须通过价格校验，否则宁可不提交
        if step.get("requires_submit") and step.get("verify_price"):
            ok = run_steps(
                ctx,
                [{"action": "verify_price", "price": step.get("verify_price")}],
                phase=None,
            )
            if not ok:
                _log_step(ctx, f"跳过 {action}（价格校验未通过）")
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
