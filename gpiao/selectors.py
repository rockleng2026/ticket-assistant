"""控件选择器：多候选 + 降级策略。

核心思想：不要用「死坐标」点击。用「文本 / resource-id / 描述 / XPath」定位，
只有在全部候选都失败时才回落到按屏幕比例计算的坐标（不同机型分辨率不一样，
所以坐标一律存成相对比例）。

一个 Target 由若干 Candidate 组成，按顺序尝试：

    {"text": "立即购买"}
    {"textContains": "购买"}
    {"textMatches": "立即购买|马上抢"}
    {"descContains": "购买"}
    {"resourceId": "com.sankuai.meituan:id/buy"}
    {"resourceIdRegex": "buy"}
    {"className": "android.widget.Button"}
    {"xpath": "//*[@text='立即购买']"}
    {"coord": [0.5, 0.93]}          # 最后的兜底
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 候选里允许出现的键 -> uiautomator2 选择器参数
DIRECT_KEYS = (
    "text",
    "textContains",
    "textMatches",
    "textStartsWith",
    "desc",
    "descContains",
    "descMatches",
    "descStartsWith",
    "resourceId",
    "resourceIdRegex",
    "className",
    "packageName",
    "checked",
    "enabled",
    "focused",
    "scrollable",
    "selected",
)

# 友好名 -> uiautomator2 实际参数名
KEY_TRANSLATE = {
    "desc": "description",
    "descContains": "descriptionContains",
    "descMatches": "descriptionMatches",
    "descStartsWith": "descriptionStartsWith",
    "resourceIdRegex": "resourceIdMatches",
}

SPECIAL_KEYS = ("xpath", "coord", "index", "prefer", "scope", "bottomButton")


class SelectorError(Exception):
    pass


def normalize(text: str) -> str:
    """全角空格/换行归一，避免 UI 文本细微差异导致匹配失败。"""
    return re.sub(r"\s+", "", (text or "").replace("　", " ")).strip()


def _to_bounds(info: Any) -> Optional[Tuple[int, int, int, int]]:
    """从 uiautomator2 返回的信息里取 bounds。

    uiautomator2 3.x 的 info["bounds"] 是 {'left','top','right','bottom'} 字典；
    老版本 / 自定义实现里可能是 [x1,y1,x2,y2] 列表或 "[x1,y1][x2,y2]" 字符串。
    """
    if info is None:
        return None
    raw = None
    if isinstance(info, dict):
        raw = info.get("bounds") or info.get("visibleBounds")
    else:
        raw = getattr(info, "bounds", None)
        if callable(raw):
            try:
                raw = raw()
            except Exception:  # noqa: BLE001
                raw = None
    if raw is None:
        return None
    if isinstance(raw, dict):
        try:
            return (
                int(raw["left"]),
                int(raw["top"]),
                int(raw["right"]),
                int(raw["bottom"]),
            )
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return tuple(int(v) for v in raw)  # type: ignore[return-value]
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", str(raw))
    if m:
        return tuple(int(g) for g in m.groups())  # type: ignore[return-value]
    return None


def _area(bounds: Optional[Tuple[int, int, int, int]]) -> int:
    if not bounds:
        return 0
    x1, y1, x2, y2 = bounds
    return max(0, x2 - x1) * max(0, y2 - y1)


@dataclass
class Candidate:
    """单个定位方案。"""

    raw: Dict[str, Any]
    index: int = 0
    prefer: str = "first"

    @property
    def is_coord(self) -> bool:
        return "coord" in self.raw

    @property
    def is_bottom_button(self) -> bool:
        return bool(self.raw.get("bottomButton"))

    def build(self, device):
        """构造 uiautomator2 选择器对象。"""
        if self.is_coord:
            return None
        if "xpath" in self.raw:
            return device.xpath(self.raw["xpath"])
        kwargs = {}
        for k, v in self.raw.items():
            if k in DIRECT_KEYS:
                kwargs[KEY_TRANSLATE.get(k, k)] = v
        if not kwargs:
            raise SelectorError(f"候选没有可用的定位条件: {self.raw}")
        return device(**kwargs)

    def coord(self, screen_size: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        if not self.is_coord:
            return None
        xr, yr = self.raw["coord"]
        w, h = screen_size
        return int(round(float(xr) * w)), int(round(float(yr) * h))

    def describe(self) -> str:
        if self.is_coord:
            return f"coord@{self.raw['coord']}"
        parts = [f"{k}={v!r}" for k, v in self.raw.items() if k not in SPECIAL_KEYS]
        return ",".join(parts) or "?"


def _find_bottom_button(device, screen_size) -> Optional[Match]:
    """找屏幕最底部的按钮（android.widget.Button / 可点元素）。

    国内 App 的主按钮文案经常是动态的（例如「已减0.45元，月付交易」、
    「确认支付 ￥479.55」），按文案匹配很脆；"最底下那个按钮" 才是稳定锚点。
    """
    try:
        from lxml import etree

        xml = device.u2.dump_hierarchy()
        root = etree.fromstring(xml.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return None

    w, h = screen_size
    best = None
    for node in root.iter("node"):
        cls = node.get("class", "") or ""
        bounds = node.get("bounds", "") or ""
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        if y1 < h * 0.72:
            continue
        is_button = cls.endswith("Button")
        clickable = node.get("clickable") == "true"
        if not (is_button or clickable):
            continue
        score = y1 + (100000 if is_button else 0)
        if best is None or score > best[0]:
            best = (score, node, (x1, y1, x2, y2), is_button)
    if best is None:
        return None
    _, node, bounds, is_button = best
    return Match(
        candidate=Candidate(raw={"bottomButton": True}),
        selector=None,
        bounds=bounds,
        clickable=True,
        text=node.get("text", "") or "",
        resource_id=node.get("resource-id", "") or "",
    )


@dataclass
class Match:
    candidate: Candidate
    selector: Any
    bounds: Optional[Tuple[int, int, int, int]]
    clickable: bool
    text: str = ""
    resource_id: str = ""

    @property
    def center(self) -> Optional[Tuple[int, int]]:
        if not self.bounds:
            return None
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    def score(self, prefer: str) -> Tuple[int, int, int]:
        """排序键：越大越优先。"""
        clickable = 1 if self.clickable else 0
        area = _area(self.bounds)
        bottom = self.bounds[3] if self.bounds else 0
        if prefer == "clickable":
            return (clickable, area, bottom)
        if prefer == "bottom":
            return (clickable, bottom, area)
        if prefer == "largest":
            return (clickable, area, bottom)
        return (clickable, area, bottom)


def _tap(device, x: int, y: int) -> None:
    """统一落点：优先用实测更快的通道。"""
    tapper = getattr(device, "tap", None)
    if callable(tapper):
        tapper(x, y)
    else:
        device.click(x, y)


@dataclass
class Target:
    """一组候选描述的控件。"""

    name: str
    candidates: List[Candidate] = field(default_factory=list)
    required: bool = True
    timeout: float = 10.0
    optional_texts: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, raw: Any) -> "Target":
        if isinstance(raw, dict):
            if "candidates" in raw:
                items = raw["candidates"]
                timeout = float(raw.get("timeout", 10))
                required = bool(raw.get("required", True))
                aliases = list(raw.get("aliases", []))
            else:
                items = [raw]
                timeout = 10.0
                required = True
                aliases = []
        elif isinstance(raw, list):
            items = raw
            timeout = 10.0
            required = True
            aliases = []
        else:
            items = [{"text": str(raw)}]
            timeout = 10.0
            required = True
            aliases = []

        candidates: List[Candidate] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                item = {"text": str(item)}
            item = dict(item)
            prefer = item.pop("prefer", "first")
            candidates.append(Candidate(raw=item, index=idx, prefer=prefer))

        # 别名（如按钮文案会从「即将开售」变成「立即购买」）
        for alias in aliases:
            candidates.insert(0, Candidate(raw={"text": alias}, index=0))

        return cls(
            name=name,
            candidates=candidates,
            required=required,
            timeout=timeout,
            optional_texts=aliases,
        )

    def describe(self) -> str:
        return f"{self.name}[" + " | ".join(c.describe() for c in self.candidates) + "]"

    # ------------- 匹配 -------------

    def _iter_matches(self, device, screen_size) -> List[Match]:
        matches: List[Match] = []
        for cand in self.candidates:
            if cand.is_coord:
                # 坐标兜底：没有 UI 节点也算命中（最后 resort）
                pt = cand.coord(screen_size)
                matches.append(
                    Match(
                        candidate=cand,
                        selector=None,
                        bounds=(pt[0], pt[1], pt[0], pt[1]) if pt else None,
                        clickable=True,
                        text="<coord>",
                    )
                )
                break
            if cand.is_bottom_button:
                m = _find_bottom_button(device, screen_size)
                if m is not None:
                    matches.append(m)
                    break
                continue
            try:
                sel = cand.build(device)
            except SelectorError:
                continue
            except Exception:  # noqa: BLE001
                continue
            if sel is None:
                continue
            try:
                count = sel.count
            except Exception:  # noqa: BLE001
                continue
            if not count:
                continue
            limit = count if cand.index == 0 else min(count, cand.index + 1)
            for i in range(limit):
                node = sel[i]
                info = getattr(node, "info", None)
                bounds = _to_bounds(info)
                clickable = False
                text = ""
                resource_id = ""
                if isinstance(info, dict):
                    clickable = bool(info.get("clickable"))
                    text = info.get("text") or ""
                    resource_id = info.get("resourceId") or info.get("resourceName") or ""
                else:
                    clickable = bool(getattr(info, "clickable", False))
                    text = getattr(info, "text", "") or ""
                    resource_id = getattr(info, "resourceId", "") or ""
                matches.append(
                    Match(
                        candidate=cand,
                        selector=node,
                        bounds=bounds,
                        clickable=clickable,
                        text=text,
                        resource_id=resource_id,
                    )
                )
            if matches:
                break  # 命中一个候选就不再尝试后面的
        return matches

    def find(self, device, screen_size=None) -> Optional[Match]:
        if screen_size is None:
            screen_size = device.screen().size
        matches = self._iter_matches(device, screen_size)
        if not matches:
            return None
        prefer = self.candidates[0].prefer if self.candidates else "first"
        matches.sort(key=lambda m: m.score(prefer), reverse=True)
        return matches[0]

    def exists(self, device, screen_size=None) -> bool:
        return self.find(device, screen_size) is not None

    def wait(self, device, timeout: Optional[float] = None, screen_size=None) -> Optional[Match]:
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            m = self.find(device, screen_size)
            if m is not None:
                return m
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.12)

    # ------------- 操作 -------------

    def click(self, device, screen_size=None, dry_run: bool = False) -> bool:
        """点击目标。优先点「可点击」的节点中心，回落到坐标兜底。"""
        screen_size = screen_size or device.screen().size
        m = self.find(device, screen_size)
        if m is None:
            return False
        if dry_run:
            return True
        if m.candidate.is_coord:
            x, y = m.candidate.coord(screen_size)  # type: ignore[misc]
            _tap(device, x, y)
            return True
        center = m.center
        if center:
            _tap(device, *center)
            return True
        try:
            m.selector.click()
            return True
        except Exception:  # noqa: BLE001
            return False

    def click_fast(self, device, screen_size=None) -> bool:
        """极速点击：直接用兜底坐标落点，**不做任何 UI 查询**。

        到点那一瞬间每 100ms 的一次 uiautomator 查询就是全部的胜负手，
        所以这一步必须走零查询路径。坐标来自 record 标定时保存的屏幕比例。
        """
        screen_size = screen_size or device.screen().size
        for cand in self.candidates:
            if not cand.is_coord:
                continue
            pt = cand.coord(screen_size)
            if not pt:
                continue
            _tap(device, *pt)
            return True
        return False

    def coord_candidates(self) -> List[Candidate]:
        return [c for c in self.candidates if c.is_coord]


def build_targets(selectors: Dict[str, Any]) -> Dict[str, Target]:
    targets: Dict[str, Target] = {}
    for name, raw in (selectors or {}).items():
        try:
            targets[name] = Target.from_dict(name, raw)
        except Exception:  # noqa: BLE001
            continue
    return targets


def pick_candidate_interactive(device, screen_size=None) -> Optional[Dict[str, Any]]:
    """列出当前页面上所有可点/有文本的节点，返回被选中节点的候选描述。"""
    screen_size = screen_size or device.screen().size
    xml = device.u2.dump_hierarchy()
    from lxml import etree  # uiautomator2 依赖 lxml，这里直接复用

    root = etree.fromstring(xml.encode("utf-8"))
    rows = []
    for node in root.iter("node"):
        text = node.get("text", "") or ""
        desc = node.get("content-desc", "") or ""
        rid = node.get("resource-id", "") or ""
        cls = node.get("class", "") or ""
        clickable = node.get("clickable", "false") == "true"
        bounds = node.get("bounds", "")
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds)
        if not (text or desc or rid or clickable):
            continue
        if not m:
            continue
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        rows.append(
            {
                "text": text,
                "desc": desc,
                "resourceId": rid,
                "className": cls,
                "clickable": clickable,
                "bounds": (x1, y1, x2, y2),
                "center": ((x1 + x2) // 2, (y1 + y2) // 2),
            }
        )
    if not rows:
        return None

    print("\n=== 当前页面可定位节点 ===")
    for i, row in enumerate(rows):
        label = row["text"] or row["desc"] or f"<{row['className'].split('.')[-1]}>"
        flags = "".join(
            [
                "C" if row["clickable"] else " ",
                "T" if row["text"] else " ",
                "D" if row["desc"] else " ",
            ]
        )
        print(f"[{i:3d}] {flags} {label!r:<40} rid={row['resourceId'][-40:]:<42} {row['bounds']}")
    return rows


def candidate_from_row(row: Dict[str, Any], screen_size: Tuple[int, int], aliases: Sequence[str] = ()) -> Dict[str, Any]:
    """把选中的节点转成候选列表。"""
    cands: List[Dict[str, Any]] = []
    for alias in aliases:
        alias = alias.strip()
        if alias:
            cands.append({"text": alias})
    if row["text"]:
        cands.append({"text": row["text"]})
    if row["desc"]:
        cands.append({"descContains": row["desc"]})
    if row["resourceId"]:
        cands.append({"resourceId": row["resourceId"]})
    x, y = row["center"]
    w, h = screen_size
    cands.append({"coord": [round(x / w, 4), round(y / h, 4)]})
    return {"candidates": cands, "aliases": list(aliases)}
