"""配置与配方（Recipe）加载。

一个 Recipe 描述一场演出的完整抢票策略：
  - 设备与 App 前置条件
  - 演出定位信息（搜索词、场次、票档、数量）
  - 开售时间
  - 控件选择器（由 record 命令标定生成）
  - 步骤序列（数据驱动，可在不改代码的情况下调参）
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CST = dt.timezone(dt.timedelta(hours=8))

DEFAULT_APP = {
    "meituan": {
        "package": "com.sankuai.meituan",
        "search_entry_hint": "搜索",
    },
    "damai": {
        "package": "cn.damai",
        "search_entry_hint": "搜索",
    },
    "maoyan": {
        "package": "com.sankuai.meituan",
        "search_entry_hint": "搜索",
    },
}


class ConfigError(Exception):
    """配方配置不合法。"""


@dataclasses.dataclass
class AppConfig:
    package: str = "com.sankuai.meituan"
    main_activity: Optional[str] = None
    search_entry_hint: str = "搜索"
    deep_link: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], platform: str = "meituan") -> "AppConfig":
        merged = dict(DEFAULT_APP.get(platform, {}))
        merged.update(raw or {})
        return cls(
            package=merged.get("package", "com.sankuai.meituan"),
            main_activity=merged.get("main_activity"),
            search_entry_hint=merged.get("search_entry_hint", "搜索"),
            deep_link=merged.get("deep_link"),
        )


@dataclasses.dataclass
class ConcertConfig:
    name: str = ""
    search_keyword: str = ""
    session: Optional[str] = None
    tier: Optional[str] = None
    tier_priority: List[str] = dataclasses.field(default_factory=lambda: ["¥680", "¥480", "¥880"])
    quantity: int = 1
    max_price: Optional[int] = None
    attendees: int = 0

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ConcertConfig":
        tier_priority = raw.get("tier_priority")
        if not tier_priority:
            tier_priority = ["¥680", "¥480", "¥880"]
        if isinstance(tier_priority, str):
            tier_priority = [p.strip() for p in tier_priority.split(",") if p.strip()]
        return cls(
            name=raw.get("name", ""),
            search_keyword=raw.get("search_keyword", ""),
            session=raw.get("session"),
            tier=raw.get("tier"),
            tier_priority=list(tier_priority),
            quantity=int(raw.get("quantity", 1)),
            max_price=raw.get("max_price"),
            attendees=int(raw.get("attendees", raw.get("quantity", 1))),
        )


@dataclasses.dataclass
class SaleConfig:
    """开售时间与触发策略。"""

    at: Optional[dt.datetime] = None
    at_text: Optional[str] = None
    lead_seconds: float = 1.2
    burst_seconds: float = 6.0
    tap_interval_ms: int = 180
    timezone: dt.tzinfo = CST
    wait_timeout: float = 900.0
    preflight_seconds: float = 300.0

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SaleConfig":
        tz = CST
        tzname = raw.get("timezone")
        if tzname:
            tz = _parse_tz(tzname)
        at = None
        at_text = raw.get("at")
        if isinstance(at_text, dt.datetime):
            at = at_text
        elif isinstance(at_text, str) and at_text.strip():
            at = _parse_datetime(at_text.strip(), tz)
        return cls(
            at=at,
            at_text=raw.get("at") if isinstance(raw.get("at"), str) else None,
            lead_seconds=float(raw.get("lead_seconds", 1.2)),
            burst_seconds=float(raw.get("burst_seconds", 6.0)),
            tap_interval_ms=int(raw.get("tap_interval_ms", 180)),
            timezone=tz,
            wait_timeout=float(raw.get("wait_timeout", 900)),
            preflight_seconds=float(raw.get("preflight_seconds", 300)),
        )

    def describe(self) -> str:
        if self.at is None:
            return "未设置开售时间"
        return self.at.astimezone(self.timezone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _parse_tz(name: str) -> dt.tzinfo:
    if name.upper() in ("UTC", "Z"):
        return dt.timezone.utc
    m = re.fullmatch(r"(?:UTC)?([+-])(\d{1,2}):?(\d{2})?", name.strip())
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        minutes = int(m.group(3) or 0)
        return dt.timezone(sign * dt.timedelta(hours=hours, minutes=minutes))
    raise ConfigError(f"无法解析时区: {name}")


def _parse_datetime(text: str, tz: dt.tzinfo) -> dt.datetime:
    """支持 '2026-01-01 12:00:00' / ISO8601 / 带时区偏移。"""
    text = text.replace("/", "-").replace("T", " ").strip()
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S.%f",
        ):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ConfigError(f"无法解析开售时间: {text!r}（建议格式 2026-01-01 12:00:00）")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


@dataclasses.dataclass
class DeviceConfig:
    serial: Optional[str] = None
    connect: Optional[str] = None
    auto_prepare: bool = True
    orientation: str = "portrait"

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "DeviceConfig":
        return cls(
            serial=raw.get("serial"),
            connect=raw.get("connect"),
            auto_prepare=bool(raw.get("auto_prepare", True)),
            orientation=raw.get("orientation", "portrait"),
        )


@dataclasses.dataclass
class Recipe:
    name: str = "未命名配方"
    platform: str = "meituan"
    app: AppConfig = dataclasses.field(default_factory=AppConfig)
    concert: ConcertConfig = dataclasses.field(default_factory=ConcertConfig)
    sale: SaleConfig = dataclasses.field(default_factory=SaleConfig)
    device: DeviceConfig = dataclasses.field(default_factory=DeviceConfig)
    selectors: Dict[str, Any] = dataclasses.field(default_factory=dict)
    steps: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    stop_before_payment: bool = True
    submit_order: bool = False
    auto_pay: bool = False
    source_path: Optional[Path] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], path: Optional[Path] = None) -> "Recipe":
        platform = raw.get("platform", "meituan")
        recipe = cls(
            name=raw.get("name", path.stem if path else "未命名配方"),
            platform=platform,
            app=AppConfig.from_dict(raw.get("app", {}), platform),
            concert=ConcertConfig.from_dict(raw.get("concert", {})),
            sale=SaleConfig.from_dict(raw.get("sale", {})),
            device=DeviceConfig.from_dict(raw.get("device", {})),
            selectors=raw.get("selectors", {}) or {},
            steps=raw.get("steps", []) or [],
            stop_before_payment=bool(raw.get("stop_before_payment", True)),
            submit_order=bool(raw.get("submit_order", False)),
            auto_pay=bool(raw.get("auto_pay", False)),
            source_path=path,
        )
        recipe.validate()
        return recipe

    def validate(self) -> None:
        if not self.steps:
            raise ConfigError("配方缺少 steps 步骤列表")
        if self.concert.quantity < 1:
            raise ConfigError("concert.quantity 必须 >= 1")
        if self.concert.quantity > 6:
            raise ConfigError("单次购票数量超过 6 张，多数平台有上限")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "platform": self.platform,
            "app": dataclasses.asdict(self.app),
            "concert": dataclasses.asdict(self.concert),
            "sale": {
                "at": self.sale.at_text,
                "lead_seconds": self.sale.lead_seconds,
                "burst_seconds": self.sale.burst_seconds,
                "tap_interval_ms": self.sale.tap_interval_ms,
                "timezone": str(self.sale.timezone),
                "wait_timeout": self.sale.wait_timeout,
                "preflight_seconds": self.sale.preflight_seconds,
            },
            "device": dataclasses.asdict(self.device),
            "selectors": self.selectors,
            "steps": self.steps,
            "stop_before_payment": self.stop_before_payment,
            "submit_order": self.submit_order,
            "auto_pay": self.auto_pay,
        }


def load_recipe(path: Path) -> Recipe:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"配方文件不存在: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配方文件格式错误（应为 YAML 映射）: {path}")
    return Recipe.from_dict(raw, path)


def save_recipe(recipe: Recipe, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(recipe.to_dict(), allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8",
    )
