from __future__ import annotations
import argparse, json, re, sys, tempfile
from pathlib import Path
from datetime import date, datetime

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:sys.path.insert(0,str(PROJECT_ROOT))

from core.db import Database
from core.data import MarketDataService
from research.g4_ledger_resolution import choose_base_ledger
from research.g4_open_confirmation import (
    DEEP_ID,RET120_ID,Position,Candidate,
    deep_open_allocation,ret120_open_allocation,pilot_target
)

DB=PROJECT_ROOT/"data/research/positionx_research.duckdb"
SETTINGS=PROJECT_ROOT/"config/settings.yaml"
DEEP_STATE=PROJECT_ROOT/"data/research/forward_deep_oversold_csi500_v1_1/state.json"
RET_STATE=PROJECT_ROOT/"data/research/forward_ret120_csi500_v1_0/state.json"

STATE_BY_STRATEGY={DEEP_ID:DEEP_STATE,RET120_ID:RET_STATE}

def readj(p):
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}

def stale_seconds():
    txt=SETTINGS.read_text(encoding="utf-8-sig")
    m=re.search(r"(?m)^\s*stale_tick_seconds\s*:\s*(\d+)",txt)
    return int(m.group(1)) if m else 30

def connect(ro):
    import duckdb
    return duckdb.connect(str(DB),read_only=ro)

def table_exists(con,t):
    return bool(con.execute("select 1 from information_schema.tables where table_schema='main' and table_name=?",[t]).fetchone())

def distinct_ledgers(con,strategy):
    if not table_exists(con,"forward_positions"):return []
    return [r[0] for r in con.execute("select distinct ledger from forward_positions where strategy_id=? order by ledger",[strategy]).fetchall()]

def resolve_base_ledger(con,strategy):
    rows=con.execute(
      "select ledger,count(*) from forward_positions where strategy_id=? group by ledger order by ledger",
      [strategy]
    ).fetchall()
    counts={str(k):int(v) for k,v in rows}
    state=readj(STATE_BY_STRATEGY[strategy])
    expected=int(state.get("active_positions") or 0)
    return choose_base_ledger(counts,expected)

def active_positions(con,strategy):
    ledger=resolve_base_ledger(con,strategy)
    if ledger is None:return []
    return [Position(r[0],float(r[1])) for r in con.execute(
      "select symbol,shares from forward_positions where strategy_id=? and ledger=? order by symbol",
      [strategy,ledger]).fetchall()]

def reservations(con):
    if not table_exists(con,"g4_shadow_reservations"):return []
    cols=[r[1] for r in con.execute("pragma table_info('g4_shadow_reservations')").fetchall()]
    needed={"shadow_decision_id","signal_date","strategy_id","symbol","stage","reservation_status"}
    if not needed.issubset(set(cols)):raise RuntimeError(f"reservation schema mismatch columns={cols}")
    rows=con.execute("""
      select shadow_decision_id,signal_date,strategy_id,symbol,stage,reservation_status
      from g4_shadow_reservations
      where reservation_status='RESERVED_AWAIT_T1_OPEN_CONFIRMATION'
      order by signal_date, sample_rank
    """).fetchall()
    return [{"id":r[0],"signal_date":r[1],"strategy_id":r[2],"symbol":r[3],"stage":r[4],"status":r[5]} for r in rows]

def candidate_symbols(con,signal_date,strategy):
    rows=con.execute("""
      select symbol from g4_shadow_reservation_candidates
      where signal_date=? and strategy_id=?
      order by symbol
    """,[signal_date,strategy]).fetchall()
    return [r[0] for r in rows]

def _first(tick,*keys):
    for k in keys:
        v=tick.get(k)
        if v is None:continue
        if isinstance(v,(list,tuple)):
            if not v:continue
            v=v[0]
        try:
            x=float(v)
            if x>0:return x
        except Exception:pass
    return None

def tick_open(tick):
    return _first(tick,"open","openPrice","open_price","openPx")

def tick_high(tick):
    return _first(tick,"high","highPrice","high_price")

def tick_low(tick):
    return _first(tick,"low","lowPrice","low_price")

