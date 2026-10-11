from __future__ import annotations

from typing import Dict, List
import pandas as pd

from core.models import TargetPosition
from core.strategy import TargetStrategy


class MATrendTargetStrategy(TargetStrategy):
    """
    极简示例策略：只负责输出目标仓位，不直接下单。

    规则：Close > slowMA 且 fastMA > slowMA → target_weight；否则 0。
    它只是验证完整实盘闭环的默认策略，不代表对任何股票的投资建议。
    """
    name = "MA_TREND"

    def targets(self, history: Dict[str, pd.DataFrame]) -> List[TargetPosition]:
        fast = int(self.params.get("fast_ma", 20))
        slow = int(self.params.get("slow_ma", 60))
        weight = float(self.params.get("target_weight", 0.20))
        out: List[TargetPosition] = []
        for symbol, df in history.items():
            signal_date = str(df.index[-1])[:10] if len(df) else ""
            if len(df) < slow:
                audit = {
                    "signal_date": signal_date,
                    "history_bars": int(len(df)),
                    "required_bars": slow,
                }
                out.append(TargetPosition(symbol, 0.0, f"历史不足<{slow}", audit))
                continue
            close = df["close"].astype(float)
            f = float(close.rolling(fast).mean().iloc[-1])
            s = float(close.rolling(slow).mean().iloc[-1])
            c = float(close.iloc[-1])
            audit = {
                "signal_date": signal_date,
                "close": round(c, 6),
                f"ma{fast}": round(f, 6),
                f"ma{slow}": round(s, 6),
            }
            if c > s and f > s:
                out.append(TargetPosition(symbol, weight, f"Close>{slow}MA 且 MA{fast}>MA{slow}", audit))
            else:
                out.append(TargetPosition(symbol, 0.0, "趋势条件不满足", audit))
        return out
