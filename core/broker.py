from __future__ import annotations

import json
import os
import itertools
import datetime
import math
import numbers
import re
import secrets
from .qmt_runtime import profile as qmt_profile, pin_sdk, verify_full_endpoint
import threading
import time
from typing import Callable, Dict, List, Optional

from .models import AccountSnapshot, PositionSnapshot, now_iso, broker_order_key


class BrokerError(RuntimeError):
    pass



_STAGE_SEQUENCE = itertools.count(1)
_STAGE_WRITE_LOCK = threading.Lock()
_NO_TRACE_RESULT = object()

def _broker_stage_trace(stage, status, *, call_id=None, result=_NO_TRACE_RESULT,
                        error=None, elapsed_ns=None):
    path = os.environ.get("POSITIONX_BROKER_TRACE_PATH")
    if not path:
        return
    record = {"schema":"BROKER_STAGE_TRACE_V1", "stage":stage, "status":status,
              "monotonic_ns":time.monotonic_ns(),
              "observed_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "pid":os.getpid(), "parent_pid":os.getppid(),
              "thread_id":threading.get_ident(), "call_id":call_id}
    if result is not _NO_TRACE_RESULT:
        typ = type(result)
        record["result_type"] = typ.__name__ if typ in (type(None),list,dict,tuple,int,bool,float,str) else "object"
        if typ is list:
            record["result_count"] = len(result)
        if stage in ("connect", "subscribe") and typ is int:
            record["return_code_zero"] = result == 0
    if error is not None:
        record["error_type"] = type(error).__name__
    if elapsed_ns is not None:
        record["elapsed_ns"] = elapsed_ns
    payload = (json.dumps(record, separators=(",",":"), allow_nan=False)+"\n").encode("utf-8")
    try:
        with _STAGE_WRITE_LOCK:
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        raise BrokerError("BROKER_STAGE_TRACE_WRITE_FAILED") from None

def _broker_traced_call(stage, operation, *args, **kwargs):
    if not os.environ.get("POSITIONX_BROKER_TRACE_PATH"):
        return operation(*args, **kwargs)
    call_id = next(_STAGE_SEQUENCE)
    try:
        _broker_stage_trace(stage, "ENTER", call_id=call_id)
    except Exception as trace_error:
        # Trace failure must not prevent disconnect cleanup or risk callbacks.
        if stage in ("stop", "stop_after_connect_error") or stage.startswith("callback/"):
            try:
                operation(*args, **kwargs)
            except BaseException as primary:
                raise primary from trace_error
        raise
    started = time.monotonic_ns()
    try:
        result = operation(*args, **kwargs)
    except BaseException as primary:
        try:
            _broker_stage_trace(stage, "EXCEPTION", call_id=call_id,
                                error=primary, elapsed_ns=time.monotonic_ns()-started)
        except Exception as trace_error:
            raise primary from trace_error
        raise
    _broker_stage_trace(stage, "RETURNED", call_id=call_id, result=result,
                        elapsed_ns=time.monotonic_ns()-started)
    return result

def _broker_sdk_types():
    from xtquant.xttrader import XtQuantTrader
    from xtquant.xttype import StockAccount
    return XtQuantTrader, StockAccount


