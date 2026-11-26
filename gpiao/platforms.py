"""平台模板：任务创建时按平台生成配方。

* meituan —— 美团 App（猫眼演出供给），已完整标定
* damai   —— 大麦 App，脚手架（控件需要用 `gpiao record` 标定后才能真正抢）
"""

from __future__ import annotations

from typing import Any, Dict

from .config import CST, Recipe


def _meituan_recipe(task_name: str, keyword: str) -> Dict[str, Any]:
    return {
        "name": task_name,
        "platform": "meituan",
        "app": {
            "package": "com.sankuai.meituan",
            "main_activity": "com.meituan.android.pt.homepage.activity.MainActivity",
        },
        "concert": {
            "name": task_name,
            "search_keyword": keyword,
            "session": None,
            "tier_priority": ["¥680"],
            "quantity": 1,
            "attendees": 1,
        },
        "sale": {
            "at": "2026-01-01 12:00:00",
            "lead_seconds": 0.5,
            "burst_seconds": 8.0,
            "tap_interval_ms": 180,
            "timezone": "+08:00",
        },
        "device": {"connect": None, "serial": None, "auto_prepare": True},
        "stop_before_payment": True,
        "submit_order": True,
        "auto_pay": False,
        "selectors": {
            # 首页金刚区（注意是 content-desc，不是 text）
            "home_show": {"candidates": [{"descContains": "电影演出"}, {"text": "电影演出"}]},
            "tab_concert": {"candidates": [{"text": "演唱会"}, {"textContains": "演唱会"}]},
            "search_box": {
                "candidates": [
                    {"textContains": "搜索明星"},
                    {"descContains": "搜索"},
                    {"textContains": "搜索演出"},
                ]
            },
            # 搜索结果里的演出名（关键字匹配，别写死全名）
            "result_show": {"candidates": [{"textContains": keyword}]},
            # 城市/站点 tab（深圳站）
            "city_tab": {"candidates": [{"text": "深圳站"}, {"text": "深圳"}]},
            # 底部购买按钮：开售前「已 预 约」，开售后变「立即购票」
            "buy_button": {
                "candidates": [
                    {"text": "立即购票"},
                    {"text": "立即购买"},
                    {"text": "去抢票"},
                    {"textNorm": "已预约"},
                    {"textNorm": "预约"},
                    {"textContains": "购票"},
                    {"coord": [0.5585, 0.9414]},
                ],
                "aliases": ["立即购票", "立即购买", "去抢票", "马上抢", "预约", "即将开售"],
            },
            # 购票面板标志
            "ticket_ready": {
                "candidates": [
                    {"text": "选择票档"},
                    {"text": "请选择票档"},
                    {"text": "确认"},
                    {"text": "提交订单"},
                ]
            },
            "session_item": {"candidates": [{"textContains": "19:"}, {"textContains": "19:00"}]},
            # 订单页标志
            "order_ready": {
                "candidates": [
                    {"text": "立即支付"},
                    {"text": "确认购票"},
                    {"textContains": "需选择"},
                    {"textContains": "总计"},
                ]
            },
            "tier": {"candidates": [{"text": "¥680"}, {"text": "￥680"}, {"text": "680"}]},
            # 数量 +/- 是无文本 ImageView，只能按屏幕比例坐标定位
            "qty_plus": {
                "candidates": [
                    {"descContains": "增加"},
                    {"text": "＋"},
                    {"text": "+"},
                    {"coord": [0.9019, 0.8777]},
                ]
            },
            "confirm_button": {"candidates": [{"text": "确认"}, {"text": "提交订单"}]},
            "pay_button": {"candidates": [{"text": "立即支付"}, {"text": "立即付款"}]},
            "pay_confirm": {
                "candidates": [
                    {"textContains": "月付交易"},
                    {"textContains": "确认支付"},
                    {"textContains": "立即支付"},
                    {"bottomButton": True},
                ]
            },
            "logged_out_marker": {
                "candidates": [{"textContains": "去登录"}, {"textContains": "登录后"}]
            },
        },
        "steps": [
            {"action": "wake", "phase": "pre"},
            {"action": "launch", "phase": "pre", "settle": 5.0},
            {
                "action": "dismiss",
                "phase": "pre",
                "rules": [
                    {"back_when": "登录体验完整功能", "timeout": 3, "settle": 1.5},
                    {"back_when": "欢迎登录美团", "timeout": 3, "settle": 1.5},
                    {"text": "允许", "timeout": 3, "settle": 1.0, "optional": True},
                    {"text": "同意", "timeout": 3, "settle": 1.5, "optional": True},
                    {"text": "以后再说", "timeout": 2, "settle": 1.0, "optional": True},
                ],
            },
            {"action": "wait", "phase": "pre", "target": "home_show", "timeout": 25},
            {"action": "tap", "phase": "pre", "target": "home_show", "settle": 6.0},
            {"action": "wait", "phase": "pre", "target": "tab_concert", "timeout": 20},
            {"action": "tap", "phase": "pre", "target": "tab_concert", "settle": 6.0},
            {"action": "wait", "phase": "pre", "target": "search_box", "timeout": 20},
            {"action": "search_submit", "phase": "pre", "text": keyword, "settle": 5.0},
            {"action": "wait", "phase": "pre", "target": "result_show", "timeout": 20},
            {"action": "tap", "phase": "pre", "target": "result_show", "settle": 8.0},
            {
                "action": "tap",
                "phase": "pre",
                "target": "city_tab",
                "settle": 4.0,
                "optional": True,
            },
            # 开票公告 / 票务须知：正文默认截断，先点末尾的「展开」箭头
            {
                "action": "dismiss",
                "phase": "pre",
                "rules": [
                    {
                        "text": "我已阅读并同意",
                        "expand": [90, 130, 60, 180],
                        "timeout": 50,
                        "settle": 2.5,
                        "optional": True,
                    },
                    {"text": "知道了", "timeout": 15, "settle": 2.5, "optional": True},
                ],
            },
            # 详情页滚动位置残留会让底部按钮坐标偏移，必须回顶
            {"action": "scroll_top", "phase": "pre", "times": 12, "settle": 0.35},
            {"action": "wait", "phase": "pre", "target": "buy_button", "timeout": 25},
            {
                "action": "require",
                "phase": "pre",
                "target": "logged_out_marker",
                "expect": "absent",
                "message": "设备未登录美团，请先在 App 内手动登录",
            },
            {"action": "dump", "phase": "pre", "prefix": "armed"},
            # 到点连点
            {
                "action": "burst",
                "phase": "fire",
                "target": "buy_button",
                "until": "ticket_ready",
                "mode": "auto",
                "window": 8.0,
                "interval_ms": 180,
                "check_every_ms": 350,
                "blockers": ["我已阅读并同意", "知道了"],
            },
            # 抢到后（热路径）
            {
                "action": "loop",
                "phase": "post",
                "until": "order_ready",
                "max_rounds": 3,
                "budget": 12,
                "steps": [
                    {
                        "action": "select_tier",
                        "priority": ["¥680"],
                        "wait": 0.8,
                        "settle": 0.35,
                        "stable_wait": 0.6,
                        "total_budget": 4.0,
                        "optional": True,
                    },
                    {
                        "action": "select_quantity",
                        "plus_target": "qty_plus",
                        "current": 1,
                        "optional": True,
                    },
                    {"action": "tap", "target": "confirm_button", "settle": 1.0, "optional": True},
                ],
            },
            {
                "action": "select_attendees",
                "phase": "post",
                "count": 1,
                "entry": "需选择",
                "confirm": "确认选择",
                "optional": True,
            },
            {
                "action": "tap",
                "phase": "post",
                "target": "pay_button",
                "settle": 1.8,
                "requires_submit": True,
                "optional": True,
            },
            {
                "action": "tap",
                "phase": "post",
                "target": "pay_confirm",
                "settle": 3.0,
                "requires_pay": True,
                "optional": True,
            },
            {
                "action": "stop",
                "phase": "post",
                "message": "已生成订单并进入支付页，请核对金额与观演人信息后手动支付",
            },
        ],
    }


