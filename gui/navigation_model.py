from __future__ import annotations

NAV_ITEMS = (
    {"key": "dashboard", "label": "Dashboard", "group": "OVERVIEW", "enabled": True},
    {"key": "strategies", "label": "Strategies", "group": "RESEARCH", "enabled": False},
    {"key": "forward", "label": "Forward", "group": "RESEARCH", "enabled": True},
    {"key": "portfolio", "label": "Portfolio", "group": "PORTFOLIO", "enabled": False},
    {"key": "execution", "label": "Execution", "group": "TRADING", "enabled": True},
    {"key": "system", "label": "System / Logs", "group": "SYSTEM", "enabled": False},
)

DEFAULT_PAGE = "dashboard"


def nav_keys():
    return [x["key"] for x in NAV_ITEMS]


def enabled_nav_keys():
    return [x["key"] for x in NAV_ITEMS if x["enabled"]]
