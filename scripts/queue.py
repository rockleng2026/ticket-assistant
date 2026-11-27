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
    "panweibo-madlove-sz-damai",   # 13:22 大麦
    "wangliming-sz",               # 13:33 大麦
    "panweibo-madlove-sz",         # 美团（补刀）
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
        cmd = [sys.executable, "-m", "gpiao", "task", "resume", tid]
        if not args.no_submit:
            cmd.append("--submit")
        print(f"\n[queue] ===== 第 {idx + 1}/{len(ids)} 个: {tid} =====", flush=True)
        print(f"[queue] $ {' '.join(cmd)}", flush=True)
        rc = subprocess.run(cmd, cwd=str(ROOT)).returncode
        print(f"[queue] {tid} 结束，退出码 {rc}", flush=True)
        # 下一个任务上膛要 ~2 分钟，留出余量
        time.sleep(5)

    print("\n[queue] 全部任务执行完毕", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
