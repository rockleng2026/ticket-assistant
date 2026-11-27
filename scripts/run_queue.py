"""抢票值守：把多个任务串行排好，按开售时间依次执行。

只有一台设备，所以任务必须**串行**：前一个跑完（触发 + 下单），
下一个再上膛等它自己的开售时刻。

用法：
    python scripts/queue.py                        # 按下面默认队列
    python scripts/queue.py --tasks a,b --start 12:30
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gpiao.config import CST  # noqa: E402
from gpiao.task import list_tasks, load_task  # noqa: E402

# 默认队列：按开售时间先后
DEFAULT_ORDER = [
    "panweibo-madlove-sz",         # 13:22 美团（先上）
    "wangliming-sz",               # 13:33 大麦
]


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(CST)


def wait_until(ts: dt.datetime, label: str) -> None:
    while True:
        delta = (ts - now()).total_seconds()
        if delta <= 0:
            print(f"[queue] {label} 到点了", flush=True)
            return
        print(f"[queue] {label} 倒计时 {delta / 60:.1f} 分钟", flush=True)
        time.sleep(min(60, max(1, delta)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=",".join(DEFAULT_ORDER), help="任务 id，逗号分隔")
    ap.add_argument("--start", help="第一个任务的上膛时间 HH:MM")
    ap.add_argument("--gap", type=int, default=150, help="两个任务之间的间隔秒数（留给你手动支付）")
    ap.add_argument("--no-submit", action="store_true", help="不生成订单（只演练）")
    args = ap.parse_args()

    ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    known = {t.id: t for t in list_tasks(ROOT)}
    missing = [i for i in ids if i not in known]
    if missing:
        print(f"任务不存在: {missing}\n现有任务: {list(known)}")
        return 1

    print("=" * 70)
    print(" 抢票值守队列")
    print("=" * 70)
    for i in ids:
        t = known[i]
        print(f"  {t.sale_text()}  {t.countdown_text():<18} {i}  ({t.name})")
    print("=" * 70)

    if args.start:
        h, m = args.start.split(":")
        wait_until(now().replace(hour=int(h), minute=int(m), second=0, microsecond=0), "第一个任务上膛")

    for idx, tid in enumerate(ids):
        t = known[tid]
        # 每个任务开跑前确认设备还在线（掉线就自愈重启 adb server）
        from gpiao.device import ensure_device

        devs = ensure_device()
        if not devs:
            print(f"[queue] ⚠ 设备不在线！{tid} 可能要错过，请手动介入", flush=True)
        else:
            print(f"[queue] 设备在线: {devs}", flush=True)

        cmd = [sys.executable, "-m", "gpiao", "task", "resume", tid, "--wait-now"]
        if not args.no_submit:
            cmd.append("--submit")
        print(f"\n[queue] ===== 第 {idx + 1}/{len(ids)} 个: {tid} =====", flush=True)
        print(f"[queue] $ {' '.join(cmd)}", flush=True)
        rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
        print(f"[queue] {tid} 结束，退出码 {rc}", flush=True)
        if idx + 1 < len(ids):
            # 留出时间让你手动支付上一个订单（订单锁 15 分钟）
            print(f"[queue] 等待 {args.gap} 秒（你可以手动支付上一个订单）", flush=True)
            time.sleep(args.gap)

    print("\n[queue] 全部任务执行完毕", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
