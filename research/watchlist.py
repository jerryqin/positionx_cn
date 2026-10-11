from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set
import math
import pandas as pd


@dataclass(frozen=True)
class WatchlistConfig:
    min_bars: int = 60
    top_n: int = 20
    min_price: float = 2.0
    min_avg_amount20: float = 50_000_000.0
    min_ret20: float = 0.0
    near_high20_ratio: float = 0.90
    require_trend: bool = True


class WatchlistEngine:
    """Pure research-side watchlist screen. Never creates orders or trading plans."""

    def __init__(self, config: Optional[WatchlistConfig] = None):
        self.cfg = config or WatchlistConfig()

    @staticmethod
    def _frame(df: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(df, pd.DataFrame):
            raise TypeError("history value must be pandas.DataFrame")
        x = df.copy()
        x.columns = [str(c).lower() for c in x.columns]
        return x

    def _metrics(self, df: pd.DataFrame) -> Dict[str, Any]:
        x = self._frame(df)
        if not {"close", "volume"}.issubset(x.columns):
            return {"ok": False, "error": "MISSING_COLUMNS"}

        cols = ["close", "volume"] + (["amount"] if "amount" in x.columns else [])
        x = x[cols].copy()
        for c in cols:
            x[c] = pd.to_numeric(x[c], errors="coerce")
        x = x.dropna(subset=["close", "volume"])

        if len(x) < self.cfg.min_bars:
            return {"ok": False, "error": "INSUFFICIENT_DATA", "bars": int(len(x))}

        close = x["close"]
        volume = x["volume"]
        last_close = float(close.iloc[-1])
        if last_close <= 0:
            return {"ok": False, "error": "INVALID_CLOSE"}

        ma20 = float(close.tail(20).mean())
        ma60 = float(close.tail(60).mean())
        ret20 = float(last_close / float(close.iloc[-21]) - 1.0)
        high20 = float(close.tail(20).max())
        near_high20 = float(last_close / high20) if high20 > 0 else 0.0

        if "amount" in x.columns and x["amount"].tail(20).notna().any():
            amount = x["amount"].fillna(close * volume)
            amount_source = "amount"
        else:
            amount = close * volume
            amount_source = "close_x_volume_proxy"

        avg_amount20 = float(amount.tail(20).mean())
        avg_volume20 = float(volume.tail(20).mean())
        volume_ratio20 = float(volume.iloc[-1] / avg_volume20) if avg_volume20 > 0 else 0.0

        trend_pass = bool(last_close > ma20 > ma60)
        price_pass = bool(last_close >= self.cfg.min_price)
        momentum_pass = bool(ret20 >= self.cfg.min_ret20)
        near_high_pass = bool(near_high20 >= self.cfg.near_high20_ratio)
        liquidity_pass = bool(avg_amount20 >= self.cfg.min_avg_amount20)

        # Broad research candidate gate: intentionally strategy-agnostic.
        # Trend/momentum/near-high remain focus-screen preferences, not blockers
        # for downstream Strategy Matching.
        candidate_eligible = bool(price_pass and liquidity_pass)

        eligible = (
            price_pass
            and momentum_pass
            and near_high_pass
            and liquidity_pass
            and (trend_pass if self.cfg.require_trend else True)
        )

        return {
            "ok": True,
            "close": last_close,
            "ma20": ma20,
            "ma60": ma60,
            "ret20": ret20,
            "high20": high20,
            "near_high20": near_high20,
            "avg_amount20": avg_amount20,
            "amount_source": amount_source,
            "volume_ratio20": volume_ratio20,
            "trend_pass": trend_pass,
            "price_pass": price_pass,
            "momentum_pass": momentum_pass,
            "near_high_pass": near_high_pass,
            "liquidity_pass": liquidity_pass,
            "candidate_eligible": candidate_eligible,
            "eligible": eligible,
        }

    @staticmethod
    def _percentile_rank(values: Mapping[str, float]) -> Dict[str, float]:
        if not values:
            return {}
        s = pd.Series(values, dtype=float)
        return {str(k): float(v) for k, v in s.rank(method="average", pct=True).to_dict().items()}

    def build(
        self,
        history: Mapping[str, pd.DataFrame],
        *,
        holdings: Iterable[str] = (),
        pinned: Iterable[str] = (),
        names: Optional[Mapping[str, str]] = None,
        sectors: Optional[Mapping[str, str]] = None,
        ranking_universe: Optional[Iterable[str]] = None,
        mode: str = "watchlist",
    ) -> List[Dict[str, Any]]:
        if mode not in {"watchlist", "candidate_pool"}:
            raise ValueError(f"unknown watchlist build mode: {mode}")

        holdings_set: Set[str] = {str(s) for s in holdings}
        pinned_set: Set[str] = {str(s) for s in pinned}
        names = names or {}
        sectors = sectors or {}

        ranking_set: Set[str] = (
            {str(s) for s in ranking_universe}
            if ranking_universe is not None
            else {str(s) for s in history.keys()}
        )
        universe = set(map(str, history.keys())) | holdings_set | pinned_set

        raw: Dict[str, Dict[str, Any]] = {}
        for symbol in sorted(universe):
            if symbol not in history:
                raw[symbol] = {"ok": False, "error": "NO_HISTORY"}
                continue
            try:
                raw[symbol] = self._metrics(history[symbol])
            except Exception as exc:
                raw[symbol] = {"ok": False, "error": f"METRIC_ERROR:{type(exc).__name__}"}

        rs20 = self._percentile_rank({
            s: float(m["ret20"]) for s, m in raw.items()
            if (
                s in ranking_set
                and m.get("ok")
                and math.isfinite(float(m.get("ret20", 0.0)))
            )
        })

        rows: List[Dict[str, Any]] = []
        for symbol, m in raw.items():
            is_holding = symbol in holdings_set
            is_pinned = symbol in pinned_set
            reasons: List[str] = []
            failed: List[str] = []
            score = None
            eligible = False

            if is_holding:
                reasons.append("HELD_POSITION")
            if is_pinned:
                reasons.append("PINNED")

            if not m.get("ok"):
                failed.append(str(m.get("error", "UNKNOWN_DATA_ERROR")))
            else:
                eligible = bool(m["eligible"])
                score = 0.0

                close_gt_ma20 = bool(m["close"] > m["ma20"])
                ma20_gt_ma60 = bool(m["ma20"] > m["ma60"])

                if m["trend_pass"]:
                    reasons.append(
                        "TREND_UP: close > MA20 > MA60 "
                        f"(close={m['close']:.3f}, MA20={m['ma20']:.3f}, MA60={m['ma60']:.3f})"
                    )
                    score += 35.0
                else:
                    failed.append("TREND_FAIL")
                    failed.append(
                        "TREND_DETAIL: "
                        f"close>MA20={close_gt_ma20}; "
                        f"MA20>MA60={ma20_gt_ma60}; "
                        f"close={m['close']:.3f}; "
                        f"MA20={m['ma20']:.3f}; "
                        f"MA60={m['ma60']:.3f}"
                    )

                if m["momentum_pass"]:
                    reasons.append(f"MOMENTUM20_PASS: {m['ret20']:.2%}")
                    score += 20.0
                else:
                    failed.append(f"MOMENTUM20_FAIL: {m['ret20']:.2%}")

                if m["near_high_pass"]:
                    reasons.append(
                        f"NEAR_20D_CLOSE_HIGH: {m['near_high20']:.3f}"
                    )
                    score += 15.0
                else:
                    failed.append(
                        f"FAR_FROM_20D_CLOSE_HIGH: {m['near_high20']:.3f}"
                    )

                if m["liquidity_pass"]:
                    reasons.append(f"LIQUIDITY_PASS: avg20={m['avg_amount20']:.0f}")
                    score += 10.0
                else:
                    failed.append(f"LIQUIDITY_FAIL: avg20={m['avg_amount20']:.0f}")

                if m["price_pass"]:
                    reasons.append("PRICE_PASS")
                else:
                    failed.append("PRICE_FAIL")

                if symbol in ranking_set:
                    rs = float(rs20.get(symbol, 0.0))
                    score += 20.0 * rs
                    reasons.append(f"RS20_PERCENTILE: {rs:.1%}")
                else:
                    reasons.append("RS20_PERCENTILE: N/A (OUTSIDE_RANKING_UNIVERSE)")

            rows.append({
                "symbol": symbol,
                "name": names.get(symbol),
                "sector": sectors.get(symbol),
                "forced": bool(is_holding or is_pinned),
                "is_holding": is_holding,
                "is_pinned": is_pinned,
                "eligible": eligible,
                "candidate_eligible": bool(m.get("candidate_eligible", False)) if m.get("ok") else False,
                "score": round(score, 3) if score is not None else None,
                "reasons": reasons,
                "failed_reasons": failed,
                "metrics": {
                    k: v for k, v in m.items()
                    if k not in {"ok", "eligible", "candidate_eligible"}
                },
            })

        if mode == "candidate_pool":
            pool: List[Dict[str, Any]] = []
            for row in rows:
                if not (row["candidate_eligible"] or row["forced"]):
                    continue
                item = dict(row)
                item["selected"] = True
                if item["is_holding"]:
                    item["status"] = "HOLDING"
                elif item["is_pinned"]:
                    item["status"] = "PINNED"
                else:
                    item["status"] = "CANDIDATE"
                pool.append(item)

            return sorted(
                pool,
                key=lambda r: (
                    0 if r["is_holding"] else 1 if r["is_pinned"] else 2,
                    -(float(r["score"]) if r["score"] is not None else -1.0),
                    r["symbol"],
                ),
            )

        eligible_rows = sorted(
            (r for r in rows if r["eligible"] and not r["forced"]),
            key=lambda r: (-float(r["score"]), r["symbol"])
        )
        selected_symbols = {r["symbol"] for r in eligible_rows[:max(0, int(self.cfg.top_n))]}

        out: List[Dict[str, Any]] = []
        for row in rows:
            row = dict(row)
            auto_selected = row["symbol"] in selected_symbols
            row["selected"] = bool(auto_selected or row["forced"])
            if row["is_holding"]:
                row["status"] = "HOLDING"
            elif row["is_pinned"]:
                row["status"] = "PINNED"
            elif auto_selected:
                row["status"] = "SELECTED"
            else:
                row["status"] = "EXCLUDED"
            if row["selected"]:
                out.append(row)

        return sorted(
            out,
            key=lambda r: (
                0 if r["is_holding"] else 1 if r["is_pinned"] else 2,
                -(float(r["score"]) if r["score"] is not None else -1.0),
                r["symbol"],
            ),
        )
