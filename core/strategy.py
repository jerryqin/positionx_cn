from __future__ import annotations

from abc import ABC, abstractmethod
from importlib import import_module
from typing import Dict, List
import pandas as pd

from .models import TargetPosition


class TargetStrategy(ABC):
    name = "base"

    def __init__(self, params: Dict):
        self.params = params

    @abstractmethod
    def targets(self, history: Dict[str, pd.DataFrame]) -> List[TargetPosition]:
        raise NotImplementedError


def load_strategy(path: str, params: Dict) -> TargetStrategy:
    module_name, class_name = path.split(":", 1)
    cls = getattr(import_module(module_name), class_name)
    obj = cls(params)
    if not isinstance(obj, TargetStrategy):
        raise TypeError(f"{path} 必须继承 TargetStrategy")
    return obj