class _CallbackProxy:
    """运行时创建真正的 XtQuantTraderCallback 子类，避免无 xtquant 环境导入失败。"""
    @staticmethod
    def build(event_sink: Optional[Callable[[str, Dict], None]] = None, account_id=None):
        from xtquant.xttrader import XtQuantTraderCallback

        class Callback(XtQuantTraderCallback):
            def _emit(self, kind: str, obj):
                if event_sink:
                    payload = {}
                    if account_id is not None:
                        # Only reviewed scalar facts enter production event logs. No account IDs,
                        # free text, arbitrary getters, class names or unknown fields.
                        try:
                            if kind != "DISCONNECTED" and getattr(obj,"account_id",None) != account_id:
                                raise ValueError("CALLBACK_ACCOUNT_BINDING")
                            for name in ("order_id","order_type","order_status","order_volume","traded_volume","traded_price","traded_amount","status","error_id"):
                                value=getattr(obj,name,None)
                                if value is None:continue
                                if isinstance(value,bool) or not isinstance(value,numbers.Real) or not math.isfinite(value):
                                    raise ValueError("CALLBACK_FIELD_TYPE")
                                payload[name]=value
                            code=getattr(obj,"stock_code",None)
                            if code is not None:
                                if type(code) is not str or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)",code):raise ValueError("CALLBACK_SYMBOL")
                                payload["stock_code"]=code
                        except Exception:
                            event_sink("CAPTURE_ERROR",{"error":"CALLBACK_FACT_CAPTURE_FAILED"})
                            return
                        event_sink(kind,payload)
                        return
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
        runtime=qmt_profile()
        self._full_mode=runtime.get("backend")=="full" or runtime["enabled"]
        self._full_connect_attempted=False
        self.last_cleanup_error=None
        self.read_coverage={}
        self.mini_path = runtime["broker_userdata"] if self._full_mode else mini_path
        self.account_id = account_id
        self.account_type = account_type
        self.session_id = (secrets.randbelow(1_999_999_999)+1) if self._full_mode else (session_id or int(time.time() * 1000) % 2_000_000_000)
        self.event_sink = event_sink
        self.trader = None
        self.account = None
        self.callback = None
        self.connected = False
        self._lock = threading.RLock()

    def connect(self):
        with self._lock:
            try:
                if self._full_mode:
                    if qmt_profile().get("enabled") is not True:raise BrokerError("FULL_QMT_DISABLED_NO_FALLBACK")
                    if self.connected:return True
                    if self._full_connect_attempted:raise BrokerError("FULL_QMT_RETRY_NOT_ALLOWED")
                    self._full_connect_attempted=True
                    if self.account_type!="STOCK":raise BrokerError("FULL_QMT_ACCOUNT_TYPE")
                    _broker_traced_call("endpoint_identity", verify_full_endpoint, qmt_profile())
                    _broker_traced_call("sdk_load", pin_sdk)
                XtQuantTrader, StockAccount = _broker_traced_call("sdk_types_load", _broker_sdk_types)
                self.trader = _broker_traced_call("trader_construct", XtQuantTrader, self.mini_path, self.session_id)
                def sink(kind,payload):
                    def deliver():
                        if kind in {"DISCONNECTED","CAPTURE_ERROR"}:self.connected=False
                        if self.event_sink:self.event_sink(kind,payload)
                    return _broker_traced_call("callback/"+kind, deliver)
                self.callback = _broker_traced_call("callback_build", _CallbackProxy.build, sink, self.account_id if self._full_mode else None)
                _broker_traced_call("register_callback", self.trader.register_callback, self.callback)
                _broker_traced_call("start", self.trader.start)
                rc = _broker_traced_call("connect", self.trader.connect)
                if type(rc) is not int or rc != 0:
                    raise BrokerError(f"miniQMT交易连接失败 rc={rc}")
                self.account = _broker_traced_call("account_construct", StockAccount, self.account_id, self.account_type)
                sub = _broker_traced_call("subscribe", self.trader.subscribe, self.account)
                if type(sub) is not int or sub != 0:
                    raise BrokerError(f"资金账号订阅失败 rc={sub}")
                self.connected = True
                return True
            except Exception:
                self.connected = False
                if self.trader is not None:
                    try:_broker_traced_call("stop_after_connect_error", self.trader.stop)
                    except Exception:self.last_cleanup_error="STOP_FAILED_AFTER_PRIMARY_ERROR"
                raise

    def disconnect(self):
        with self._lock:
            if self.trader:
                try: _broker_traced_call("stop", self.trader.stop)
                except Exception:
                    self.last_cleanup_error="STOP_FAILED"
                    self.connected=False
                    raise BrokerError("BROKER_STOP_FAILED") from None
            self.connected = False

    def ensure_connected(self):
        if not self.connected or self.trader is None or self.account is None:
            raise BrokerError("交易服务未连接")

    def _validate_row(self, obj, kind):
        if not self._full_mode:return
        if getattr(obj,"account_id",None)!=self.account_id:raise BrokerError("ACCOUNT_BINDING_MISMATCH")
        fields={"positions":("stock_code","volume","can_use_volume","market_value"),
                "orders":("stock_code","order_id","order_type","order_volume","price","traded_volume","order_status"),
                "trades":("stock_code","order_id","order_type","traded_id","traded_time","traded_volume","traded_price","traded_amount"),
                "asset":("total_asset","cash","market_value","frozen_cash")}[kind]
        if any(not hasattr(obj,k) for k in fields):raise BrokerError("BROKER_SCHEMA_MISSING_FIELD")
        for key in fields:
            value=getattr(obj,key)
            if key=="stock_code":
                if type(value) is not str or not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)",value):raise BrokerError("BROKER_STOCK_CODE")
            elif key=="traded_id":
                if type(value) not in (str,int) or value=="" or type(value) is int and value<=0:raise BrokerError("BROKER_TRADE_ID")
            else:
                if isinstance(value,bool) or not isinstance(value,numbers.Real) or not math.isfinite(value):raise BrokerError("BROKER_NUMERIC_FIELD")
                if key in ("order_id","volume","can_use_volume","order_volume","traded_volume") and (not isinstance(value,numbers.Integral) or value<0 or key=="order_id" and value==0 and kind not in ("orders","trades")):raise BrokerError("BROKER_INTEGER_FIELD")
        if kind=="trades" and obj.order_id==0:
            # No local order ID: retain the native counter reference, never invent an ID.
            sysid=getattr(obj,"order_sysid",None)
            if type(sysid) is not str or not sysid:raise BrokerError("BROKER_TRADE_COUNTER_ID_MISSING_OR_INVALID")
        if kind=="orders" and obj.order_id==0:
            try:
                broker_order_key({"order_id":obj.order_id,"symbol":obj.stock_code,
                                  "order_sysid":getattr(obj,"order_sysid",None),
                                  "order_time":getattr(obj,"order_time",None)})
            except ValueError as exc:
                raise BrokerError(str(exc)) from None
        if kind=="positions":
            price=getattr(obj,"avg_price",getattr(obj,"open_price",None))
            if isinstance(price,bool) or not isinstance(price,numbers.Real) or not math.isfinite(price) or price<0:raise BrokerError("BROKER_POSITION_PRICE")
    def _query_rows(self, method, kind):
        value=_broker_traced_call(method, getattr(self.trader,method), self.account)
        if value is None:raise BrokerError("BROKER_QUERY_RETURNED_NONE")
        if type(value) is not list:raise BrokerError("BROKER_QUERY_WRONG_TYPE")
        for obj in value:self._validate_row(obj,kind)
        self.read_coverage[kind]="EMPTY_SCHEMA_NOT_COVERED" if len(value)==0 else "RUNTIME_FIELDS_VALIDATED"
        return value

    def query_account(self) -> AccountSnapshot:
        self.ensure_connected()
        a = _broker_traced_call("query_stock_asset", self.trader.query_stock_asset, self.account)
        if a is None:
            raise BrokerError("账户资产查询失败")
        self._validate_row(a,"asset")
        self.read_coverage["asset"]="RUNTIME_FIELDS_VALIDATED"
        return AccountSnapshot(now_iso(), float(a.total_asset), float(a.cash), float(a.market_value), float(a.frozen_cash) if self._full_mode else float(getattr(a, "frozen_cash", 0) or 0))

    def query_positions(self) -> List[PositionSnapshot]:
        self.ensure_connected()
        out = []
        for p in self._query_rows("query_stock_positions", "positions"):
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
        for o in self._query_rows("query_stock_orders", "orders"):
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
            if self._full_mode:
                if o.order_id==0:
                    out[-1]["order_sysid"] = o.order_sysid
                out[-1]["order_key"] = broker_order_key(out[-1])
        if self._full_mode and len({o["order_key"] for o in out}) != len(out):
            raise BrokerError("BROKER_ORDER_IDENTITY_DUPLICATE")
        return out

    def query_trades(self) -> List[Dict]:
        self.ensure_connected()
        out = []
        for t in self._query_rows("query_stock_trades", "trades"):
            out.append({
                "trade_id": str(getattr(t, "traded_id", getattr(t, "trade_id", ""))),
                "order_id": int(t.order_id), "ts": now_iso(),
                "traded_time": getattr(t, "traded_time", 0), "symbol": t.stock_code,
                "direction": self._direction(t.order_type), "price": float(t.traded_price or 0),
                "volume": int(t.traded_volume or 0), "amount": float(getattr(t, "traded_amount", 0) or 0),
                "strategy_name": str(getattr(t, "strategy_name", "") or ""),
                "remark": str(getattr(t, "order_remark", "") or ""),
            })
            if self._full_mode and t.order_id==0:
                out[-1]["order_sysid"] = t.order_sysid
        if self._full_mode and len({t["trade_id"] for t in out}) != len(out):
            raise BrokerError("BROKER_TRADE_IDENTITY_DUPLICATE")
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
        if self._full_mode:raise BrokerError("FULL_QMT_READ_ONLY_SUBMIT_FORBIDDEN")
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
        if self._full_mode:raise BrokerError("FULL_QMT_READ_ONLY_CANCEL_FORBIDDEN")
        self.ensure_connected()
        return self.trader.cancel_order_stock(self.account, int(order_id)) == 0

# Existing constructor and public model contract are retained.
QmtBrokerAdapter = MiniQmtBroker
