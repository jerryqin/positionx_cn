from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
import json

import pandas as pd


@dataclass(frozen=True)
class AdapterConfig:
    sector: str = "沪深300"
    bars: int = 80
    dividend_type: str = "front"


class XtDataWatchlistAdapter:
    """Thin read-only adapter from xtdata to WatchlistEngine input."""

    FIELDS = ["open", "high", "low", "close", "volume", "amount"]

    def __init__(self, xtdata: Any, config: Optional[AdapterConfig] = None):
        self.xtdata = xtdata
        self.cfg = config or AdapterConfig()

    def universe(self, *, refresh_sector: bool = False) -> List[str]:
        if refresh_sector:
            self.xtdata.download_sector_data()
        rows = self.xtdata.get_stock_list_in_sector(self.cfg.sector) or []
        # Keep only ordinary SH/SZ-style codes at this stage.
        return sorted({str(s) for s in rows if str(s).endswith((".SH", ".SZ"))})

    @staticmethod
    def merge_holdings(universe: Iterable[str], holdings: Iterable[str]) -> List[str]:
        return sorted(set(map(str, universe)) | set(map(str, holdings)))

    def refresh_history(self, symbols: Sequence[str], calendar_days: int = 240) -> None:
        symbols = list(dict.fromkeys(map(str, symbols)))
        if not symbols:
            return

        calendar_days = int(calendar_days)
        if calendar_days <= 0:
            raise ValueError("calendar_days must be > 0")
        start_time = (datetime.now() - timedelta(days=calendar_days)).strftime("%Y%m%d")

        def on_progress(data):
            if not isinstance(data, dict):
                print(f"[WATCHLIST DATA] {data}", flush=True)
                return
            total = data.get("total", "?")
            finished = data.get("finished", "?")
            stockcode = data.get("stockcode", "")
            message = data.get("message", "")
            print(
                f"[WATCHLIST DATA] {finished}/{total} {stockcode} {message}".rstrip(),
                flush=True,
            )

        fn = getattr(self.xtdata, "download_history_data2", None)
        if callable(fn):
            fn(symbols, "1d", start_time, "", on_progress, None)
            return

        total = len(symbols)
        for i, symbol in enumerate(symbols, start=1):
            print(f"[WATCHLIST DATA] {i}/{total} {symbol}", flush=True)
            self.xtdata.download_history_data(symbol, "1d", start_time, "", None)

    def history(self, symbols: Sequence[str]) -> Dict[str, pd.DataFrame]:
        symbols = list(dict.fromkeys(map(str, symbols)))
        if not symbols:
            return {}
        raw = self.xtdata.get_market_data(
            field_list=self.FIELDS,
            stock_list=symbols,
            period="1d",
            start_time="",
            end_time="",
            count=int(self.cfg.bars),
            dividend_type=self.cfg.dividend_type,
            fill_data=True,
        ) or {}
        return self._parse_market_data(raw, symbols)

    @classmethod
    def _parse_market_data(
        cls, raw: Mapping[str, Any], symbols: Sequence[str]
    ) -> Dict[str, pd.DataFrame]:
        out: Dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            cols: Dict[str, pd.Series] = {}
            index = None
            for field in cls.FIELDS:
                obj = raw.get(field)
                if not isinstance(obj, pd.DataFrame) or symbol not in obj.index:
                    continue
                row = obj.loc[symbol]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[-1]
                series = pd.to_numeric(pd.Series(row), errors="coerce")
                if index is None:
                    index = series.index
                cols[field] = series
            if "close" not in cols or "volume" not in cols:
                continue
            df = pd.DataFrame(cols)
            df = df.dropna(subset=["close", "volume"])
            if not df.empty:
                out[symbol] = df
        return out


def connected_xtdata(market):
    """Return xtdata after explicitly initializing MarketDataService."""
    market.connect()
    xtdata = getattr(market, "_xtdata", None)
    if xtdata is None:
        raise RuntimeError(
            "MarketDataService.connect() completed but xtdata is still unavailable"
        )
    return xtdata

def save_watchlist_snapshot(path: Path, payload: Dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path
