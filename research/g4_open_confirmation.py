from __future__ import annotations
from dataclasses import dataclass
import math

DEEP_ID="DEEP_OVERSOLD_CSI500_FORWARD_V1_1"
RET120_ID="RET120_CSI500_FORWARD_V1_0"
SLEEVE_SCALE=6.0            # 1m Forward base ledger -> 6m sleeve in 12m Shadow Book
TARGET_WEIGHT=0.01
MAX_TARGET_GROSS=0.30
MAIN_HALF_COST=0.001        # 20bp round-trip => 10bp per side
RET120_MAX_POSITIONS=30
PILOT_MAX_NOTIONAL=50_000.0

@dataclass(frozen=True)
class Position:
    symbol:str
    shares:float

@dataclass(frozen=True)
class Candidate:
    symbol:str
    fillable:bool

def deep_open_allocation(base_cash_1m:float, positions_1m:list[Position],
                         open_prices:dict[str,float], candidates:list[Candidate]) -> dict:
    cash=float(base_cash_1m)*SLEEVE_SCALE
    held={p.symbol for p in positions_1m}
    open_mv=sum(float(p.shares)*SLEEVE_SCALE*float(open_prices[p.symbol]) for p in positions_1m)
    equity_open=cash+open_mv
    remaining_gross=max(0.0,MAX_TARGET_GROSS*equity_open-open_mv)
    remaining_cash=max(0.0,cash/(1.0+MAIN_HALF_COST))
    available=min(remaining_gross,remaining_cash)
    rows=[c for c in candidates if c.fillable and c.symbol not in held]
    desired_each=TARGET_WEIGHT*equity_open
    desired_total=desired_each*len(rows)
    alloc_total=min(desired_total,available)
    scale=(alloc_total/desired_total) if desired_total>0 else 0.0
    per={c.symbol:desired_each*scale for c in rows}
    return {
      "equity_open":equity_open,"open_mv":open_mv,"cash":cash,
      "remaining_gross":remaining_gross,"remaining_cash_notional":remaining_cash,
      "available_new_notional":available,"desired_each":desired_each,
      "fillable_new_count":len(rows),"scale":scale,"notional_by_symbol":per
    }

def ret120_open_allocation(base_cash_1m:float, positions_1m:list[Position],
                           open_prices:dict[str,float], candidates:list[Candidate]) -> dict:
    cash=float(base_cash_1m)*SLEEVE_SCALE
    held={p.symbol for p in positions_1m}
    open_mv=sum(float(p.shares)*SLEEVE_SCALE*float(open_prices[p.symbol]) for p in positions_1m)
    equity_open=cash+open_mv
    remaining_gross=max(0.0,MAX_TARGET_GROSS*equity_open-open_mv)
    remaining_cash=max(0.0,cash/(1.0+MAIN_HALF_COST))
    available=min(remaining_gross,remaining_cash)
    desired_each=TARGET_WEIGHT*equity_open
    rows=sorted([c for c in candidates if c.fillable and c.symbol not in held],key=lambda c:c.symbol)
    slots=max(0,RET120_MAX_POSITIONS-len(positions_1m))
    capacity_count=int(math.floor((available+1e-9)/desired_each)) if desired_each>0 else 0
    k=min(len(rows),slots,capacity_count)
    selected=rows[:k]
    per={c.symbol:desired_each for c in selected}
    return {
      "equity_open":equity_open,"open_mv":open_mv,"cash":cash,
      "remaining_gross":remaining_gross,"remaining_cash_notional":remaining_cash,
      "available_new_notional":available,"desired_each":desired_each,
      "open_slots":slots,"capacity_count":capacity_count,
      "selected_count":k,"notional_by_symbol":per
    }

def pilot_target(confirmed_shadow_notional:float)->float:
    x=float(confirmed_shadow_notional)
    if x<=0:return 0.0
    return min(PILOT_MAX_NOTIONAL,x)
