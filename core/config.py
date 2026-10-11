from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
import yaml


class Settings:
    def __init__(self, path: str = "config/settings.yaml"):
        self.path = Path(path)
        with self.path.open("r", encoding="utf-8") as f:
            self.data: Dict[str, Any] = yaml.safe_load(f) or {}

    def section(self, name: str) -> Dict[str, Any]:
        return dict(self.data.get(name, {}) or {})

    def get(self, dotted: str, default=None):
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def require(self, dotted: str):
        value = self.get(dotted)
        if value is None or value == "":
            raise ValueError(f"配置项 {dotted} 不能为空")
        return value
