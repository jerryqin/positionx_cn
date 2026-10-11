from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

import pandas as pd


@dataclass(frozen=True)
class BreakoutRetestConfig:
    lookback: int = 20
    breakout_buffer: float = 0.003
    volume_lookback: int = 20
    min_volume_ratio: float = 1.10
    retest_tolerance: float = 0.01
    retest_upper_buffer: float = 0.005
    max_retest_bars: int = 5
    invalidation_tolerance: float = 0.015


@dataclass(frozen=True)
class LevelHoldConfig:
    confirm_bars: int = 2
    low_tolerance: float = 0.01
    use_volume_filter: bool = False
    volume_lookback: int = 20
    min_volume_ratio: float = 1.0


@dataclass(frozen=True)
class MA60SupportConfig:
    ma60_period: int = 60
    ma200_period: int = 200
    support_band: float = 0.05
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    volume_lookback: int = 20
    volume_ratio: float = 1.30
    min_filters: int = 2
    consecutive_down_days: int = 2
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9


def _frame(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("history must be pandas.DataFrame")
    x = df.copy()
    x.columns = [str(c).lower() for c in x.columns]
    for c in x.columns:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return rsi


def _macd(close: pd.Series, fast: int, slow: int, signal: int):
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def _decline_streak_before_latest(close: pd.Series) -> int:
    if len(close) < 3:
        return 0
    streak = 0
    i = len(close) - 2
    while i >= 1 and float(close.iloc[i]) < float(close.iloc[i - 1]):
        streak += 1
        i -= 1
    return streak


class BreakoutRetestMatcher:
    """Daily-bar matcher using only information available at each historical bar.

    Breakout level:
        prior `lookback` highs only: high.shift(1).rolling(lookback).max()

    Breakout:
        close >= level * (1 + breakout_buffer)
        volume / prior volume-lookback mean >= min_volume_ratio

    After breakout:
        no entry on breakout itself;
        within max_retest_bars, watch for a retest;
        invalid if a close falls below level by invalidation_tolerance.

    Retest hold on latest completed bar:
        low enters the retest zone,
        close recovers to >= breakout level,
        and close is still near the level (upper buffer).
    """

    name = "BREAKOUT_RETEST"

    def __init__(self, config: Optional[BreakoutRetestConfig] = None):
        self.cfg = config or BreakoutRetestConfig()

    def match(self, symbol: str, history: pd.DataFrame) -> Dict[str, Any]:
        x = _frame(history)
        required = {"high", "low", "close", "volume"}
        if not required.issubset(x.columns):
            return self._result(symbol, False, False, "NO_MATCH", "MISSING_COLUMNS")

        x = x.dropna(subset=list(required)).copy()
        min_bars = max(self.cfg.lookback, self.cfg.volume_lookback) + self.cfg.max_retest_bars + 2
        if len(x) < min_bars:
            return self._result(
                symbol, False, False, "NO_MATCH",
                f"INSUFFICIENT_DATA: need>={min_bars}, got={len(x)}"
            )

        level = x["high"].shift(1).rolling(self.cfg.lookback).max()
        prior_vol = x["volume"].shift(1).rolling(self.cfg.volume_lookback).mean()
        vol_ratio = x["volume"] / prior_vol

        breakout = (
            (x["close"] >= level * (1.0 + self.cfg.breakout_buffer))
            & (vol_ratio >= self.cfg.min_volume_ratio)
        )

        latest_pos = len(x) - 1
        start_pos = max(0, latest_pos - self.cfg.max_retest_bars)

        breakout_positions = [
            i for i in range(start_pos, latest_pos + 1)
            if bool(breakout.iloc[i]) and pd.notna(level.iloc[i])
        ]
        if not breakout_positions:
            return self._result(symbol, False, False, "NO_MATCH", "NO_RECENT_BREAKOUT")

        bpos = breakout_positions[-1]
        active_level = float(level.iloc[bpos])
        breakout_close = float(x["close"].iloc[bpos])
        breakout_vr = float(vol_ratio.iloc[bpos])
        bars_since = latest_pos - bpos

        evidence = {
            "breakout_position": int(bpos),
            "breakout_date": str(x.index[bpos]),
            "latest_date": str(x.index[-1]),
            "bars_since_breakout": int(bars_since),
            "active_breakout_level": round(active_level, 6),
            "breakout_close": round(breakout_close, 6),
            "breakout_volume_ratio": round(breakout_vr, 4),
            "latest_close": round(float(x["close"].iloc[-1]), 6),
            "latest_low": round(float(x["low"].iloc[-1]), 6),
        }

        if bars_since == 0:
            return self._result(
                symbol, True, False, "ARMED",
                "BREAKOUT_CONFIRMED_WAIT_RETEST", evidence
            )

        post = x.iloc[bpos + 1 : latest_pos + 1]
        invalid_line = active_level * (1.0 - self.cfg.invalidation_tolerance)
        if bool((post["close"] < invalid_line).any()):
            evidence["invalidation_line"] = round(invalid_line, 6)
            return self._result(
                symbol, False, False, "BREAKOUT_FAILED",
                "CLOSE_BELOW_INVALIDATION_LINE", evidence
            )

        latest_low = float(x["low"].iloc[-1])
        latest_close = float(x["close"].iloc[-1])
        touched = latest_low <= active_level * (1.0 + self.cfg.retest_tolerance)
        held = latest_close >= active_level
        near = latest_close <= active_level * (1.0 + self.cfg.retest_upper_buffer)

        evidence.update({
            "retest_touch": bool(touched),
            "retest_hold": bool(held),
            "retest_near_level": bool(near),
        })

        if touched and held and near:
            return self._result(
                symbol, True, True, "RETEST_HOLD",
                "BREAKOUT_RETEST_CONFIRMED", evidence
            )

        return self._result(
            symbol, True, False, "ARMED",
            "BREAKOUT_VALID_WAIT_RETEST", evidence
        )

    def _result(
        self,
        symbol: str,
        matched: bool,
        triggered: bool,
        state: str,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "strategy": self.name,
            "matched": bool(matched),
            "triggered": bool(triggered),
            "state": state,
            "reason": reason,
            "evidence": evidence or {},
            "parameters": asdict(self.cfg),
        }


class MA60SupportMatcher:
    """Research-side MA60 support setup matcher."""

    name = "MA60_SUPPORT"

    def __init__(self, config: Optional[MA60SupportConfig] = None):
        self.cfg = config or MA60SupportConfig()

    def match(self, symbol: str, history: pd.DataFrame) -> Dict[str, Any]:
        x = _frame(history)
        required = {"low", "close", "volume"}
        if not required.issubset(x.columns):
            return self._result(symbol, False, False, "NO_MATCH", "MISSING_COLUMNS")

        x = x.dropna(subset=list(required)).copy()
        need = max(
            self.cfg.ma200_period,
            self.cfg.volume_lookback + 1,
            self.cfg.rsi_period + 2,
            self.cfg.macd_slow + self.cfg.macd_signal + 2,
        )
        if len(x) < need:
            return self._result(
                symbol, False, False, "NO_MATCH",
                f"INSUFFICIENT_DATA: need>={need}, got={len(x)}"
            )

        close = x["close"]
        volume = x["volume"]
        ma60 = close.rolling(self.cfg.ma60_period).mean()
        ma200 = close.rolling(self.cfg.ma200_period).mean()
        rsi = _rsi(close, self.cfg.rsi_period)
        dif, dea, hist = _macd(
            close, self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal
        )

        latest_close = float(close.iloc[-1])
        latest_low = float(x["low"].iloc[-1])
        latest_ma60 = float(ma60.iloc[-1])
        latest_ma200 = float(ma200.iloc[-1])
        latest_rsi = float(rsi.iloc[-1])

        trend_ok = bool(latest_close > latest_ma60 and latest_close > latest_ma200)
        support_zone = bool(
            latest_close >= latest_ma60
            and latest_close <= latest_ma60 * (1.0 + self.cfg.support_band)
        )
        wick_reclaim = bool(latest_low <= latest_ma60 and latest_close >= latest_ma60)
        rsi_confirm = bool(latest_rsi < self.cfg.rsi_oversold)
        stop_confirmed = bool(wick_reclaim or rsi_confirm)

        prior_volume = float(volume.iloc[-self.cfg.volume_lookback - 1:-1].mean())
        latest_volume = float(volume.iloc[-1])
        volume_ratio = latest_volume / prior_volume if prior_volume > 0 else 0.0
        volume_filter = bool(volume_ratio > self.cfg.volume_ratio)

        latest_dif = float(dif.iloc[-1])
        latest_dea = float(dea.iloc[-1])
        latest_hist = float(hist.iloc[-1])
        prev_hist = float(hist.iloc[-2])
        macd_filter = bool(latest_hist > prev_hist or latest_dif > latest_dea)

        decline_streak = _decline_streak_before_latest(close)
        decline_filter = bool(decline_streak >= self.cfg.consecutive_down_days)

        filters = {
            "volume_expansion": volume_filter,
            "macd_momentum": macd_filter,
            "consecutive_decline": decline_filter,
        }
        filter_count = sum(1 for v in filters.values() if v)
        filters_ok = bool(filter_count >= self.cfg.min_filters)

        evidence = {
            "latest_date": str(x.index[-1]),
            "latest_close": round(latest_close, 6),
            "latest_low": round(latest_low, 6),
            "ma60": round(latest_ma60, 6),
            "ma200": round(latest_ma200, 6),
            "distance_to_ma60": round(latest_close / latest_ma60 - 1.0, 6),
            "trend_ok": trend_ok,
            "support_zone": support_zone,
            "wick_reclaim": wick_reclaim,
            "rsi": round(latest_rsi, 4),
            "rsi_confirm": rsi_confirm,
            "stop_confirmed": stop_confirmed,
            "volume_ratio": round(volume_ratio, 4),
            "macd_dif": round(latest_dif, 6),
            "macd_dea": round(latest_dea, 6),
            "macd_hist": round(latest_hist, 6),
            "macd_hist_prev": round(prev_hist, 6),
            "decline_streak_before_latest": int(decline_streak),
            "filters": filters,
            "filter_count": int(filter_count),
            "filters_ok": filters_ok,
        }

        if not trend_ok:
            return self._result(symbol, False, False, "NO_MATCH", "BIG_TREND_FAIL", evidence)
        if not support_zone:
            return self._result(symbol, False, False, "NO_MATCH", "OUTSIDE_MA60_SUPPORT_ZONE", evidence)
        if stop_confirmed and filters_ok:
            return self._result(
                symbol, True, True, "SUPPORT_CONFIRMED",
                "MA60_SUPPORT_CONFIRMED", evidence
            )
        if not stop_confirmed:
            return self._result(
                symbol, True, False, "WATCH_SUPPORT",
                "MA60_ZONE_WAIT_STOP_CONFIRMATION", evidence
            )
        return self._result(
            symbol, True, False, "WATCH_SUPPORT",
            "STOP_CONFIRMED_WAIT_FILTERS", evidence
        )

    def _result(
        self, symbol: str, matched: bool, triggered: bool, state: str, reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "strategy": self.name,
            "matched": bool(matched),
            "triggered": bool(triggered),
            "state": state,
            "reason": reason,
            "evidence": evidence or {},
            "parameters": asdict(self.cfg),
        }


class LevelHoldMatcher:
    """Daily-bar hold confirmation around an explicit user/system level.

    The matcher never invents a level. A caller must supply it.
    """

    name = "LEVEL_HOLD"

    def __init__(self, config: Optional[LevelHoldConfig] = None):
        self.cfg = config or LevelHoldConfig()

    def match(self, symbol: str, history: pd.DataFrame, level: float) -> Dict[str, Any]:
        level = float(level)
        if level <= 0:
            raise ValueError("level must be > 0")

        x = _frame(history)
        required = {"low", "close", "volume"}
        if not required.issubset(x.columns):
            return self._result(symbol, level, False, False, "NO_MATCH", "MISSING_COLUMNS")

        x = x.dropna(subset=list(required)).copy()
        need = max(self.cfg.confirm_bars, self.cfg.volume_lookback + 1)
        if len(x) < need:
            return self._result(
                symbol, level, False, False, "NO_MATCH",
                f"INSUFFICIENT_DATA: need>={need}, got={len(x)}"
            )

        closes = x["close"].tail(self.cfg.confirm_bars)
        closes_hold = bool((closes > level).all())
        latest_low = float(x["low"].iloc[-1])
        low_hold = latest_low >= level * (1.0 - self.cfg.low_tolerance)

        volume_ratio = None
        volume_pass = True
        if self.cfg.use_volume_filter:
            prior = float(x["volume"].iloc[-self.cfg.volume_lookback - 1 : -1].mean())
            latest_volume = float(x["volume"].iloc[-1])
            volume_ratio = latest_volume / prior if prior > 0 else 0.0
            volume_pass = volume_ratio >= self.cfg.min_volume_ratio

        evidence = {
            "level": round(level, 6),
            "confirm_bars": self.cfg.confirm_bars,
            "confirm_closes": [round(float(v), 6) for v in closes.tolist()],
            "latest_low": round(latest_low, 6),
            "closes_hold": closes_hold,
            "low_hold": low_hold,
            "volume_filter_enabled": self.cfg.use_volume_filter,
            "volume_ratio": round(volume_ratio, 4) if volume_ratio is not None else None,
            "volume_pass": bool(volume_pass),
        }

        ok = closes_hold and low_hold and volume_pass
        if ok:
            return self._result(
                symbol, level, True, True, "HOLD_CONFIRMED",
                "LEVEL_HOLD_CONFIRMED", evidence
            )

        reasons = []
        if not closes_hold:
            reasons.append("CLOSE_CONFIRM_FAIL")
        if not low_hold:
            reasons.append("LOW_TOLERANCE_FAIL")
        if not volume_pass:
            reasons.append("VOLUME_FILTER_FAIL")
        return self._result(
            symbol, level, False, False, "NO_MATCH",
            "|".join(reasons) or "LEVEL_HOLD_NOT_CONFIRMED", evidence
        )

    def _result(
        self,
        symbol: str,
        level: float,
        matched: bool,
        triggered: bool,
        state: str,
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "strategy": self.name,
            "matched": bool(matched),
            "triggered": bool(triggered),
            "state": state,
            "reason": reason,
            "evidence": evidence or {"level": level},
            "parameters": asdict(self.cfg),
        }
