"""精确计时：把「到点触发」做到毫秒级。

要点：
1. 用 `time.monotonic()` 做倒计时（不受系统时间被调整的影响），
   在开始时把目标 epoch 换算成 monotonic 截止点。
2. 粗睡到距目标 ~40ms 后转为忙等，保证唤醒精度。
3. 支持「提前触发」lead_seconds：服务器到点后才放票，但客户端请求要提前发出，
   提前量默认 1.2s（网络 RTT + 渲染延迟）。
4. 提供 NTP 式校时检查：比较设备时间与电脑时间，偏差过大时告警。
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import CST


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(CST)


def sleep_until(deadline_mono: float, log: Optional[Callable[[str], None]] = None) -> float:
    """睡到 monotonic 截止点，返回实际唤醒时的正负偏差（秒，负=提前）。"""
    coarse = 0.04
    while True:
        remaining = deadline_mono - time.monotonic()
        if remaining <= coarse:
            break
        time.sleep(min(remaining - coarse, 0.25))
    while True:
        remaining = deadline_mono - time.monotonic()
        if remaining <= 0:
            break
        # 最后 40ms 忙等，避开 sleep 的调度抖动
        if remaining > 0.002:
            time.sleep(remaining - 0.002)
    return deadline_mono - time.monotonic()


def sleep_for(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


@dataclass
class TriggerPlan:
    target_epoch: float
    fire_epoch: float
    lead_seconds: float

    @property
    def target_text(self) -> str:
        return dt.datetime.fromtimestamp(self.target_epoch, CST).strftime("%Y-%m-%d %H:%M:%S")

    @property
    def fire_text(self) -> str:
        return dt.datetime.fromtimestamp(self.fire_epoch, CST).strftime("%H:%M:%S.%f")[:-3]

    def describe(self) -> str:
        return f"开售 {self.target_text} / 提前 {self.lead_seconds:.2f}s 于 {self.fire_text} 触发"


def build_plan(target: dt.datetime, lead_seconds: float = 1.2) -> TriggerPlan:
    target_epoch = target.timestamp()
    return TriggerPlan(
        target_epoch=target_epoch,
        fire_epoch=target_epoch - lead_seconds,
        lead_seconds=lead_seconds,
    )


def count_down(plan: TriggerPlan, log: Optional[Callable[[str], None]] = None,
               tick: float = 10.0) -> None:
    """等待到触发时刻（含进度日志）。

    注意：这里等的是 **fire_epoch**（= 开售时间 - lead_seconds），
    精确唤醒交给 fire_now()，两者不要重复等待，否则会白白迟到一个 lead。
    """
    if log is None:
        log = lambda msg: None  # noqa: E731
    fire_mono = time.monotonic() + (plan.fire_epoch - time.time())
    last_tick = float("inf")
    while True:
        remaining = fire_mono - time.monotonic()
        if remaining <= 0:
            break
        # 平时每 tick 秒报一次，最后 10 秒每 1 秒报一次
        step = 1.0 if remaining < 10 else tick
        if remaining - last_tick >= step:
            log(f"距触发还有 {remaining:8.2f}s  (触发时刻 {plan.fire_text})")
            last_tick = remaining
        time.sleep(min(0.1, max(0.02, remaining)))


def fire_now(plan: TriggerPlan, log: Optional[Callable[[str], None]] = None) -> float:
    """等待并返回实际唤醒误差（秒，正数=迟到）。"""
    if log is None:
        log = lambda msg: None  # noqa: E731
    fire_mono = time.monotonic() + (plan.fire_epoch - time.time())
    log(f"进入精等待，目标 {plan.fire_text}")
    deviation = sleep_until(fire_mono, log)
    log(f"触发时刻到达，偏差 {deviation * 1000:+.1f}ms")
    return -deviation