def _damai_recipe(task_name: str, keyword: str) -> Dict[str, Any]:
    """大麦脚手架。

    大麦的控件和美团完全不同，且必须先在 App 里登录 + 完成实名。
    这里给出结构 + 占位选择器，用 `gpiao record` 标定后即可用。
    """
    return {
        "name": task_name,
        "platform": "damai",
        "app": {
            "package": "cn.damai",
            "main_activity": None,
        },
        "concert": {
            "name": task_name,
            "search_keyword": keyword,
            "tier_priority": ["¥680"],
            "quantity": 1,
            "attendees": 1,
        },
        "sale": {
            "at": "2026-01-01 12:00:00",
            "lead_seconds": 0.5,
            "burst_seconds": 8.0,
            "tap_interval_ms": 180,
            "timezone": "+08:00",
        },
        "device": {"connect": None, "serial": None, "auto_prepare": True},
        "stop_before_payment": True,
        "submit_order": True,
        "auto_pay": False,
        "selectors": {
            # TODO 标定：python -m gpiao record -r <recipe> -t <名字>
            "search_entry": {"candidates": [{"descContains": "搜索"}, {"text": "搜索"}]},
            "result_show": {"candidates": [{"textContains": keyword}]},
            "buy_button": {
                "candidates": [
                    {"text": "立即购买"},
                    {"text": "立即购票"},
                    {"textContains": "购买"},
                    {"coord": [0.5, 0.94]},
                ],
                "aliases": ["立即购买", "立即购票", "预约", "即将开售"],
            },
            "ticket_ready": {"candidates": [{"text": "选票"}, {"text": "选择票档"}]},
            "order_ready": {"candidates": [{"text": "提交订单"}, {"textContains": "订单"}]},
            "tier": {"candidates": [{"text": "¥680"}, {"text": "680"}]},
            "qty_plus": {"candidates": [{"descContains": "增加"}, {"text": "+"}]},
            "confirm_button": {"candidates": [{"text": "确认"}, {"text": "提交订单"}]},
            "pay_button": {"candidates": [{"text": "提交订单"}, {"text": "立即支付"}]},
            "logged_out_marker": {"candidates": [{"textContains": "登录"}]},
        },
        "steps": [
            {"action": "wake", "phase": "pre"},
            {"action": "launch", "phase": "pre", "settle": 6.0},
            {"action": "wait", "phase": "pre", "target": "search_entry", "timeout": 20},
            {"action": "tap", "phase": "pre", "target": "search_entry", "settle": 2.0},
            {"action": "search_submit", "phase": "pre", "text": keyword, "settle": 5.0},
            {"action": "wait", "phase": "pre", "target": "result_show", "timeout": 20},
            {"action": "tap", "phase": "pre", "target": "result_show", "settle": 6.0},
            {"action": "scroll_top", "phase": "pre", "times": 10, "settle": 0.3},
            {"action": "wait", "phase": "pre", "target": "buy_button", "timeout": 25},
            {
                "action": "require",
                "phase": "pre",
                "target": "logged_out_marker",
                "expect": "absent",
                "message": "大麦未登录，请先在 App 内登录",
            },
            {
                "action": "burst",
                "phase": "fire",
                "target": "buy_button",
                "until": "ticket_ready",
                "mode": "auto",
                "window": 8.0,
                "interval_ms": 180,
                "check_every_ms": 350,
            },
            {
                "action": "loop",
                "phase": "post",
                "until": "order_ready",
                "max_rounds": 3,
                "budget": 12,
                "steps": [
                    {
                        "action": "select_tier",
                        "priority": ["¥680"],
                        "wait": 0.8,
                        "settle": 0.35,
                        "optional": True,
                    },
                    {
                        "action": "select_quantity",
                        "plus_target": "qty_plus",
                        "current": 1,
                        "optional": True,
                    },
                    {"action": "tap", "target": "confirm_button", "settle": 1.5, "optional": True},
                ],
            },
            {
                "action": "tap",
                "phase": "post",
                "target": "pay_button",
                "settle": 2.0,
                "requires_submit": True,
                "optional": True,
            },
            {"action": "stop", "phase": "post", "message": "已生成订单，请手动完成支付"},
        ],
    }


PLATFORMS = {
    "meituan": _meituan_recipe,
    "damai": _damai_recipe,
}


def build_recipe(platform: str, task_name: str, keyword: str) -> Recipe:
    fn = PLATFORMS.get(platform)
    if fn is None:
        raise ValueError(f"暂不支持的平台: {platform}（可用: {', '.join(PLATFORMS)}）")
    return Recipe.from_dict(fn(task_name, keyword))
