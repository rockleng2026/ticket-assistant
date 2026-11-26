"""抢票任务：先建任务、备好流程，到点再续跑。

一个「任务」= 一场演出的完整抢票计划，可以提前几天建好，等到开抢前 1 小时
再 `task resume` 继续执行。

任务与配方的关系：
  * 任务（tasks/<id>.json）保存「目标 + 参数 + 时间表 + 状态」
  * 配方（recipes/<id>.yaml）保存「怎么点」，由任务创建时按平台模板生成
  * `task resume` 会把任务参数同步回配方，然后走 arm -> 等待 -> 抢
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import CST, Recipe, load_recipe, save_recipe
from .logging_setup import RunRecorder

TASKS_DIRNAME = "tasks"
TASK_STATUS = ("created", "armed", "fired", "done", "failed")


class TaskError(Exception):
    pass


def slugify(text: str) -> str:
    """把演出名转成安全的文件名/任务 id。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", (text or "").strip())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:60] or "task"


@dataclasses.dataclass
class Schedule:
    sale_at: Optional[dt.datetime] = None
    prepare_lead_minutes: int = 60
    lead_seconds: float = 0.5

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Schedule":
        at = raw.get("sale_at")
        if isinstance(at, str) and at.strip():
            at = dt.datetime.fromisoformat(at.strip().replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=CST)
        else:
            at = None
        return cls(
            sale_at=at,
            prepare_lead_minutes=int(raw.get("prepare_lead_minutes", 60)),
            lead_seconds=float(raw.get("lead_seconds", 0.5)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sale_at": self.sale_at.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")
            if self.sale_at
            else None,
            "prepare_lead_minutes": self.prepare_lead_minutes,
            "lead_seconds": self.lead_seconds,
        }


@dataclasses.dataclass
class Task:
    id: str
    name: str
    platform: str = "meituan"
    status: str = "created"
    created_at: str = ""
    schedule: Schedule = dataclasses.field(default_factory=Schedule)
    target: Dict[str, Any] = dataclasses.field(default_factory=dict)
    params: Dict[str, Any] = dataclasses.field(default_factory=dict)
    recipe: str = ""
    history: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    source_path: Optional[Path] = None

    # ---------------- 序列化 ----------------

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], path: Optional[Path] = None) -> "Task":
        return cls(
            id=raw.get("id", "task"),
            name=raw.get("name", ""),
            platform=raw.get("platform", "meituan"),
            status=raw.get("status", "created"),
            created_at=raw.get("created_at", ""),
            schedule=Schedule.from_dict(raw.get("schedule", {})),
            target=raw.get("target", {}) or {},
            params=raw.get("params", {}) or {},
            recipe=raw.get("recipe", ""),
            history=raw.get("history", []) or [],
            source_path=path,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "status": self.status,
            "created_at": self.created_at,
            "schedule": self.schedule.to_dict(),
            "target": self.target,
            "params": self.params,
            "recipe": self.recipe,
            "history": self.history,
        }

    # ---------------- 展示 ----------------

    def sale_text(self) -> str:
        if not self.schedule.sale_at:
            return "未设置"
        return self.schedule.sale_at.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")

    def describe(self) -> str:
        prices = " > ".join(self.params.get("tier_priority", []) or [])
        return (
            f"{self.name}\n"
            f"    平台 {self.platform} | 状态 {self.status}\n"
            f"    开售 {self.sale_text()} | 价格 {prices} | 数量 {self.params.get('quantity')} 张"
            f" | 观演人 {self.params.get('attendees')} 位\n"
            f"    配方 {self.recipe}"
        )

    def countdown_text(self) -> str:
        if not self.schedule.sale_at:
            return "未设置开售时间"
        delta = (self.schedule.sale_at - dt.datetime.now(dt.timezone.utc).astimezone(CST)).total_seconds()
        if delta <= 0:
            return "已开售"
        days, rem = divmod(int(delta), 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        return f"{days}天{hours:02d}时{mins:02d}分{secs:02d}秒"

    def log(self, event: str, **fields) -> None:
        self.history.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event, **fields})


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #


def tasks_dir(root: Path) -> Path:
    return Path(root) / TASKS_DIRNAME


def list_tasks(root: Path) -> List[Task]:
    d = tasks_dir(root)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(Task.from_dict(json.loads(p.read_text(encoding="utf-8")), p))
        except Exception:  # noqa: BLE001
            continue
    return out


def load_task(root: Path, task_id: str) -> Task:
    p = tasks_dir(root) / f"{task_id}.json"
    if not p.exists():
        raise TaskError(f"任务不存在: {task_id}（用 `gpiao task list` 看有哪些）")
    return Task.from_dict(json.loads(p.read_text(encoding="utf-8")), p)


def save_task(root: Path, task: Task) -> Path:
    d = tasks_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{task.id}.json"
    task.source_path = p
    p.write_text(json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def delete_task(root: Path, task_id: str) -> None:
    p = tasks_dir(root) / f"{task_id}.json"
    if p.exists():
        p.unlink()


# --------------------------------------------------------------------------- #
# 任务 -> 配方
# --------------------------------------------------------------------------- #


def apply_params_to_recipe(recipe: Recipe, task: Task) -> Recipe:
    """把任务里的参数同步回配方。"""
    prices = task.params.get("tier_priority") or recipe.concert.tier_priority
    recipe.concert.tier_priority = list(prices)
    if task.params.get("quantity"):
        recipe.concert.quantity = int(task.params["quantity"])
        recipe.concert.attendees = int(task.params.get("attendees", recipe.concert.quantity))
    if task.target.get("keyword"):
        recipe.concert.search_keyword = task.target["keyword"]
    if task.target.get("show_name"):
        recipe.concert.name = task.target["show_name"]
    if task.target.get("result_match"):
        recipe.concert.result_match = task.target["result_match"]
        # 搜索结果控件改用更精确的匹配串（避免点到搜索框里的输入内容）
        for step in recipe.steps:
            if step.get("action") == "wait" and step.get("target") == "result_show":
                step["target"] = "result_show"
        if "result_show" in recipe.selectors:
            recipe.selectors["result_show"] = {
                "candidates": [
                    {"textContains": task.target["result_match"]},
                    {"textContains": recipe.concert.search_keyword},
                ]
            }
    if task.schedule.sale_at:
        recipe.sale.at = task.schedule.sale_at
        recipe.sale.lead_seconds = task.schedule.lead_seconds
    # 优先级也写进步骤里（select_tier 的 priority 是写死的）
    for step in recipe.steps:
        if step.get("action") == "loop":
            for inner in step.get("steps", []):
                if inner.get("action") == "select_tier":
                    inner["priority"] = list(prices)
    return recipe


def recipe_path(root: Path, task_id: str) -> Path:
    return Path(root) / "recipes" / f"{task_id}.yaml"


def bind_recipe(root: Path, task: Task) -> Path:
    """任务 -> 配方的同步入口。"""
    p = recipe_path(root, task.id)
    recipe = load_recipe(p)
    apply_params_to_recipe(recipe, task)
    save_recipe(recipe, p)
    return p


def evidence_dir(root: Path, task_id: str, tag: str) -> RunRecorder:
    return RunRecorder(Path(root) / "runs", tag=f"{task_id}-{tag}")
