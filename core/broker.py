from __future__ import annotations

import json
import threading
import time
from typing import Callable, Dict, List, Optional

from .models import AccountSnapshot, PositionSnapshot, now_iso


class BrokerError(RuntimeError):
    pass


class _CallbackProxy:
    """运行时创建真正的 XtQuantTraderCallback 子类，避免无 xtquant 环境导入失败。"""
    @staticmethod
    def build(event_sink: Optional[Callable[[str, Dict], None]] = None):
        from xtquant.xttrader import XtQuantTraderCallback

        class Callback(XtQuantTraderCallback):
            def _emit(self, kind: str, obj):
                if event_sink:
                    payload = {}
                    for name in dir(obj):
                        if name.startswith("_"):
                            continue
                        try:
                            val = getattr(obj, name)
                            if not callable(val) and isinstance(val, (str, int, float, bool, type(None))):
                                payload[name] = val
                        except Exception:
                            pass
                    event_sink(kind, payload)

            def on_disconnected(self): self._emit("DISCONNECTED", type("X", (), {})())
            def on_stock_order(self, order): self._emit("ORDER", order)
            def on_stock_trade(self, trade): self._emit("TRADE", trade)
            def on_order_error(self, error): self._emit("ORDER_ERROR", error)
            def on_cancel_error(self, error): self._emit("CANCEL_ERROR", error)
            def on_account_status(self, status): self._emit("ACCOUNT_STATUS", status)

        return Callback()


class MiniQmtBroker:
    def __init__(self, mini_path: str, account_id: str, account_type: str = "STOCK", session_id: int = 0,
                 event_sink: Optional[Callable[[str, Dict], None]] = None):
        self.mini_path = mini_path
        self.account_id = account_id
        self.account_type = account_type
        self.session_id = session_id or int(time.time() * 1000) % 2_000_000_000
        self.event_sink = event_sink
        self.trader = None
        self.account = None
        self.callback = None
        self.connected = False
        self._lock = threading.RLock()

    def connect(self):
        with self._lock:
            try:
                from xtquant.xttrader import XtQuantTrader
                from xtquant.xttype import StockAccount
                self.trader = XtQuantTrader(self.mini_path, self.session_id)
                self.callback = _CallbackProxy.build(self.event_sink)
                self.trader.register_callback(self.callback)
                self.trader.start()
                rc = self.trader.connect()
                if rc != 0:
                    raise BrokerError(f"miniQMT交易连接失败 rc={rc}")
                self.account = StockAccount(self.account_id, self.account_type)
                sub = self.trader.subscribe(self.account)
                if sub != 0:
                    raise BrokerError(f"资金账号订阅失败 rc={sub}")
                self.connected = True
                return True
            except Exception:
                self.connected = False
                raise

    def disconnect(self):
        with self._lock:
            if self.trader:
                try: self.trader.stop()
                except Exception: pass
            self.connected = False

    def ensure_connected(self):
        if not self.connected or self.trader is None or self.account is None:
            raise BrokerError("交易服务未连接")

    def query_account(self) -> AccountSnapshot:
        self.ensure_connected()
        a = self.trader.query_stock_asset(self.account)
        if a is None:
            raise BrokerError("账户资产查询失败")
        return AccountSnapshot(now_iso(), float(a.total_asset), float(a.cash), float(a.market_value), float(getattr(a, "frozen_cash", 0) or 0))

    def query_positions(self) -> List[PositionSnapshot]:
        self.ensure_connected()
        out = []
        for p in self.trader.query_stock_positions(self.account) or []:
            avg_price = getattr(p, "avg_price", None)
            if avg_price is None:
                avg_price = getattr(p, "open_price", 0)
            out.append(PositionSnapshot(
                ts=now_iso(), symbol=p.stock_code, volume=int(p.volume), can_use_volume=int(p.can_use_volume),
                avg_price=float(avg_price or 0), market_value=float(p.market_value or 0),
                last_price=0.0, pnl=float(getattr(p, "pnl", 0) or 0)
            ))
        return out

    @staticmethod
    def _direction(order_type: int) -> str:
        try:
            from xtquant import xtconstant
            if order_type == xtconstant.STOCK_BUY: return "BUY"
            if order_type == xtconstant.STOCK_SELL: return "SELL"
        except Exception:
            pass
        return str(order_type)

    def query_orders(self) -> List[Dict]:
        self.ensure_connected()
        out = []
        for o in self.trader.query_stock_orders(self.account) or []:
            out.append({
                "order_id": int(o.order_id), "ts": now_iso(), "order_time": getattr(o, "order_time", 0), "symbol": o.stock_code,
                "direction": self._direction(o.order_type), "price": float(o.price or 0),
                "volume": int(getattr(o, "order_volume", getattr(o, "volume", 0)) or 0),
                "traded_volume": int(o.traded_volume or 0), "status": str(o.order_status),
                "status_msg": str(getattr(o, "status_msg", "") or ""),
                "traded_price": float(getattr(o, "traded_price", 0) or 0),
                "strategy_name": str(getattr(o, "strategy_name", "") or ""),
                "remark": str(getattr(o, "order_remark", "") or ""),
            })
        return out

    def query_trades(self) -> List[Dict]:
        self.ensure_connected()
        out = []
        for t in self.trader.query_stock_trades(self.account) or []:
            out.append({
                "trade_id": str(getattr(t, "traded_id", getattr(t, "trade_id", ""))),
                "order_id": int(t.order_id), "ts": now_iso(),
                "traded_time": getattr(t, "traded_time", 0), "symbol": t.stock_code,
                "direction": self._direction(t.order_type), "price": float(t.traded_price or 0),
                "volume": int(t.traded_volume or 0), "amount": float(getattr(t, "traded_amount", 0) or 0),
                "strategy_name": str(getattr(t, "strategy_name", "") or ""),
                "remark": str(getattr(t, "order_remark", "") or ""),
            })
        return out

    def active_external_orders(self) -> List[Dict]:
        try:
            from xtquant import xtconstant
            active = {xtconstant.ORDER_UNREPORTED, xtconstant.ORDER_WAIT_REPORTING, xtconstant.ORDER_REPORTED,
                      xtconstant.ORDER_REPORTED_CANCEL, xtconstant.ORDER_PARTSUCC_CANCEL, xtconstant.ORDER_PART_SUCC}
        except Exception:
            active = {48,49,50,51,52,55}
        return [o for o in self.query_orders() if int(o["status"]) in active and not str(o.get("strategy_name", "")).startswith("PositionX")]

    def submit_limit(self, symbol: str, side: str, volume: int, price: float, remark: str) -> int:
        self.ensure_connected()
        from xtquant import xtconstant
        order_type = xtconstant.STOCK_BUY if side.upper() == "BUY" else xtconstant.STOCK_SELL
        order_id = self.trader.order_stock(
            self.account, symbol, order_type, int(volume), xtconstant.FIX_PRICE, float(price),
            "PositionX", remark[:64]
        )
        if int(order_id) <= 0:
            raise BrokerError(f"下单失败 {symbol} {side} {volume}@{price}, rc={order_id}")
        return int(order_id)

    def cancel(self, order_id: int) -> bool:
        self.ensure_connected()
        return self.trader.cancel_order_stock(self.account, int(order_id)) == 0
