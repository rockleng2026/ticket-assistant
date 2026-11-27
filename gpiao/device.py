"""设备层：统一模拟器与真机的连接、准备与常用操作。

支持两类设备：
  * 模拟器（雷电 / MuMu / 官方 AVD）：ADB 走 127.0.0.1:5555 这类本地端口
  * 真机：ADB 无线调试（adb pair + adb connect <手机IP>:5555）

两种设备走完全相同的上层 API，切换只需改配方里的 device.connect。
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import uiautomator2 as u2

from .logging_setup import RunRecorder

DEFAULT_CONNECT_CANDIDATES = [
    "127.0.0.1:5555",  # 雷电 / MuMu / 通用
    "127.0.0.1:5557",
    "127.0.0.1:62001",  # 夜神
    "emulator-5554",  # 官方 AVD
]


class DeviceError(Exception):
    """设备不可用。"""


def adb_run(*args: str, timeout: int = 30) -> str:
    """执行 adb 命令并返回 stdout。"""
    try:
        proc = subprocess.run(
            ["adb", *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise DeviceError("找不到 adb 可执行文件，请确认 ANDROID_HOME/platform-tools 在 PATH 中") from exc
    out = proc.stdout.decode("utf-8", "ignore").strip()
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "ignore").strip()
        raise DeviceError(f"adb {' '.join(args)} 失败: {err or out}")
    return out


def list_devices() -> List[str]:
    out = adb_run("devices")
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def adb_connect(target: str) -> None:
    """adb connect <host:port>，失败时抛出 DeviceError。"""
    out = adb_run("connect", target)
    if "connected to" not in out and "already connected" not in out:
        raise DeviceError(f"连接 {target} 失败: {out}")


def ensure_device() -> List[str]:
    """确保有设备在线，返回在线设备列表。

    ADB 偶尔会「掉线」（USB 闲置后、或 adb server 状态异常）。
    抢票这种一次性机会，掉线等于直接输，所以这里做一次自愈：
    没有任何设备时重启 adb server 再试。
    """
    devices = list_devices()
    if devices:
        return devices
    log = logging.getLogger("gpiao.device")
    log.warning("没有检测到设备，尝试重启 adb server 自愈")
    try:
        subprocess.run(["adb", "kill-server"], capture_output=True, timeout=20, check=False)
        time.sleep(2)
        subprocess.run(["adb", "start-server"], capture_output=True, timeout=20, check=False)
        time.sleep(3)
    except Exception:  # noqa: BLE001
        pass
    devices = list_devices()
    if devices:
        log.warning("adb server 重启后设备恢复: %s", devices)
    return devices


@dataclass
class ScreenInfo:
    width: int
    height: int
    density: int = 0

    @property
    def size(self) -> Tuple[int, int]:
        return self.width, self.height


def shell_text(d, cmd: str) -> str:
    """uiautomator2 3.x 的 shell() 返回 ShellResponse，统一取 .output。"""
    resp = d.shell(cmd)
    out = getattr(resp, "output", None)
    if out is None:
        out = str(resp)
    return (out or "").strip()


class Device:
    """uiautomator2 的薄封装，附加本项目需要的运维能力。"""

    def __init__(self, d, recorder: Optional[RunRecorder] = None):
        self._d = d
        self.recorder = recorder
        self._fast_tap_is_adb = True
        self._tap_bench: Dict[str, float] = {}

    # ---------------- 连接 ----------------

    @classmethod
    def connect(
        cls,
        serial: Optional[str] = None,
        connect_to: Optional[str] = None,
        recorder: Optional[RunRecorder] = None,
    ) -> "Device":
        target = serial
        if connect_to:
            adb_connect(connect_to)
            target = connect_to
        if not target:
            devices = ensure_device()
            if not devices:
                raise DeviceError(
                    "没有检测到任何 ADB 设备。请先启动模拟器，或用 `gpiao doctor` 查看诊断信息。"
                )
            # 真机（网络设备 serial 带 ':'）优先于本地模拟器
            net = [d for d in devices if ":" in d]
            target = net[0] if net else devices[0]
        try:
            d = u2.connect(target)
            d.info  # 触发一次真实通信，暴露连接问题
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"连接设备 {target} 失败: {exc}") from exc
        return cls(d, recorder=recorder)

    @property
    def serial(self) -> str:
        return getattr(self._d, "serial", "unknown")

    @property
    def u2(self):
        """裸 uiautomator2 设备对象（高级用法）。"""
        return self._d

    def __call__(self, **kwargs):
        """代理到 uiautomator2 的选择器构造：d(text='购买')。"""
        return self._d(**kwargs)

    def xpath(self, expr: str):
        return self._d.xpath(expr)

    # ---------------- 基础信息 ----------------

    def screen(self) -> ScreenInfo:
        w, h = self._d.window_size()
        density = 0
        try:
            raw = shell_text(self._d, "wm density")
            density = int(raw.split(":")[-1].strip())
        except Exception:  # noqa: BLE001
            pass
        return ScreenInfo(width=w, height=h, density=density)

    def device_epoch(self) -> float:
        """设备自身的时间戳，用于和电脑对时。"""
        return float(shell_text(self._d, "date +%s"))

    def clock_skew(self) -> float:
        """设备时间 - 电脑时间（秒）。正数表示设备慢。"""
        return self.device_epoch() - time.time()

    def package_installed(self, package: str) -> bool:
        out = shell_text(self._d, f"pm list packages {package}")
        return package in out

    # ---------------- 环境准备 ----------------

    def prepare(self) -> None:
        """关闭动画、保持常亮、清理后台——显著提升点击与页面切换速度。"""
        cmds = [
            "settings put global window_animation_scale 0",
            "settings put global transition_animation_scale 0",
            "settings put global animator_duration_scale 0",
            "svc power stayon true",
            "settings put system screen_off_timeout 2147483647",
        ]
        for cmd in cmds:
            try:
                self._d.shell(cmd)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._d.set_fastinput_ime(True)
        except Exception:  # noqa: BLE001
            pass

    def wake_up(self) -> None:
        """点亮屏幕并尝试拉起应用。"""
        try:
            if not self._d.info.get("screenOn"):
                self._d.screen_on()
                time.sleep(0.6)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._d.press("home")
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 锁屏 ----------------

    def keyguard_showing(self) -> bool:
        """当前是否被锁屏（PIN/指纹/图案）挡住。"""
        try:
            out = shell_text(self._d, "dumpsys window")
        except Exception:  # noqa: BLE001
            return False
        for line in out.splitlines():
            if "isKeyguardShowing=true" in line or "mShowingLockscreen=true" in line:
                return True
        return False

    def unlock(self) -> bool:
        """尝试自动解锁。

        * 屏幕没亮先点亮
        * `wm dismiss-keyguard`（无密码锁屏可以）
        * 向上滑动（滑开非安全锁屏）
        有 PIN/指纹的安全锁屏无法自动解锁，返回 False。
        """
        try:
            if not self._d.info.get("screenOn"):
                self._d.screen_on()
                time.sleep(0.8)
        except Exception:  # noqa: BLE001
            pass
        for cmd in ("wm dismiss-keyguard", "input keyevent 224"):
            try:
                subprocess.run(
                    ["adb", "-s", self.serial, "shell", *cmd.split()],
                    capture_output=True,
                    timeout=8,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.8)
            if not self.keyguard_showing():
                return True
        # 再试着滑一下（非安全锁屏）
        try:
            w, h = self.screen().size
            self._d.swipe(w // 2, int(h * 0.85), w // 2, int(h * 0.25), 0.25)
            time.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass
        return not self.keyguard_showing()

    def set_lock_screen(self, enabled: bool) -> Tuple[str, bool]:
        """开关系统锁屏。返回 (命令输出, 实际是否已生效)。

        注意（Android 官方说明）：
            "If the lock screen is secure, this has no immediate effect.
             I.e. this can only change between Swipe and None."
        也就是**设备设了 PIN/图案/指纹时，关不掉系统锁屏**。
        这种情况下真正的护栏是：
            - screen_off_timeout 设成最大值（屏幕永不熄灭 → keyguard 永不弹出）
            - stay_on_while_plugged_in=1（充电时常亮）
            - 开跑前自检发现被锁屏挡住就中止并提示手动解锁
        """
        cmd = ["adb", "-s", self.serial, "shell", "locksettings", "set-disabled",
               "false" if enabled else "true"]
        proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
        out = (proc.stdout.decode("utf-8", "ignore") + proc.stderr.decode("utf-8", "ignore")).strip()
        state = self.lock_screen_disabled()
        return out, state

    def lock_screen_disabled(self) -> bool:
        """系统锁屏是否已被真正关闭。"""
        try:
            out = shell_text(self._d, "locksettings get-disabled")
            return out.strip().lower() == "true"
        except Exception:  # noqa: BLE001
            return False

    # ---------------- App 操作 ----------------

    def app_start(self, package: str, activity: Optional[str] = None) -> None:
        self._d.app_start(package, activity=activity, stop=False)

    def app_stop(self, package: str) -> None:
        try:
            self._d.app_stop(package)
        except Exception:  # noqa: BLE001
            pass

    def app_current(self) -> Tuple[str, str]:
        info = self._d.app_current()
        return info.get("package", ""), info.get("activity", "")

    def open_deep_link(self, url: str) -> None:
        try:
            self._d.shell(f"am start -a android.intent.action.VIEW -d '{url}'")
        except Exception as exc:  # noqa: BLE001
            raise DeviceError(f"打开深度链接失败: {exc}") from exc

    def press(self, key: str) -> None:
        self._d.press(key)

    def click(self, x: int, y: int) -> None:
        self._d.click(x, y)

    def tap(self, x: int, y: int, method: str = "auto") -> None:
        """点击。method=auto 时用实测更快的通道。

        真机实测（RMX5090 / USB）：
          adb shell input tap  ≈ 70ms
          atx-agent JSON-RPC  ≈ 167ms
        所以到点连点默认走 adb shell。
        """
        if method == "u2":
            self._d.click(x, y)
            return
        if method == "adb" or self._fast_tap_is_adb:
            self._adb_tap(x, y)
            return
        self._d.click(x, y)

    def _adb_tap(self, x: int, y: int) -> None:
        try:
            subprocess.run(
                ["adb", "-s", self.serial, "shell", "input", "tap", str(x), str(y)],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:  # noqa: BLE001
            self._d.click(x, y)

    def benchmark_tap(self) -> str:
        """实测两种点击通道的延迟，返回更快的那种（"adb" / "u2"）。"""
        import time

        samples = {"adb": [], "u2": []}
        for _ in range(3):
            t0 = time.perf_counter()
            self._adb_tap(100, 100)
            samples["adb"].append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            try:
                self._d.click(100, 100)
            except Exception:  # noqa: BLE001
                pass
            samples["u2"].append(time.perf_counter() - t0)
        adb_avg = sum(samples["adb"]) / len(samples["adb"])
        u2_avg = sum(samples["u2"]) / len(samples["u2"])
        self._tap_bench = {"adb": adb_avg * 1000, "u2": u2_avg * 1000}
        self._fast_tap_is_adb = adb_avg <= u2_avg
        return "adb" if self._fast_tap_is_adb else "u2"

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3) -> None:
        self._d.swipe(x1, y1, x2, y2, duration)

    def type_text(self, text: str) -> None:
        self._d.send_keys(text)

    # ---------------- 证据 ----------------

    def screenshot(self, prefix: str = "shot") -> Optional[Any]:
        if not self.recorder:
            return None
        return self.recorder.screenshot(self._d, prefix)

    def dump(self, prefix: str = "dump") -> Optional[Any]:
        if not self.recorder:
            return None
        return self.recorder.dump(self._d, prefix)

    def event(self, name: str, **fields) -> None:
        if self.recorder:
            self.recorder.event(name, **fields)


def pick_device(serial: Optional[str], connect_to: Optional[str]) -> Optional[str]:
    """在多个候选中挑一个在线设备。"""
    if serial:
        return serial
    if connect_to:
        return connect_to
    devices = list_devices()
    if not devices:
        return None
    for candidate in DEFAULT_CONNECT_CANDIDATES:
        if candidate in devices:
            return candidate
    return devices[0]


_ANDROID_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def device_summary() -> List[Dict[str, Any]]:
    """汇总所有在线设备的型号/版本信息，供 doctor 使用。"""
    out = []
    for serial in list_devices():
        try:
            props = adb_run("-s", serial, "shell", "getprop")
        except DeviceError:
            continue
        mapping = {}
        for line in props.splitlines():
            # getprop 输出格式:  [ro.product.model]: [PJJ110]
            if "]" in line:
                key, _, value = line.partition("]:")
                mapping[key.lstrip("[").strip()] = value.strip().lstrip("[").rstrip("]").strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                mapping[key.strip()] = value.strip()
        out.append(
            {
                "serial": serial,
                "model": mapping.get("ro.product.model", "?"),
                "brand": mapping.get("ro.product.brand", "?"),
                "android": mapping.get("ro.build.version.release", "?"),
                "sdk": mapping.get("ro.build.version.sdk", "?"),
                "abi": mapping.get("ro.product.cpu.abi", "?"),
            }
        )
    return out