def tick_last(tick):
    return _first(tick,"lastPrice","last_price","price")

def tick_prev_close(tick):
    return _first(tick,"lastClose","preClose","pre_close","last_close")

def fillability(market,symbol,tick):
    sec=stale_seconds()
    if not market.is_tick_fresh(tick,sec):
        return False,"STALE_TICK"
    if MarketDataService.security_state(tick)!=13:
        return False,"NOT_CONTINUOUS_SESSION"
    op=tick_open(tick)
    if not op or op<=0:
        return False,"MISSING_POSITIVE_OPEN"
    try:
        market.ensure_tradable(symbol)
    except Exception as e:
        return False,f"STATIC_TRADABILITY:{type(e).__name__}:{e}"
    try:
        down,up=market.price_limits(symbol)
    except Exception:
        down,up=None,None
    detail={}
    try:detail=market.instrument_detail(symbol) or {}
    except Exception:detail={}
    tick_size=float(detail.get("PriceTick") or 0.01)
    if up is not None:
        try:
            if op >= float(up)-tick_size/2:
                return False,"OPEN_AT_OR_NEAR_UP_LIMIT"
        except Exception:
            return False,"UP_LIMIT_PARSE_ERROR"
    else:
        hi,lo,last,prev=tick_high(tick),tick_low(tick),tick_last(tick),tick_prev_close(tick)
        if hi is not None and lo is not None and last is not None and prev is not None:
            eps=max(tick_size/2,1e-9)
            if abs(hi-lo)<=eps and abs(last-lo)<=eps and last>prev:
                return False,"ONE_PRICE_UP_LOCK_LIMIT_UNKNOWN"
        else:
            return False,"LIMIT_UNKNOWN_AND_LOCK_DIAGNOSTIC_INCOMPLETE"
    return True,"STANDARD_OPEN_V1_PASS"

def market_snapshot(symbols):
    with tempfile.TemporaryDirectory() as td:
        db=Database(str(Path(td)/"g4_market_probe.db"))
        market=MarketDataService(db)
        out={}
        for s in sorted(set(symbols)):
            tick=market.full_tick(s)
            ok,reason=fillability(market,s,tick)
            out[s]={
              "tick":tick,"open":tick_open(tick),"fillable":ok,"fillability_reason":reason,
              "tick_fresh":bool(market.is_tick_fresh(tick,stale_seconds())),
              "security_state":MarketDataService.security_state(tick),
            }
        return out

def check():
    con=connect(True)
    try:
        result={
          "status":"PASS_G4_OPEN_CONFIRMATION_V0_3_CHECK",
          "ledgers":{s:distinct_ledgers(con,s) for s in (DEEP_ID,RET120_ID)},
          "reservations_waiting":len(reservations(con)),
          "states":{s:readj(p) for s,p in STATE_BY_STRATEGY.items()},
          "rules":{
            "shadow_book_rmb":12000000,"sleeve_rmb_initial":6000000,
            "source_forward_scale":6.0,"target_weight":0.01,"max_target_gross":0.30,
            "deep":"all fillable same-day candidates share remaining gross pro-rata",
            "ret120":"fillable candidates SYMBOL_ASC; max 30 positions; full 1% tickets while slot/gross/cash available",
            "pilot_target":"min(50000, confirmed_shadow_notional)",
          },
          "safety":{"qmt":False,"broker":False,"production_db_opened":False,"production_db_mutation":False}
        }
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
    finally:con.close()

