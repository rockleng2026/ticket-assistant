"""命令行入口。

常用命令：
  python -m gpiao doctor                      # 环境体检
  python -m gpiao init <name>                 # 生成配方骨架
  python -m gpiao inspect                     # 列出当前页可定位节点
  python -m gpiao record -r recipes/x.yaml -t buy_button   # 标定控件
  python -m gpiao arm   -r recipes/x.yaml    # 只做开售前准备
  python -m gpiao run   -r recipes/x.yaml    # 准备 + 等待 + 到点抢
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import clock
from .config import ConfigError, Recipe, load_recipe, save_recipe
from .device import Device, DeviceError, adb_connect, device_summary, list_devices
from .flow import FlowError, build_context, run_steps
from .logging_setup import RunRecorder, setup_logging
from .selectors import Target, build_targets, candidate_from_row, pick_candidate_interactive

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECIPES = ROOT / "recipes"
RUNS_DIR = ROOT / "runs"


def fix_console() -> None:
    """Windows 默认 GBK 代码页，中文/符号会炸；统一切到 UTF-8。"""
    if sys.platform == "win32":
        try:
            import os

            os.system("chcp 65001 >nul 2>&1")
        except Exception:  # noqa: BLE001
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def _local_ips() -> str:
    """列出本机 IPv4，方便判断手机该用哪个网段连。"""
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:  # noqa: BLE001
        return "?"


def cmd_connect(args) -> int:
    """真机无线 ADB 接入。

    1) 手机：设置 → 关于手机 → 连点「版本号」7 次进开发者选项
             → 打开「无线调试」→ 记下 IP:端口
    2) 首次要配对：  python -m gpiao connect --host <IP> --port <配对端口> --pair
    3) 正式连接：    python -m gpiao connect --host <IP> --port <调试端口>
    """
    host = args.host
    port = args.port
    if not host:
        print("请用 --host 指定手机 IP，例如 --host 192.168.1.23")
        return 1
    if not port:
        port = 5555
    target = f"{host}:{port}"

    if args.pair:
        code = args.code or input("请输入手机上的 6 位配对码: ").strip()
        if not code:
            print("没有配对码，已取消")
            return 1
        out = adb_run("pair", target, timeout=40)
        print(f"配对 {target}: {out}")
        if "successfully" not in out.lower() and "已成功" not in out:
            return 2
        print("\n配对成功。现在请用「无线调试」主界面显示的端口再执行一次：")
        print(f"  python -m gpiao connect --host {host} --port <调试端口>")
        return 0

    print(f"→ 连接 {target}")
    try:
        adb_connect(target)
    except DeviceError as exc:
        print(f"连接失败: {exc}")
        print("\n排查：")
        print("  · 手机和电脑必须在同一 WiFi")
        print("  · 手机「无线调试」已开启")
        print("  · 端口用对了（配对端口 ≠ 调试端口，调试端口会变）")
        print("  · 先执行 --pair 完成配对")
        return 2

    try:
        device = Device.connect(serial=target)
        w, h = device.screen().size
        print(f"✓ 已连接 {target}")
        print(f"  屏幕 {w}x{h}, 时钟偏差 {device.clock_skew():+.2f}s")
        print(f"  当前应用: {device.app_current()[0]}")
        print(f"  美团已安装: {'是' if device.package_installed('com.sankuai.meituan') else '否'}")
    except Exception as exc:  # noqa: BLE001
        print(f"连接成功但通信失败: {exc}")
        return 2
    return 0


def cmd_lockscreen(args) -> int:
    """锁屏开关 / 状态查询。

    ⚠ Android 限制：设备设了 PIN/图案/指纹（安全锁屏）时，
    `locksettings set-disabled` **不生效**（官方原话：只能在 Swipe 和 None 之间切）。
    这种情况下脚本靠下面三件事保证不熄屏：
        - screen_off_timeout = 最大值（屏幕永不熄灭 → keyguard 永不弹出）
        - stay_on_while_plugged_in = 1（充电常亮）
        - 开跑前自检发现被锁屏挡住就中止并提示手动解锁
    """
    device = _connect(None, args)
    locked = device.keyguard_showing()
    disabled = device.lock_screen_disabled()
    print(f"当前锁屏状态 : {'锁定中 ⚠' if locked else '未锁定 ✓'}")
    print(f"系统锁屏开关 : {'已关闭' if disabled else '启用中（PIN 等安全锁屏无法通过 ADB 关闭）'}")

    if args.enable or args.disable:
        out, state = device.set_lock_screen(enabled=args.enable)
        print(f"\n命令返回: {out}")
        if state == (args.enable is False):
            print("✓ 已生效")
        else:
            print("⚠ 未生效 —— 设备设了 PIN/图案/指纹时无法通过 ADB 关闭系统锁屏。")
            print("  脚本靠「屏幕永不熄灭 + 充电常亮 + 开跑前自检」来兜底。")
    return 0


def cmd_task(args) -> int:

    """任务管理：create / list / show / resume / run / rm。"""
    from . import task as taskmod

    root = ROOT
    action = args.task_action

    # ---------------- create ----------------
    if action == "create":
        platform = args.platform
        keyword = args.keyword
        name = args.name or keyword
        prices = []
        for chunk in (args.price or "").replace("，", ",").split(","):
            chunk = chunk.strip().replace("￥", "").replace("¥", "").replace("元", "")
            if chunk:
                prices.append(f"¥{chunk}" if chunk.isdigit() else chunk)
        if not prices:
            print("请用 --price 指定价格，例如 --price 680 或 --price 680,480")
            return 1

        sale_at = None
        if args.at:
            from .config import CST as _CST
            from .config import _parse_datetime

            sale_at = _parse_datetime(args.at, _CST)
        task_id = args.id or taskmod.slugify(f"{keyword}-{platform}")
        exists = (taskmod.tasks_dir(root) / f"{task_id}.json").exists()
        if exists and not args.force:
            print(f"任务 {task_id} 已存在，加 --force 覆盖")
            return 1

        from .platforms import build_recipe
        from .config import save_recipe

        task = taskmod.Task(
            id=task_id,
            name=name,
            platform=platform,
            status="created",
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            schedule=taskmod.Schedule(
                sale_at=sale_at,
                prepare_lead_minutes=int(args.prepare_lead),
                lead_seconds=float(args.lead),
            ),
            target={
                "keyword": keyword,
                "show_name": name,
                "city": args.city,
            },
            params={
                "tier_priority": prices,
                "quantity": int(args.qty),
                "attendees": int(args.attendees if args.attendees is not None else args.qty),
                "attendees_filled": bool(args.attendees_filled),
            },
            recipe=str(taskmod.recipe_path(root, task_id).relative_to(root)).replace("\\", "/"),
        )

        # 按平台模板生成配方，并把任务参数同步进去
        # （价格优先级要同时写进 loop 里的 select_tier.priority）
        recipe = build_recipe(platform, name, keyword)
        taskmod.apply_params_to_recipe(recipe, task)
        rpath = taskmod.recipe_path(root, task_id)
        save_recipe(recipe, rpath)

        task.log("created")
        p = taskmod.save_task(root, task)
        print(f"✓ 任务已创建: {task_id}")
        print(f"  任务文件 {p.relative_to(root)}")
        print(f"  配方文件 {task.recipe}")
        print(f"  开售时间 {task.sale_text()}  |  距开抢 {task.countdown_text()}")
        print(f"  价格 {' > '.join(prices)}  |  数量 {task.params['quantity']} 张"
              f"  |  观演人 {task.params['attendees']} 位"
              f"（{'已预填' if task.params['attendees_filled'] else '未预填'}）")
        print(f"\n开抢前 1 小时执行：  python -m gpiao task resume {task_id}")
        return 0

    # ---------------- list ----------------
    if action == "list":
        tasks = taskmod.list_tasks(root)
        if not tasks:
            print("还没有任务。用 `gpiao task create --help` 看怎么建。")
            return 0
        print(f"{'ID':<26} {'状态':<9} {'开售时间':<21} {'倒计时':<16} 名称")
        print("-" * 100)
        for t in tasks:
            print(f"{t.id:<26} {t.status:<9} {t.sale_text():<21} {t.countdown_text():<16} {t.name}")
        return 0

    # ---------------- show ----------------
    if action == "show":
        t = taskmod.load_task(root, args.task_id)
        print(t.describe())
        print(f"    距开抢 {t.countdown_text()}")
        if t.history:
            print("    历史:")
            for h in t.history[-10:]:
                print(f"      {h.get('ts')} {h.get('event')}")
        return 0

    # ---------------- resume / run ----------------
    if action in ("resume", "run"):
        t = taskmod.load_task(root, args.task_id)
        # 任务参数同步回配方
        taskmod.bind_recipe(root, t)
        recipe = load_recipe(Path(root) / t.recipe)

        print(f"▶ 任务 {t.id}")
        print(f"  {t.name}")
        print(f"  开售 {t.sale_text()}  |  距开抢 {t.countdown_text()}")
        print(f"  价格 {' > '.join(t.params.get('tier_priority', []))}"
              f"  |  数量 {t.params.get('quantity')} 张")
        if not t.params.get("attendees_filled"):
            print("  ⚠ 观演人未标记为已预填 —— 实名制演唱会会卡在支付前一步")

        # resume：开抢前 prepare_lead_minutes 分钟就该把设备摆好
        if action == "resume" and t.schedule.sale_at and not args.arm_only:
            import datetime as _dt

            from .config import CST as _CST

            now = _dt.datetime.now(_dt.timezone.utc).astimezone(_CST)
            prepare_at = t.schedule.sale_at - _dt.timedelta(minutes=t.schedule.prepare_lead_minutes)

            if now < prepare_at:
                wait_s = (prepare_at - now).total_seconds()
                print(f"\n  还没到准备时间（{prepare_at.strftime('%m-%d %H:%M')} 开始准备）")
                print(f"  现在 {now.strftime('%m-%d %H:%M')}，需要等 {wait_s / 3600:.1f} 小时")
                print("  → 到了再执行一次 `gpiao task resume <id>`；")
                print("    或加 --wait-now 让脚本在这里一直等到准备时间")
                if not args.wait_now:
                    return 0
                print("  等待中…")
                while _dt.datetime.now(_dt.timezone.utc).astimezone(_CST) < prepare_at:
                    time.sleep(30)


        recorder = taskmod.evidence_dir(root, t.id, action)
        setup_logging(args.verbose, RUNS_DIR / "gpiao.log")
        device = _connect(recipe, args)
        device.recorder = recorder
        ctx = build_context(device, recipe)
        t.status = "armed"
        t.log("armed")
        taskmod.save_task(root, t)

        # 开跑前自检：锁屏 / 登录 / 时钟
        print("\n▶ 开跑前自检")
        ok = True
        locked = device.keyguard_showing()
        if locked:
            print("  ✗ 手机处于锁屏状态 —— 脚本看不到界面，所有点击都会失效")
            print("    解锁手机，或执行：python -m gpiao lockscreen --disable")
            ok = False
        else:
            print("  ✓ 无锁屏阻挡")
        try:
            skew = device.clock_skew()
            if abs(skew) > 1.5:
                print(f"  ⚠ 设备时钟与电脑相差 {skew:+.2f}s（只影响 App 自己显示的倒计时，")
                print("     不影响脚本触发 —— 脚本用的是电脑的 NTP 时钟）")
            else:
                print(f"  ✓ 设备时钟偏差 {skew:+.2f}s（脚本触发用电脑 NTP 时钟）")
        except Exception:  # noqa: BLE001
            pass
        if not ok and not args.force:
            print("\n自检未通过，已中止。确认无误可加 --force 强行继续。")
            return 2

        print("\n▶ 准备阶段")

        run_steps(ctx, recipe.steps, phase="pre")
        device.screenshot("armed")
        t.status = "armed"
        t.log("armed_ok")
        taskmod.save_task(root, t)

        if args.arm_only:
            print("\n✅ 准备阶段完成（--arm-only：不进入等待与抢购）")
            print(f"证据目录: {recorder.dir}")
            for line in ctx.trace:
                print(f"  · {line}")
            return 0

        plan = clock.build_plan(recipe.sale.at, recipe.sale.lead_seconds)
        print(f"\n▶ {plan.describe()}")
        if args.skip_wait:
            print("  --skip-wait：立即触发")
            plan.fire_epoch = clock.now().timestamp()
        clock.count_down(plan, print)

        from .flow import run_fire

        print("\n▶ 触发阶段")
        run_fire(ctx, plan)
        device.screenshot("after-fire")
        t.status = "fired"
        t.log("fired")
        taskmod.save_task(root, t)

        print("\n✅ 流程结束")
        print(f"证据目录: {recorder.dir}")
        for line in ctx.trace:
            print(f"  · {line}")
        return 0

    # ---------------- rm ----------------
    if action == "rm":
        taskmod.delete_task(root, args.task_id)
        print(f"已删除任务 {args.task_id}")
        return 0

    return 1


def cmd_doctor(args) -> int:
    print("=" * 62)
    print(" gpiao 诊断")
    print("=" * 62)

    print("\n[1] Python / 依赖")
    import uiautomator2 as u2

    print(f"    uiautomator2 : {getattr(u2, '__version__', '?')}")
    try:
        import adbutils

        print(f"    adbutils     : {getattr(adbutils, '__version__', '?')}")
    except Exception:  # noqa: BLE001
        print("    adbutils     : 缺失")

    print("\n[2] ADB 设备")
    devices = list_devices()
    if not devices:
        print("    ✗ 没有在线设备")
        print("      - 模拟器：启动后执行  adb connect 127.0.0.1:5555")
        print("      - 真机  ：python -m gpiao connect --host <手机IP> --port <端口>")
    else:
        for info in device_summary():
            print(
                f"    ✓ {info['serial']:<24} {info['brand']} {info['model']} "
                f"Android {info['android']} (SDK {info['sdk']}, {info['abi']})"
            )
    if not any(":" in d for d in devices):
        print("    (提示: 没有无线设备。真机请用 `python -m gpiao connect --host <IP> --port <端口>`)")
    print(f"    本机 WiFi IP: {_local_ips()}")

    print("\n[3] 设备可读性")
    for serial in devices:
        try:
            d = Device.connect(serial=serial)
            w, h = d.screen().size
            skew = d.clock_skew()
            print(f"    ✓ {serial}: 屏幕 {w}x{h}, 时钟偏差 {skew:+.2f}s")
            print(f"      当前应用: {d.app_current()[0]}")
            for pkg in ("com.sankuai.meituan", "cn.damai"):
                print(f"      {pkg}: {'已安装' if d.package_installed(pkg) else '未安装'}")
        except Exception as exc:  # noqa: BLE001
            print(f"    ✗ {serial}: {exc}")

    print("\n[4] 配方")
    if DEFAULT_RECIPES.exists():
        for p in sorted(DEFAULT_RECIPES.glob("*.y*ml")):
            try:
                r = load_recipe(p)
                print(f"    ✓ {p.name}: {r.name} | 开售 {r.sale.describe()}")
            except Exception as exc:  # noqa: BLE001
                print(f"    ✗ {p.name}: {exc}")
    else:
        print("    （还没有 recipes/ 目录，执行 `python -m gpiao init <名称>` 创建）")
    print()
    return 0


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #

TEMPLATE_STEPS = [
    {"action": "launch", "phase": "pre", "settle": 3.0},
    {"action": "search", "phase": "pre", "text": "", "entry": "search_entry"},
    {"action": "wait_text", "phase": "pre", "text": "演出"},
    {"action": "tap_text", "phase": "pre", "text": ""},
    {"action": "wait", "phase": "pre", "target": "buy_button", "timeout": 30},
    {"action": "burst", "phase": "fire", "target": "buy_button", "until": "order_confirm",
     "window": 6.0, "interval_ms": 180},
    {"action": "stop", "phase": "post", "message": "已提交订单，请手动完成支付"},
]


def cmd_init(args) -> int:
    out = Path(args.output) if args.output else DEFAULT_RECIPES / f"{args.name}.yaml"
    if out.exists() and not args.force:
        print(f"已存在 {out}，加 --force 覆盖")
        return 1
    recipe = Recipe.from_dict(
        {
            "name": args.name,
            "platform": "meituan",
            "concert": {"name": "", "search_keyword": "", "session": None, "tier": None, "quantity": 1},
            "sale": {"at": "2026-01-01 12:00:00", "lead_seconds": 1.2, "burst_seconds": 6.0},
            "device": {},
            "selectors": {
                "search_entry": {"candidates": [{"text": "搜索"}, {"descContains": "搜索"}]},
                "buy_button": {"candidates": [{"text": "立即购买"}, {"textContains": "购买"}],
                               "aliases": []},
            },
            "steps": TEMPLATE_STEPS,
        }
    )
    save_recipe(recipe, out)
    print(f"已生成配方: {out}")
    return 0


# --------------------------------------------------------------------------- #
# 设备连接
# --------------------------------------------------------------------------- #


def _connect(recipe: Optional[Recipe], args) -> Device:
    serial = getattr(args, "serial", None)
    connect_to = getattr(args, "connect", None)
    if recipe is not None:
        serial = serial or recipe.device.serial
        connect_to = connect_to or recipe.device.connect
    if connect_to:
        print(f"→ 连接 {connect_to}")
        adb_connect(connect_to)
        serial = connect_to
    device = Device.connect(serial=serial)
    print(f"✓ 设备已连接: {device.serial}  屏幕 {device.screen().size}")
    if recipe is None or recipe.device.auto_prepare:
        device.prepare()
    return device


# --------------------------------------------------------------------------- #
# inspect / record
# --------------------------------------------------------------------------- #


def cmd_inspect(args) -> int:
    device = _connect(None, args)
    device.prepare()
    device.wake_up()
    rows = pick_candidate_interactive(device)
    if not rows:
        print("当前页面没有可定位节点（可能是空白页或 WebView）")
    print("\n提示：这些节点来自 uiautomator dump；若目标在 WebView 内，dump 可能为空。")
    return 0


def cmd_record(args) -> int:
    recipe = load_recipe(Path(args.recipe))
    device = _connect(recipe, args)
    device.prepare()
    device.wake_up()

    rows = pick_candidate_interactive(device)
    if not rows:
        print("没有可标定的节点")
        return 1

    print("\n输入要标定的节点编号（例如 12），或输入别名（逗号分隔，如 12,立即购买,马上抢）：")
    raw = input("> ").strip()
    if not raw:
        print("已取消")
        return 1

    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    aliases: List[str] = []
    if parts and parts[0].isdigit():
        idx = int(parts[0])
        if idx < 0 or idx >= len(rows):
            print(f"编号超出范围 (0-{len(rows) - 1})")
            return 1
        aliases = parts[1:]
    else:
        print("只接受编号开头的输入")
        return 1

    row = rows[idx]
    spec = candidate_from_row(row, device.screen().size, aliases)
    recipe.selectors[args.target] = spec
    save_recipe(recipe, Path(args.recipe))
    print(f"\n✓ 已标定 {args.target} -> {json.dumps(spec, ensure_ascii=False)}")
    print(f"  写入 {args.recipe}")
    return 0


# --------------------------------------------------------------------------- #
# arm / run
# --------------------------------------------------------------------------- #


def cmd_arm(args) -> int:
    recipe = load_recipe(Path(args.recipe))
    if not args.yes:
        from .ask import ask, save as save_ask

        recipe, cont = ask(recipe, interactive=True)
        if not cont:
            print("已取消")
            return 1
        save_ask(recipe, Path(args.recipe))
    recorder = RunRecorder(RUNS_DIR, tag="arm")
    setup_logging(args.verbose, RUNS_DIR / "gpiao.log")
    device = _connect(recipe, args)
    device.recorder = recorder
    ctx = build_context(device, recipe)
    device.event("arm_start", recipe=recipe.name)
    ok = run_steps(ctx, recipe.steps, phase="pre")
    device.screenshot("arm-end")
    print("\n准备阶段完成。")
    print(f"证据目录: {recorder.dir}")
    if not ok:
        print("⚠ 有步骤失败，请检查上面的日志与截图")
    return 0 if ok else 2


def cmd_run(args) -> int:
    recipe = load_recipe(Path(args.recipe))
    if recipe.sale.at is None:
        print("配方没有设置 sale.at，无法定时触发")
        return 1

    # 开跑前确认 价格 / 数量 / 观演人
    if not args.yes:
        from .ask import ask, save as save_ask

        recipe, cont = ask(recipe, interactive=True)
        if not cont:
            print("已取消")
            return 1
        save_ask(recipe, Path(args.recipe))

    recorder = RunRecorder(RUNS_DIR, tag="run")
    setup_logging(args.verbose, RUNS_DIR / "gpiao.log")
    device = _connect(recipe, args)
    device.recorder = recorder
    ctx = build_context(device, recipe)
    device.event("run_start", recipe=recipe.name, sale_at=recipe.sale.describe())

    print("\n▶ 准备阶段")
    run_steps(ctx, recipe.steps, phase="pre")
    device.screenshot("armed")

    plan = clock.build_plan(recipe.sale.at, recipe.sale.lead_seconds)
    print(f"\n▶ {plan.describe()}")
    print(f"  当前时间 {clock.now().strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")

    if args.skip_wait:
        print("  --skip-wait：立即触发")
        plan.fire_epoch = clock.now().timestamp()

    # count_down 一直等到 fire_epoch（含提前量），fire_now 再做精唤醒，
    # 这里绝不能再等一次，否则会白白迟到一个 lead_seconds。
    clock.count_down(plan, print)

    from .flow import run_fire

    print("\n▶ 触发阶段")
    run_fire(ctx, plan)
    device.screenshot("after-fire")

    print("\n✅ 流程结束")
    print(f"证据目录: {recorder.dir}")
    for line in ctx.trace:
        print(f"  · {line}")
    return 0


def cmd_fire(args) -> int:
    """设备已停在购买页，直接到点开抢。"""
    recipe = load_recipe(Path(args.recipe))
    recorder = RunRecorder(RUNS_DIR, tag="fire")
    setup_logging(args.verbose, RUNS_DIR / "gpiao.log")
    device = _connect(recipe, args)
    device.recorder = recorder
    ctx = build_context(device, recipe)
    plan = clock.build_plan(recipe.sale.at, recipe.sale.lead_seconds) if recipe.sale.at else None
    from .flow import run_fire

    if plan is None:
        plan = clock.build_plan(clock.now(), 0)
    clock.count_down(plan, print)
    run_fire(ctx, plan)
    device.screenshot("after-fire")
    print(f"证据目录: {recorder.dir}")
    return 0


def cmd_dump(args) -> int:
    device = _connect(None, args)
    xml = device.u2.dump_hierarchy()
    out = Path(args.output) if args.output else (RUNS_DIR / "hierarchy.xml")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml, encoding="utf-8")
    print(f"已写入 {out}")
    return 0


def cmd_shot(args) -> int:
    device = _connect(None, args)
    out = Path(args.output) if args.output else (RUNS_DIR / "shot.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    device.u2.screenshot().save(out)
    print(f"已保存 {out}")
    return 0


def cmd_tap(args) -> int:
    """一次性点击：按文本 / 无障碍描述 / 坐标。调试与初始化时很好用。"""
    device = _connect(None, args)
    if args.text:
        target = Target.from_dict("inline", {"text": args.text})
        ok = target.click(device, device.screen().size)
        print(f"点击文本 {args.text!r}: {'成功' if ok else '未找到'}")
        return 0 if ok else 2
    if args.contains:
        target = Target.from_dict("inline", {"textContains": args.contains})
        ok = target.click(device, device.screen().size)
        print(f"点击文本包含 {args.contains!r}: {'成功' if ok else '未找到'}")
        return 0 if ok else 2
    if args.desc:
        target = Target.from_dict("inline", {"descContains": args.desc})
        ok = target.click(device, device.screen().size)
        print(f"点击无障碍描述包含 {args.desc!r}: {'成功' if ok else '未找到'}")
        return 0 if ok else 2
    if args.x is not None and args.y is not None:
        device.click(args.x, args.y)
        print(f"点击坐标 ({args.x}, {args.y})")
        return 0
    print("请指定 --text / --contains / --desc / --x --y")
    return 1


def cmd_swipe(args) -> int:
    device = _connect(None, args)
    w, h = device.screen().size
    device.swipe(args.x1, args.y1, args.x2, args.y2, args.duration)
    print(f"滑动 ({args.x1},{args.y1}) -> ({args.x2},{args.y2})")
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpiao", description="演出票自动抢购助手")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("doctor", help="环境体检")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("connect", help="真机无线 ADB 连接/配对")
    sp.add_argument("--host", required=True, help="手机 IP")
    sp.add_argument("--port", default="5555", help="端口（无线调试主界面显示的端口）")
    sp.add_argument("--pair", action="store_true", help="执行配对（首次连接需要）")
    sp.add_argument("--code", help="6 位配对码，省略则交互输入")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("lockscreen", help="锁屏状态查询 / 开关（无人值守跑必须解锁）")
    sp.add_argument("--disable", action="store_true", help="关闭系统锁屏（抓完票记得恢复）")
    sp.add_argument("--enable", action="store_true", help="恢复系统锁屏")
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_lockscreen)

    # ---------------- task ----------------
    sp = sub.add_parser("task", help="抢票任务：create / list / show / resume / run / rm")

    tsp = sp.add_subparsers(dest="task_action", required=True)

    c = tsp.add_parser("create", help="创建抢票任务（现在建，开抢前再来跑）")
    c.add_argument("--id", help="任务 id（默认按关键字生成）")
    c.add_argument("--platform", default="meituan", choices=["meituan", "damai"])
    c.add_argument("--name", help="演出名称")
    c.add_argument("--keyword", required=True, help="App 内搜索关键字")
    c.add_argument("--city", default="深圳")
    c.add_argument("--price", required=True, help="价格优先级，如 680 或 680,480,880")
    c.add_argument("--qty", type=int, default=1, help="购票数量")
    c.add_argument("--attendees", type=int, default=None, help="实名观演人位数")
    c.add_argument(
        "--attendees-filled",
        action="store_true",
        help="确认实名观演人已在 App 里预填写完毕",
    )
    c.add_argument("--at", help="开售时间，如 2026-11-27 13:22:00")
    c.add_argument("--prepare-lead", type=int, default=60, help="开抢前多少分钟开始准备")
    c.add_argument("--lead", type=float, default=0.5, help="到点提前量（秒）")
    c.add_argument("--force", action="store_true")
    c.set_defaults(func=cmd_task)

    c = tsp.add_parser("list", help="列出所有任务")
    c.set_defaults(func=cmd_task)

    c = tsp.add_parser("show", help="查看任务详情")
    c.add_argument("task_id")
    c.set_defaults(func=cmd_task)

    c = tsp.add_parser("resume", help="继续任务：准备 -> 等待 -> 抢")
    c.add_argument("task_id")
    c.add_argument("--serial")
    c.add_argument("--connect")
    c.add_argument("--skip-wait", action="store_true", help="不等待，立即触发")
    c.add_argument("--wait-now", action="store_true", help="还没到准备时间就原地等待")
    c.add_argument("--arm-only", action="store_true", help="只做准备阶段，不等待不抢购")
    c.add_argument("--force", action="store_true", help="自检不通过也强行继续")
    c.set_defaults(func=cmd_task)

    c = tsp.add_parser("run", help="立刻执行任务（不等准备时间）")
    c.add_argument("task_id")
    c.add_argument("--serial")
    c.add_argument("--connect")
    c.add_argument("--skip-wait", action="store_true")
    c.set_defaults(func=cmd_task)

    c = tsp.add_parser("rm", help="删除任务")
    c.add_argument("task_id")
    c.set_defaults(func=cmd_task)

    sp = sub.add_parser("init", help="生成配方骨架")
    sp.add_argument("name", help="配方名（也用作文件名）")
    sp.add_argument("-o", "--output", help="输出路径")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("inspect", help="列出当前页可定位节点")
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("record", help="标定控件并写入配方")
    sp.add_argument("-r", "--recipe", required=True, help="配方文件")
    sp.add_argument("-t", "--target", required=True, help="控件名")
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("arm", help="只执行开售前准备")
    sp.add_argument("-r", "--recipe", required=True)
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.add_argument("--yes", "-y", action="store_true", help="跳过开跑前的参数问询")
    sp.set_defaults(func=cmd_arm)

    sp = sub.add_parser("run", help="准备 + 等待 + 到点抢")
    sp.add_argument("-r", "--recipe", required=True)
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.add_argument("--skip-wait", action="store_true", help="不等待，立即触发（演练用）")
    sp.add_argument("--yes", "-y", action="store_true", help="跳过开跑前的参数问询")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("fire", help="直接执行到点抢购（设备已就位）")
    sp.add_argument("-r", "--recipe", required=True)
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_fire)

    sp = sub.add_parser("dump", help="导出当前 UI 层级 XML")
    sp.add_argument("-o", "--output")
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("shot", help="截取设备屏幕")
    sp.add_argument("-o", "--output")
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_shot)

    sp = sub.add_parser("tap", help="按文本或坐标点击一次")
    sp.add_argument("--text")
    sp.add_argument("--contains")
    sp.add_argument("--desc", help="按 content-desc 匹配")
    sp.add_argument("--x", type=int)
    sp.add_argument("--y", type=int)
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_tap)

    sp = sub.add_parser("swipe", help="滑动一次")
    sp.add_argument("--x1", type=int, required=True)
    sp.add_argument("--y1", type=int, required=True)
    sp.add_argument("--x2", type=int, required=True)
    sp.add_argument("--y2", type=int, required=True)
    sp.add_argument("--duration", type=float, default=0.3)
    sp.add_argument("--serial")
    sp.add_argument("--connect")
    sp.set_defaults(func=cmd_swipe)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    fix_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "doctor":
        setup_logging(args.verbose, RUNS_DIR / "gpiao.log")
    try:
        return args.func(args)
    except (ConfigError, DeviceError, FlowError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
