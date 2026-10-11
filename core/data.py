from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional
import math
import pandas as pd

from .db import Database


class MarketDataError(RuntimeError):
    pass


class MarketDataService:
    def __init__(self, db: Database):
        self.db = db
        self._xtdata = None

    def connect(self):
        if self._xtdata is not None:
            return
        try:
            from xtquant import xtdata
        except ImportError as e:
            raise MarketDataError("无法导入 xtquant。请在安装了 miniQMT/xtquant 的 Windows Python 环境运行。") from e
        self._xtdata = xtdata
        # 新版通常自动连接；老版 connect() 仍可用。存在则调用。
        if hasattr(xtdata, "connect"):
            try:
                xtdata.connect()
            except Exception:
                pass

    @staticmethod
    def _normalize_index(v) -> str:
        # xtdata 不同版本的 index 可能是 YYYYMMDD 字符串、Timestamp 或 epoch 时间戳。
        if isinstance(v, (int, float)):
            x = float(v)
            if x > 1e12:
                return pd.to_datetime(x, unit="ms").strftime("%Y-%m-%d")
            if x > 1e9:
                return pd.to_datetime(x, unit="s").strftime("%Y-%m-%d")
        s = str(v)
        if len(s) >= 8 and s[:8].isdigit() and s[4:6] in {f"{m:02d}" for m in range(1,13)}:
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        try:
            ts = pd.to_datetime(v)
            return ts.strftime("%Y-%m-%d")
        except Exception:
            return s[:10]

    def update_daily(self, symbol: str, start: str = "", end: str = "") -> pd.DataFrame:
        self.connect()
        xtdata = self._xtdata
        start_raw = start.replace("-", "")
        end_raw = end.replace("-", "")
        try:
            xtdata.download_history_data(symbol, "1d", start_time=start_raw, end_time=end_raw)
        except TypeError:
            xtdata.download_history_data(symbol, "1d", start_raw, end_raw)
        data = xtdata.get_market_data_ex(
            ["open", "high", "low", "close", "volume"], [symbol], "1d",
            start_time=start_raw, end_time=end_raw, count=-1, dividend_type="front"
        )
        if not data or symbol not in data or data[symbol] is None or len(data[symbol]) == 0:
            raise MarketDataError(f"{symbol} 日线数据为空")
        df = data[symbol].copy()
        required = ["open", "high", "low", "close", "volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            raise MarketDataError(f"{symbol} 缺少行情字段: {missing}")
        rows = []
        for idx, r in df.iterrows():
            vals = {k: float(r[k]) for k in required}
            if any(not math.isfinite(x) for x in vals.values()) or vals["close"] <= 0:
                continue
            rows.append({"trade_date": self._normalize_index(idx), **vals})
        if not rows:
            raise MarketDataError(f"{symbol} 没有有效日线")
        self.db.save_daily_bars(symbol, rows)
        return pd.DataFrame(rows).set_index("trade_date")

    def history(self, symbol: str, bars: int = 260, refresh: bool = False) -> pd.DataFrame:
        rows = self.db.load_daily_bars(symbol, bars)
        if refresh or len(rows) < bars:
            self.update_daily(symbol)
            rows = self.db.load_daily_bars(symbol, bars)
        if not rows:
            raise MarketDataError(f"{symbol} 本地没有日线")
        return pd.DataFrame(rows).set_index("trade_date")[["open","high","low","close","volume"]]

    @staticmethod
    def _latest_scalar(obj, symbol: str):
        """Best-effort extraction from xtdata get_full_kline field frames.

        Official xtdata documents get_full_kline as ``{field: DataFrame}``, but
        different client builds can orient the stock code on rows or columns.
        This helper accepts both shapes and deliberately returns only the most
        recent non-null scalar.
        """
        if obj is None:
            return None
        try:
            if isinstance(obj, pd.DataFrame):
                if symbol in obj.index:
                    row = obj.loc[symbol]
                    if isinstance(row, pd.DataFrame):
                        row = row.iloc[-1]
                    vals = pd.Series(row).dropna()
                    return vals.iloc[-1] if len(vals) else None
                if symbol in obj.columns:
                    vals = obj[symbol].dropna()
                    return vals.iloc[-1] if len(vals) else None
                vals = obj.stack().dropna()
                return vals.iloc[-1] if len(vals) else None
            if isinstance(obj, pd.Series):
                vals = obj.dropna()
                return vals.iloc[-1] if len(vals) else None
            if isinstance(obj, dict):
                if symbol in obj:
                    return MarketDataService._latest_scalar(obj[symbol], symbol)
                vals = [v for v in obj.values() if v is not None]
                return vals[-1] if vals else None
            if isinstance(obj, (list, tuple)):
                return obj[-1] if obj else None
            return obj
        except Exception:
            return None

    def latest_full_kline_bar(self, symbol: str) -> Optional[Dict]:
        """Read the latest trading day's 1d bar from xtdata full-kline data.

        This is a narrow fallback for the post-close gap where the historical
        download cache may still end at yesterday even though MiniQMT already
        exposes today's completed daily bar as latest-trading-day full-kline
        data.  It never fabricates a bar from intraday prices.
        """
        self.connect()
        xtdata = self._xtdata
        if not hasattr(xtdata, "get_full_kline"):
            return None
        fields = ["time", "open", "high", "low", "close", "volume"]
        try:
            raw = xtdata.get_full_kline(
                field_list=fields, stock_list=[symbol], period="1d",
                start_time="", end_time="", count=1,
                dividend_type="front", fill_data=True,
            )
        except TypeError:
            # Compatibility with builds that only accept positional args.
            raw = xtdata.get_full_kline(fields, [symbol], "1d", "", "", 1, "front", True)
        if not raw or not isinstance(raw, dict):
            return None

        vals = {f: self._latest_scalar(raw.get(f), symbol) for f in fields}
        required = ["open", "high", "low", "close", "volume"]
        try:
            nums = {k: float(vals[k]) for k in required}
        except (TypeError, ValueError, KeyError):
            return None
        if any(not math.isfinite(v) for v in nums.values()) or nums["close"] <= 0:
            return None

        trade_date = None
        if vals.get("time") is not None:
            trade_date = self._normalize_index(vals["time"])
        if not trade_date or len(str(trade_date)) < 10:
            try:
                tick = self.full_tick(symbol)
                raw_time = tick.get("time") or tick.get("timestamp") or tick.get("timetag")
                if raw_time is not None:
                    trade_date = self._normalize_index(raw_time)
            except Exception:
                return None
        return {"trade_date": str(trade_date)[:10], **nums}

    @staticmethod
    def _tick_datetime(tick: Dict) -> Optional[datetime]:
        """Best-effort local wall-clock time carried by a full-tick snapshot."""
        tag = tick.get("timetag")
        if tag:
            s = str(tag).strip()
            for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d%H%M%S"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    pass
        raw = tick.get("time") or tick.get("timestamp")
        if raw is None:
            return None
        try:
            x = float(raw)
            if x > 1e12:
                x /= 1000.0
            return datetime.fromtimestamp(x)
        except Exception:
            return None

    def latest_close_snapshot_bar(self, symbol: str, trade_date: str) -> Optional[Dict]:
        """Build the just-finished daily bar from the exchange close snapshot.

        This fallback is intentionally narrow.  It is used only after the
        historical 1d cache and get_full_kline are unavailable.  A snapshot is
        accepted only when it belongs to *trade_date*, is timestamped at/after
        15:00 local time, and the exchange/security state says the continuous
        session has ended (15=closed, 22/23=post-close fixed-price states).

        For front-adjusted series the latest trading day's price scale is the
        live/raw scale; later official history downloads will upsert/replace
        this row with MiniQMT's canonical daily bar.
        """
        try:
            tick = self.full_tick(symbol)
        except Exception:
            return None

        tick_dt = self._tick_datetime(tick)
        if tick_dt is None or tick_dt.date().isoformat() != trade_date:
            return None
        if tick_dt.strftime("%H:%M:%S") < "15:00:00":
            return None

        state = self.security_state(tick)
        if state not in {15, 22, 23}:
            return None

        raw_values = {
            "open": tick.get("open"),
            "high": tick.get("high"),
            "low": tick.get("low"),
            "close": tick.get("lastPrice") if tick.get("lastPrice") is not None else tick.get("last_price"),
            "volume": tick.get("volume"),
        }
        try:
            vals = {k: float(v) for k, v in raw_values.items()}
        except (TypeError, ValueError):
            return None
        if any(not math.isfinite(v) for v in vals.values()):
            return None
        if vals["open"] <= 0 or vals["high"] <= 0 or vals["low"] <= 0 or vals["close"] <= 0 or vals["volume"] < 0:
            return None
        if vals["high"] < max(vals["open"], vals["low"], vals["close"]):
            return None
        if vals["low"] > min(vals["open"], vals["high"], vals["close"]):
            return None
        return {"trade_date": trade_date, **vals, "_snapshot_state": state, "_snapshot_time": tick_dt.strftime("%Y-%m-%d %H:%M:%S")}

    def refresh_completed_daily_bar(self, symbol: str, trade_date: str, now: Optional[datetime] = None) -> Dict:
        """Ensure the requested completed daily bar is present locally.

        Source priority:
        1) normal historical download/cache;
        2) after the close, xtdata.get_full_kline() for the latest trading day;
        3) strict exchange close snapshot (full_tick) when this MiniQMT build
           cannot publish same-day daily/minute bars yet.

        Returns a structured status instead of treating normal post-close data
        publication lag as a program failure.
        """
        now = now or datetime.now()
        status = {
            "symbol": symbol,
            "requested_date": trade_date,
            "ok": False,
            "source": None,
            "latest_date": None,
        }
        try:
            self.update_daily(symbol)
        except Exception as e:
            status["history_error"] = str(e)

        rows = self.db.load_daily_bars(symbol, 1)
        latest = str(rows[-1].get("trade_date", ""))[:10] if rows else None
        status["latest_date"] = latest
        if latest == trade_date:
            status["ok"] = True
            status["source"] = "HISTORY_CACHE"
            return status

        # Never use the latest-day fallback for today's still-open session.
        if trade_date == now.date().isoformat() and now.strftime("%H:%M") < "15:00":
            status["reason"] = "TODAY_NOT_CLOSED"
            return status

        try:
            row = self.latest_full_kline_bar(symbol)
        except Exception as e:
            status["full_kline_error"] = str(e)
            row = None
        if row is not None:
            status["full_kline_date"] = str(row.get("trade_date", ""))[:10]
            if status["full_kline_date"] == trade_date:
                self.db.save_daily_bars(symbol, [row])
                status["ok"] = True
                status["source"] = "FULL_KLINE"
                status["latest_date"] = trade_date
                return status

        # Some MiniQMT builds expose neither same-day 1d history nor
        # get_full_kline.  The full-tick close snapshot still carries the
        # exchange's completed OHLCV.  Accept it only under strict close-state
        # validation; never synthesize from an intraday/continuous snapshot.
        try:
            row = self.latest_close_snapshot_bar(symbol, trade_date)
        except Exception as e:
            status["close_snapshot_error"] = str(e)
            row = None
        if row is not None:
            self.db.save_daily_bars(symbol, [row])
            status["ok"] = True
            status["source"] = "CLOSE_SNAPSHOT"
            status["latest_date"] = trade_date
            status["snapshot_state"] = row.get("_snapshot_state")
            status["snapshot_time"] = row.get("_snapshot_time")
            return status

        status["reason"] = "DAILY_BAR_NOT_READY"
        return status


    def instrument_detail(self, symbol: str) -> Dict:
        self.connect()
        if not hasattr(self._xtdata, "get_instrument_detail"):
            return {}
        try:
            return self._xtdata.get_instrument_detail(symbol, False) or {}
        except TypeError:
            return self._xtdata.get_instrument_detail(symbol) or {}

    def price_tick(self, symbol: str, default: float = 0.01) -> float:
        d = self.instrument_detail(symbol)
        try:
            v = float(d.get("PriceTick", 0) or 0)
            return v if v > 0 else default
        except Exception:
            return default

    def min_limit_order_volume(self, symbol: str, default: int = 100) -> int:
        d = self.instrument_detail(symbol)
        try:
            v = int(d.get("MinLimitOrderVolume", 0) or 0)
            return max(default, v) if v > 0 else default
        except Exception:
            return default

    def price_limits(self, symbol: str):
        d = self.instrument_detail(symbol)
        try: up = float(d.get("UpStopPrice", 0) or 0)
        except Exception: up = 0.0
        try: down = float(d.get("DownStopPrice", 0) or 0)
        except Exception: down = 0.0
        return down, up

    def ensure_tradable(self, symbol: str):
        """Static suspension check for A-share cash equities.

        ``get_instrument_detail()`` may report ``IsTrading=False`` even for
        ordinary A-share stocks that are actively trading.  The official
        stock dictionary defines ``InstrumentStatus <= 0`` as normal trading
        (``-1`` = resumed) and ``>= 1`` as suspended for N days.  Therefore
        PositionX does not use ``IsTrading`` as a hard gate for A-shares.

        Real-time executability is still fail-closed in ``_limit_price`` via
        a fresh full-tick snapshot and ``openInt/openInterest == 13``.
        """
        d = self.instrument_detail(symbol)
        if not d:
            return
        raw = d.get("InstrumentStatus")
        if raw is None:
            # No reliable static status: defer to the stricter real-time
            # full-tick/openInt check in ExecutionEngine.
            return
        try:
            status = int(raw)
        except (TypeError, ValueError):
            raise MarketDataError(f"{symbol} InstrumentStatus 无法解析: {raw!r}")
        if status >= 1:
            raise MarketDataError(f"{symbol} 当前停牌/不可交易，InstrumentStatus={status}")

    def full_tick(self, symbol: str) -> Dict:
        self.connect()
        data = self._xtdata.get_full_tick([symbol])
        if not data or symbol not in data:
            raise MarketDataError(f"{symbol} 无最新快照")
        return data[symbol]

    @staticmethod
    def tick_price(tick: Dict, side: str) -> float:
        def first_number(v):
            if isinstance(v, (list, tuple)) and v:
                return float(v[0] or 0)
            try:
                # numpy array / Series
                if hasattr(v, "__len__") and not isinstance(v, (str, bytes)) and len(v):
                    return float(v[0] or 0)
            except Exception:
                pass
            return 0.0
        ask = first_number(tick.get("askPrice") or tick.get("ask_price") or [])
        bid = first_number(tick.get("bidPrice") or tick.get("bid_price") or [])
        last = float(tick.get("lastPrice") or tick.get("last_price") or tick.get("price") or 0)
        p = ask if side.upper() == "BUY" and ask > 0 else bid if side.upper() == "SELL" and bid > 0 else last
        if p <= 0:
            raise MarketDataError("快照没有有效买卖盘/最新价")
        return p

    def is_tick_fresh(self, tick: Dict, max_age_seconds: int) -> bool:
        raw = tick.get("time") or tick.get("timestamp") or tick.get("timetag")
        if raw is None:
            # 某些 xtdata 版本 full_tick 不给可靠时间；不凭空判过期，由连接状态兜底。
            return True
        try:
            x = float(raw)
            if x > 1e12:
                x /= 1000.0
            return (datetime.now().timestamp() - x) <= max_age_seconds
        except Exception:
            return True


    def has_completed_daily_bar(self, symbol: str, trade_date: str, refresh: bool = True) -> bool:
        """Whether *trade_date* has a completed daily bar.

        The check avoids the optional trading-calendar API.  When refresh is
        requested it also handles the normal post-close lag between latest-day
        full-kline data and the historical download cache.
        """
        if refresh:
            return bool(self.refresh_completed_daily_bar(symbol, trade_date).get("ok"))
        rows = self.db.load_daily_bars(symbol, 3)
        return bool(rows and str(rows[-1].get("trade_date", ""))[:10] == trade_date)

    @staticmethod
    def security_state(tick: Dict) -> Optional[int]:
        """Return the stock trading-state code from a full-tick snapshot.

        For A-share stocks miniQMT exposes the exchange/security state in
        ``openInt`` (some builds use ``openInterest``).  ``stockStatus`` is
        not the same code system and must not be compared with 13.
        """
        raw = tick.get("openInt")
        if raw is None:
            raw = tick.get("openInterest")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def is_live_session(self, symbol: str, max_age_seconds: int = 30, now: Optional[datetime] = None) -> bool:
        """Fail-closed check that the A-share continuous session is actually live.

        We do not predict holidays. A plan becomes executable only in the
        first subsequent continuous session confirmed by a fresh miniQMT tick.
        For stocks, openInt/openInterest=13 means continuous trading.
        """
        now = now or datetime.now()
        hhmm = now.strftime("%H:%M")
        if not ("09:30" <= hhmm <= "11:30" or "13:00" <= hhmm <= "15:00"):
            return False
        try:
            tick = self.full_tick(symbol)
        except Exception:
            return False
        raw = tick.get("time") or tick.get("timestamp") or tick.get("timetag")
        if raw is None:
            return False
        if not self.is_tick_fresh(tick, max_age_seconds):
            return False
        state = self.security_state(tick)
        # Fail closed: without a reliable stock-state field we do not infer
        # that the exchange is in continuous trading merely from clock time.
        if state != 13:
            return False
        return True