def confirm():
    today=date.today().strftime("%Y%m%d")
    con=connect(False)
    try:
        rs=reservations(con)
        if not rs:
            print(json.dumps({"status":"PASS_G4_OPEN_CONFIRMATION_V0_3_NOOP","reason":"NO_WAITING_RESERVATIONS","trade_date":today},indent=2));return 0
        signal_dates={r["signal_date"] for r in rs}
        if len(signal_dates)!=1:
            raise RuntimeError(f"MIXED_RESERVATION_SIGNAL_DATES {sorted(signal_dates)}")
        sig=next(iter(signal_dates))
        # Forward runner must not already have advanced beyond the signal day.
        for strategy,p in STATE_BY_STRATEGY.items():
            st=readj(p); lp=str(st.get("last_processed_date") or "").replace("-","")
            if lp!=sig:
                raise RuntimeError(f"FORWARD_STATE_TIMING_MISMATCH strategy={strategy} signal_date={sig} last_processed={lp}")

        all_symbols={r["symbol"] for r in rs}
        candidates_by={}
        positions_by={}
        for strategy in (DEEP_ID,RET120_ID):
            syms=candidate_symbols(con,sig,strategy)
            candidates_by[strategy]=syms
            pos=active_positions(con,strategy)
            positions_by[strategy]=pos
            all_symbols.update(syms)
            all_symbols.update(p.symbol for p in pos)

        snap=market_snapshot(sorted(all_symbols))
        # Require at least reservation symbols to be live/fresh. Existing-position open must exist too.
        for s in all_symbols:
            if snap[s]["open"] is None:
                raise RuntimeError(f"MISSING_OPEN {s}")
            if not snap[s]["tick_fresh"]:
                raise RuntimeError(f"STALE_TICK {s}")

        alloc={}
        for strategy in (DEEP_ID,RET120_ID):
            st=readj(STATE_BY_STRATEGY[strategy])
            cash=float(st.get("base_cash"))
            pos=positions_by[strategy]
            opens={s:snap[s]["open"] for s in set([p.symbol for p in pos]+candidates_by[strategy])}
            cands=[Candidate(s,bool(snap[s]["fillable"])) for s in candidates_by[strategy]]
            if strategy==DEEP_ID:
                alloc[strategy]=deep_open_allocation(cash,pos,opens,cands)
            else:
                alloc[strategy]=ret120_open_allocation(cash,pos,opens,cands)

        updates=[]
        con.execute("begin transaction")
        try:
            for r in rs:
                strategy=r["strategy_id"];sym=r["symbol"]
                fill=bool(snap[sym]["fillable"])
                shadow=float(alloc[strategy]["notional_by_symbol"].get(sym,0.0))
                if not fill:
                    status="BLOCKED_ENTRY_FILLABILITY"
                    pilot=0.0
                elif shadow<=0:
                    status="SHADOW_NOT_EXECUTABLE_CAPACITY_OR_SLOT"
                    pilot=0.0
                else:
                    status="CONFIRMED_READY_FOR_PREVIEW"
                    pilot=pilot_target(shadow)
                con.execute("""
                  update g4_shadow_reservations
                  set reservation_status=?, confirmed_entry_trade_date=?,
                      confirmed_shadow_notional_rmb=?, pilot_target_notional_rmb=?,
                      updated_at=current_timestamp
                  where shadow_decision_id=?
                """,[status,today,shadow,pilot,r["id"]])
                updates.append({
                  **r,"open":snap[sym]["open"],"fillable":fill,
                  "fillability_reason":snap[sym]["fillability_reason"],
                  "confirmed_shadow_notional_rmb":shadow,
                  "pilot_target_notional_rmb":pilot,
                  "confirmation_status":status,
                })
            con.execute("commit")
        except Exception:
            con.execute("rollback");raise

        result={
          "status":"PASS_G4_OPEN_CONFIRMATION_V0_3",
          "trade_date":today,"signal_date":sig,
          "confirmed":updates,
          "allocation_audit":alloc,
          "market_audit":{s:{k:v for k,v in snap[s].items() if k!="tick"} for s in sorted(all_symbols)},
          "next":"Only CONFIRMED_READY_FOR_PREVIEW rows may enter isolated ORDER_PREVIEW adapter.",
          "safety":{"qmt_read_only":True,"broker":False,"production_db_opened":False,"production_db_mutation":False,"research_db_mutated":True}
        }
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str));return 0
    finally:con.close()

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["check","confirm"])
    a=p.parse_args()
    try:return check() if a.command=="check" else confirm()
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_G4_OPEN_CONFIRMATION_V0_3","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));return 3
if __name__=="__main__":raise SystemExit(main())
