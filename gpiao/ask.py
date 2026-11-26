"""开跑前的参数问询。

每次抢票前必须确认三件事，缺一不可：
  1. 价格（可接受的价格优先级列表）
  2. 数量
  3. 实名观演人是否已经在美团 App 里预填写完毕

问完写回配方，保证「同样的策略」可以复现。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Recipe, save_recipe

SPLIT_RE = re.compile(r"[,，、\-\s]+")


def parse_prices(raw: str) -> List[str]:
    """把 '680-480-880' / '¥680，¥480' / '680,480,880' 解析成 ['¥680','¥480','¥880']。"""
    out: List[str] = []
    for part in SPLIT_RE.split(raw.strip()):
        part = part.strip().replace("￥", "").replace("¥", "").replace("元", "")
        if not part:
            continue
        if part.isdigit():
            out.append(f"¥{part}")
        else:
            out.append(part)
    return out


def ask(recipe: Recipe, interactive: bool = True) -> Tuple[Recipe, bool]:
    """问三项参数；返回 (更新后的配方, 是否继续)。"""
    if not interactive:
        return recipe, True

    print("\n" + "=" * 56)
    print(" 抢票参数确认")
    print("=" * 56)

    # ---- 1. 价格 ----
    default_prices = ", ".join(recipe.concert.tier_priority)
    while True:
        raw = input(f"1) 可接受的价格优先级（从高到低，逗号分隔） [{default_prices}]: ").strip()
        prices = parse_prices(raw) if raw else list(recipe.concert.tier_priority)
        if prices:
            break
        print("   至少给一个价格，例如 680-480-880")

    # ---- 2. 数量 ----
    default_qty = recipe.concert.quantity
    while True:
        raw = input(f"2) 购票数量 [{default_qty}]: ").strip()
        qty = int(raw) if raw.isdigit() else default_qty
        if 1 <= qty <= 6:
            break
        print("   数量要在 1~6 之间")

    # ---- 3. 实名观演人 ----
    while True:
        raw = input("3) 实名观演人已在美团 App 里预填写好了吗？(y/n) [y]: ").strip().lower()
        attendees_ok = raw in ("", "y", "yes", "是", "已填")
        if raw in ("n", "no", "否", "没填"):
            print("   ! 没有预填实名观演人，演唱会是实名制，无法下单。")
            print("     请先在美团 App 里「我的 → 观演人/实名信息」添加好再回来。")
            if input("   仍然继续？(y/n) [n]: ").strip().lower() not in ("y", "yes"):
                return recipe, False
            attendees_ok = False
        break

    if not attendees_ok:
        # 只有一个默认观演人时只能买 1 张
        qty = 1
        print(f"   → 按 1 位默认观演人处理，数量改为 1")

    recipe.concert.tier_priority = prices
    recipe.concert.quantity = qty
    recipe.concert.attendees = qty if attendees_ok else 1

    print("\n→ 本次策略：")
    print(f"   价格优先级 : {' > '.join(prices)}")
    print(f"   数量       : {qty} 张")
    print(f"   观演人     : {'已预填' if attendees_ok else '未预填(按默认1位)'}")
    return recipe, True


def save(recipe: Recipe, path: Path) -> None:
    save_recipe(recipe, path)
