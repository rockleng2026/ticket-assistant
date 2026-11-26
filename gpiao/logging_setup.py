"""日志与运行留证（截图 / UI dump / 事件流水）。"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

_LOG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(name)-18s | %(message)s"


class RunRecorder:
    """一次运行的证据目录：截图、UI dump、事件日志。"""

    def __init__(self, root: Path, tag: str = "run"):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = Path(root) / f"{stamp}-{tag}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._log = logging.getLogger("gpiao.recorder")

    def _next(self, prefix: str) -> Path:
        self._seq += 1
        return self.dir / f"{self._seq:03d}-{prefix}.png"

    def screenshot(self, device, prefix: str = "shot") -> Optional[Path]:
        try:
            path = self._next(prefix)
            device.screenshot().save(path)
            return path
        except Exception as exc:  # noqa: BLE001
            self._log.warning("截图失败: %s", exc)
            return None

    def dump(self, device, prefix: str = "dump") -> Optional[Path]:
        try:
            path = self.dir / f"{self._seq:03d}-{prefix}.xml"
            device.dump_hierarchy().encode("utf-8").decode("utf-8", "ignore")
            path.write_text(device.dump_hierarchy(), encoding="utf-8")
            return path
        except Exception as exc:  # noqa: BLE001
            self._log.warning("UI dump 失败: %s", exc)
            return None

    def event(self, name: str, **fields) -> None:
        parts = " ".join(f"{k}={v}" for k, v in fields.items())
        self._log.info("EVENT %s %s", name, parts)
        with (self.dir / "events.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.time():.3f}\t{name}\t{parts}\n")


def setup_logging(verbose: bool = False, logfile: Optional[Path] = None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("gpiao")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    stream = logging.StreamHandler(sys.stdout)
    stream.setLevel(level)
    stream.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(fh)

    logging.getLogger("adbutils").setLevel(logging.WARNING)
    logging.getLogger("uiautomator2").setLevel(logging.WARNING)
