from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable

PILOT_STRATEGY_NAME = "PositionX:G4_EXECUTION_PILOT"
CAPITAL_ENVELOPE_RMB = 600_000.0
PLANNED_GROSS_CAP_RMB = 150_000.0
HARD_GROSS_GUARD_RMB = 180_000.0
MAX_ORDER_VALUE_RMB = 50_000.0
LOSS_WARNING_RMB = 20_000.0
STOP_NEW_ENTRIES_LOSS_RMB = 30_000.0
EMERGENCY_EXIT_PLAN_LOSS_RMB = 40_000.0
LOSS_BUDGET_RMB = 50_000.0

@dataclass(frozen=True)
class Fill:
    trade_id: str
    order_id: int
    trade_date: str
    symbol: str
    side: str
    price: float
    volume: int
    amount: float

def expected_remark(trade_date: str, intent_key: str) -> str:
    return f"PX|{str(trade_date).replace('-','')}|{str(intent_key)[:8]}"

def reconstruct_subledger(fills: Iterable[Fill], marks: dict[str,float] | None = None) -> dict:
    """Long-only Pilot subledger, isolated from external/account holdings.

    PnL before broker fees:
      cashflow_pnl + marked inventory
    where buys are negative cashflow and sells positive cashflow.
    Average cost is used only to split realized/unrealized; total PnL is
    independent of that split.
    """
    marks = dict(marks or {})
    pos = {}
    cashflow = 0.0
    realized = 0.0

    rows = sorted(list(fills), key=lambda x:(x.trade_date, x.order_id, x.trade_id))
    for f in rows:
        side=f.side.upper()
        if side not in ("BUY","SELL"):
            raise ValueError(f"invalid side={f.side}")
        if f.price <= 0 or f.volume <= 0 or f.amount <= 0:
            raise ValueError(f"invalid fill={f}")
        expected_amt=float(f.price)*int(f.volume)
        # Broker amount may be rounded. Allow a tiny relative tolerance.
        if abs(float(f.amount)-expected_amt) > max(0.05, expected_amt*1e-6):
            raise ValueError(f"amount mismatch trade_id={f.trade_id}")
        p=pos.setdefault(f.symbol,{"shares":0,"avg_cost":0.0})
        if side=="BUY":
            old_sh=int(p["shares"])
            new_sh=old_sh+int(f.volume)
            p["avg_cost"]=(
                (old_sh*float(p["avg_cost"]) + int(f.volume)*float(f.price))/new_sh
                if new_sh>0 else 0.0
            )
            p["shares"]=new_sh
            cashflow -= float(f.amount)
        else:
            if int(f.volume) > int(p["shares"]):
                raise ValueError(
                    f"PILOT_SELL_EXCEEDS_PILOT_SHARES symbol={f.symbol} "
                    f"sell={f.volume} pilot_shares={p['shares']}"
                )
            realized += int(f.volume)*(float(f.price)-float(p["avg_cost"]))
            p["shares"]=int(p["shares"])-int(f.volume)
            cashflow += float(f.amount)
            if p["shares"]==0:
                p["avg_cost"]=0.0

    open_positions={}
    market_value=0.0
    unrealized=0.0
    missing_marks=[]
    for symbol,p in sorted(pos.items()):
        sh=int(p["shares"])
        if sh<=0:
            continue
        mark=marks.get(symbol)
        if mark is None or float(mark)<=0:
            missing_marks.append(symbol)
            continue
        mark=float(mark)
        mv=sh*mark
        market_value += mv
        unrealized += sh*(mark-float(p["avg_cost"]))
        open_positions[symbol]={
            "shares":sh,"avg_cost":float(p["avg_cost"]),
            "mark":mark,"market_value":mv,
            "unrealized_pnl":sh*(mark-float(p["avg_cost"])),
        }

    total_pnl_pre_fee = realized + unrealized
    # Cashflow + MV is an independent invariant whenever all open positions are marked.
    cashflow_mark_pnl = None if missing_marks else cashflow + market_value
    if cashflow_mark_pnl is not None and abs(cashflow_mark_pnl-total_pnl_pre_fee)>0.1:
        raise AssertionError(
            f"PnL invariant violated total={total_pnl_pre_fee} cashflow_mark={cashflow_mark_pnl}"
        )

    return {
        "fill_count":len(rows),
        "open_positions":open_positions,
        "missing_marks":missing_marks,
        "marked_gross_rmb":market_value,
        "realized_pnl_pre_fee_rmb":realized,
        "unrealized_pnl_pre_fee_rmb":unrealized,
        "total_pnl_pre_fee_rmb":total_pnl_pre_fee,
        "cashflow_rmb":cashflow,
        "cashflow_mark_pnl_rmb":cashflow_mark_pnl,
    }

def loss_ladder(total_pnl_pre_fee_rmb: float) -> dict:
    loss=max(0.0,-float(total_pnl_pre_fee_rmb))
    if loss >= LOSS_BUDGET_RMB:
        state="BUDGET_BREACH"
    elif loss >= EMERGENCY_EXIT_PLAN_LOSS_RMB:
        state="EMERGENCY_EXIT_PLAN_REQUIRED"
    elif loss >= STOP_NEW_ENTRIES_LOSS_RMB:
        state="STOP_NEW_ENTRIES"
    elif loss >= LOSS_WARNING_RMB:
        state="WARNING"
    else:
        state="NORMAL"
    return {
        "state":state,
        "loss_used_pre_fee_rmb":loss,
        "allow_new_entries":state in ("NORMAL","WARNING"),
        "generate_emergency_exit_plan":state in ("EMERGENCY_EXIT_PLAN_REQUIRED","BUDGET_BREACH"),
        "fee_accounting_status":"BROKER_FEES_NOT_IN_CURRENT_SQLITE_SCHEMA",
        "safety_note":"Earlier 20k/30k/40k triggers provide buffer; 50k remains a budget, not a guaranteed realized-loss ceiling.",
    }

def gross_guard(marked_gross_rmb: float, pending_new_notional_rmb: float = 0.0) -> dict:
    after=float(marked_gross_rmb)+max(0.0,float(pending_new_notional_rmb))
    if after > HARD_GROSS_GUARD_RMB + 1e-9:
        state="BLOCK_HARD_GROSS"
    elif after > PLANNED_GROSS_CAP_RMB + 1e-9:
        state="BLOCK_NEW_ENTRY_PLANNED_GROSS"
    else:
        state="PASS"
    return {
        "state":state,
        "marked_gross_rmb":float(marked_gross_rmb),
        "pending_new_notional_rmb":float(pending_new_notional_rmb),
        "gross_after_rmb":after,
        "planned_gross_cap_rmb":PLANNED_GROSS_CAP_RMB,
        "hard_gross_guard_rmb":HARD_GROSS_GUARD_RMB,
        "allow_new_entry":state=="PASS",
    }
