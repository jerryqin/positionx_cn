from __future__ import annotations
import argparse, importlib.util, json, math, os, sys, hashlib
from pathlib import Path
from datetime import datetime
from collections import Counter
import pandas as pd
import numpy as np
import duckdb

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0,str(PROJECT_ROOT))

from research.g5_nearest_lot_economic_replay import (
    POLICY, norm_date, nearest_executable_shares, map_get,
    curve_metrics, combine_fixed_initial_sleeves, lot_fidelity, drift_pass, metric_drift,
)

P1_SCRIPT=PROJECT_ROOT/"scripts/audit_ret120_frozen_signal_comparable_v02.py"
RET_EVENTS=Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_q5_candidate_events.csv")
RET_SUMMARY=Path(r"D:\positionx_data\research\ret120_vs_52wh_family_v0_2_frozen_signal\ret120_comparable_summary.json")
DEEP_EVENTS=Path(r"D:\positionx_data\research\alpha2_incremental_value_v0_1\deep_oversold_export\deep_oversold_frozen_dedup_events.csv")
CATALOG=Path(r"D:\positionx_data\catalog\market_data.duckdb")
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g5_nearest_lot_economic_replay_v0_1")

FROZEN_DEEP={"trades":1365,"cagr":0.110399287254,"max_drawdown":-0.096240912573,"sharpe":1.04181152}
DEEP_EQUIV_THRESH={"trade_count":1,"cagr":0.0001,"max_drawdown":0.0001,"sharpe":0.005}

P3_FROZEN_APPROX={
    "deep_cagr":0.048927,
    "deep_mdd":-0.060875,
    "deep_sharpe":0.88835,
    "ret_cagr":0.092580,
    "ret_mdd":-0.068891,
    "ret_sharpe":1.13355,
    "combo_cagr":0.071136,
    "combo_mdd":-0.053823,
    "combo_sharpe":1.17674,
}
P3_REPRO_TOL={"cagr":0.0001,"mdd":0.0001,"sharpe":0.005}

def load_module(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError(f"cannot import {path}")
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def write_json_atomic(path:Path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(tmp,path)

def sha(p:Path):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(8*1024*1024),b""):h.update(b)
    return h.hexdigest()

def extract_calendar(panel:pd.DataFrame):
    for c in ("trade_date","date"):
        if c in panel.columns:
            return sorted({norm_date(x) for x in panel[c].dropna()})
    if isinstance(panel.index,pd.MultiIndex):
        for lev in range(panel.index.nlevels):
            vals=[norm_date(x) for x in panel.index.get_level_values(lev)]
            plausible=[x for x in vals if len(x)==8 and x.isdigit()]
            if len(set(plausible))>100: return sorted(set(plausible))
    vals=[norm_date(x) for x in panel.index]
    if len(set(vals))>100: return sorted(set(vals))
    raise RuntimeError(f"cannot extract calendar; columns={list(panel.columns)}")

def normalize_result(res, metric_fn=None):
    curve=None; trades=None; diagnostics={}
    if isinstance(res,dict):
        for k in ("curve","equity_curve","curve_df"):
            if isinstance(res.get(k),pd.DataFrame): curve=res[k]; break
        for k in ("trades_df","trades","trade_df"):
            if isinstance(res.get(k),pd.DataFrame): trades=res[k]; break
        diagnostics=res.get("diagnostics",{}) if isinstance(res.get("diagnostics",{}),dict) else {}
    elif isinstance(res,(tuple,list)):
        for x in res:
            if isinstance(x,pd.DataFrame):
                cols=set(x.columns)
                if "equity" in cols and curve is None: curve=x
                elif "symbol" in cols and trades is None: trades=x
            elif isinstance(x,dict):
                diagnostics.update(x)
    if curve is None:
        raise RuntimeError(f"unsupported simulator result type={type(res).__name__}")
    if trades is None: trades=pd.DataFrame()
    metrics=metric_fn(curve) if metric_fn is not None else None
    return curve.copy(),trades.copy(),diagnostics,metrics

def ret120_data(p1):
    q5=pd.read_csv(RET_EVENTS,dtype={"signal_date":str,"entry_date":str,"exit_date":str,"symbol":str})
    for c in ("signal_date","entry_date","exit_date"): q5[c]=q5[c].map(norm_date)
    symbols=sorted(q5["symbol"].unique())
    con=duckdb.connect(str(CATALOG),read_only=True)
    try:
        panel=p1.load_panel(con,symbols,"20260930")
    finally: con.close()
    full_calendar=extract_calendar(panel)
    d0=min(q5["entry_date"]); d1=max(q5["exit_date"])
    calendar=[d for d in full_calendar if d0 <= d <= d1]
    if len(calendar) != 665:
        raise RuntimeError(f"RET120 calendar window mismatch sessions={len(calendar)} expected=665 start={calendar[0] if calendar else None} end={calendar[-1] if calendar else None}")
    pm=p1.price_maps(panel)
    if not isinstance(pm,(tuple,list)) or len(pm)<2:
        raise RuntimeError(f"unexpected price_maps return {type(pm).__name__}")
    open_map,close_map=pm[0],pm[1]
    return q5,calendar,open_map,close_map,panel

def ret_sim_lot(q5,calendar,open_map,close_map,cost,initial_equity):
    half=cost/2
    dates=[norm_date(x) for x in calendar]
    q=q5.copy()
    q["entry_date"]=q["entry_date"].map(norm_date); q["exit_date"]=q["exit_date"].map(norm_date)
    entries={d:g.sort_values("symbol").to_dict("records") for d,g in q.groupby("entry_date")}
    cash=float(initial_equity); positions={}; last_close={}
    curve=[]; trades=[]; skips=Counter(); decisions=[]
    sizing_attempts=0; lot_feasible_attempts=0

    for date in dates:
        # OPEN: current holdings only; same-day CLOSE proceeds are not reusable.
        open_mv=0.0
        for s,pos in positions.items():
            px=map_get(open_map,s,date,last_close.get(s,pos["entry_price"]))
            if px is None: raise RuntimeError(f"RET missing open mark {s} {date}")
            open_mv += pos["shares"]*px

        eq_open=cash+open_mv
        gross_cap=POLICY.max_target_gross*eq_open
        gross_remaining=max(0.0,gross_cap-open_mv)
        cash_remaining=max(0.0,cash/(1+half))

        for row in entries.get(date,[]):
            s=str(row["symbol"])
            if s in positions:
                skips["DUPLICATE_WHILE_HELD"]+=1; continue
            if len(positions)>=POLICY.ret120_max_positions:
                skips["NO_SLOT"]+=1; continue
            if min(gross_remaining,cash_remaining) <= 0:
                skips["NO_GROSS_OR_CASH_CAPACITY"]+=1; continue

            px=float(row["entry_adj_open"]) if pd.notna(row.get("entry_adj_open")) else map_get(open_map,s,date)
            if px is None or px<=0:
                skips["NO_ENTRY_PRICE"]+=1; continue

            target=POLICY.strategy_target_weight*eq_open
            sizing_attempts += 1
            pure=nearest_executable_shares(s,px,target,POLICY.max_relative_target_error,None)
            if pure is None:
                skips["LOT_INFEASIBLE"]+=1
                decisions.append({"symbol":s,"date":date,"signal_date":norm_date(row["signal_date"]),
                                  "target_notional":target,"entry_price":px,"lot_feasible":False,"executed":False})
                continue
            lot_feasible_attempts += 1

            max_notional=min(gross_remaining,cash_remaining)
            chosen=nearest_executable_shares(s,px,target,POLICY.max_relative_target_error,max_notional)
            if chosen is None:
                skips["LOT_FEASIBLE_BUT_CAPACITY_BLOCKED"]+=1
                decisions.append({"symbol":s,"date":date,"signal_date":norm_date(row["signal_date"]),
                                  "target_notional":target,"entry_price":px,"lot_feasible":True,"executed":False})
                continue

            sh,notional,err=chosen
            fee=notional*half
            if notional+fee>cash+1e-8:
                skips["CASH"]+=1; continue
            cash-=notional+fee
            gross_remaining=max(0.0,gross_remaining-notional)
            cash_remaining=max(0.0,cash/(1+half))

            positions[s]={
                "shares":sh,"entry_price":px,"entry_notional":notional,"entry_fee":fee,
                "signal_date":norm_date(row["signal_date"]),"entry_date":date,
                "exit_date":norm_date(row["exit_date"]),
                "exit_price":float(row["exit_adj_close"]) if pd.notna(row.get("exit_adj_close")) else None,
                "last_value_price":px,"target_notional":target,"relative_target_error":err
            }
            decisions.append({"symbol":s,"date":date,"signal_date":norm_date(row["signal_date"]),
                              "target_notional":target,"actual_notional":notional,
                              "relative_target_error":err,"lot_feasible":True,"executed":True,
                              "shares":sh,"entry_price":px})

        # CLOSE mark.
        for s,pos in list(positions.items()):
            cp=map_get(close_map,s,date,last_close.get(s,pos["last_value_price"]))
            if cp is None: raise RuntimeError(f"RET missing close mark {s} {date}")
            pos["last_value_price"]=cp; last_close[s]=cp

        # Exit ONLY the currently held signal's own predeclared exit_date.
        due=[s for s,pos in positions.items() if pos["exit_date"]==date]
        for s in sorted(due):
            pos=positions[s]
            px=pos["exit_price"] if pos["exit_price"] is not None else map_get(close_map,s,date,pos["last_value_price"])
            if px is None or px<=0: raise RuntimeError(f"RET missing exact exit price {s} {date}")
            proceeds=pos["shares"]*px; fee=proceeds*half; cash+=proceeds-fee
            pnl=proceeds-fee-pos["entry_notional"]-pos["entry_fee"]
            trades.append({
                "symbol":s,"signal_date":pos["signal_date"],"entry_date":pos["entry_date"],"exit_date":date,
                "shares":pos["shares"],"entry_price":pos["entry_price"],"exit_price":px,
                "entry_notional":pos["entry_notional"],"exit_notional":proceeds,"net_pnl":pnl,
                "net_return_on_entry_notional":pnl/pos["entry_notional"],
                "relative_target_error":pos["relative_target_error"],
            })
            del positions[s]

        close_mv=sum(p["shares"]*p["last_value_price"] for p in positions.values())
        equity=cash+close_mv
        curve.append({"date":date,"equity":equity,"cash":cash,"gross_market_value":close_mv,
                      "gross_weight":close_mv/equity if equity>0 else 0.0,
                      "active_positions":len(positions)})

    if positions: raise RuntimeError(f"RET positions remain {len(positions)}")
    dec=pd.DataFrame(decisions); tr=pd.DataFrame(trades); cv=pd.DataFrame(curve)
    return {
        "curve":cv,"trades_df":tr,"decisions":dec,"skip_counts":dict(skips),
        "entry_fidelity":lot_fidelity(lot_feasible_attempts,sizing_attempts),
        "sizing_attempts":int(sizing_attempts),
        "lot_feasible_attempts":int(lot_feasible_attempts),
        "executed_entries":int((dec["executed"]==True).sum()) if len(dec) and "executed" in dec else 0,
    }

def load_deep_inputs():
    from xtquant import xtdata
    from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter
    from research.deep_oversold_replication import normalize_history
    from research.deep_oversold_economic_gate import EconomicProfile, simulate_shared_equity
    events=pd.read_csv(DEEP_EVENTS,dtype={"signal_date":str,"entry_date":str,"exit_date":str,"symbol":str})
    for c in ("signal_date","entry_date","exit_date"): events[c]=events[c].map(norm_date)
    symbols=sorted(events["symbol"].unique())
    adapter=XtDataWatchlistAdapter(xtdata,AdapterConfig(sector="",bars=2000,dividend_type="front"))
    loaded=adapter.history(symbols+["000905.SH"])
    loaded={s:normalize_history(df) for s,df in loaded.items() if df is not None and not df.empty}
    missing=[s for s in symbols+["000905.SH"] if s not in loaded]
    if missing: raise RuntimeError(f"DEEP cached QMT history missing={len(missing)} sample={missing[:20]}")
    bench=loaded.pop("000905.SH")
    d0=min(events["entry_date"]); d1=max(events["exit_date"])
    dates=[norm_date(d) for d in bench.index if d0<=norm_date(d)<=d1]
    profile=EconomicProfile("G5_1PCT_30PCT",0.01,0.30)
    return events,loaded,dates,profile,simulate_shared_equity

def deep_sim_lot(events,histories,dates,cost,initial_equity):
    from research.deep_oversold_economic_gate import _price
    half=cost/2
    entries={d:g.sort_values("symbol").to_dict("records") for d,g in events.groupby("entry_date")}
    cash=float(initial_equity); positions={}; last_close={}
    curve=[]; trades=[]; decisions=[]; skips=Counter()
    sizing_attempts=0; lot_feasible_attempts=0

    for date in dates:
        open_mv=0.0
        for s,pos in positions.items():
            px=_price(histories,s,date,"open",last_close.get(s,pos["entry_price"]))
            open_mv += pos["shares"]*px
        eq_open=cash+open_mv
        gross_cap=POLICY.max_target_gross*eq_open
        available=min(max(0.0,gross_cap-open_mv),max(0.0,cash/(1+half)))
        new=[r for r in entries.get(date,[]) if r["symbol"] not in positions]

        if new:
            desired_each=POLICY.strategy_target_weight*eq_open
            desired_total=desired_each*len(new)
            alloc_total=min(desired_total,available)
            scale=alloc_total/desired_total if desired_total>0 else 0.0
            target_each=desired_each*scale
            remaining=available

            for row in new:
                s=str(row["symbol"])
                if target_each<=0 or remaining<=0:
                    skips["NO_CAPACITY"]+=1; continue
                px=float(row["entry_open"]) if pd.notna(row.get("entry_open")) else _price(histories,s,date,"open")

                sizing_attempts += 1
                pure=nearest_executable_shares(s,px,target_each,POLICY.max_relative_target_error,None)
                if pure is None:
                    skips["LOT_INFEASIBLE"]+=1
                    decisions.append({"symbol":s,"date":date,"signal_date":norm_date(row["signal_date"]),
                                      "target_notional":target_each,"entry_price":px,"lot_feasible":False,"executed":False})
                    continue
                lot_feasible_attempts += 1

                chosen=nearest_executable_shares(s,px,target_each,POLICY.max_relative_target_error,remaining)
                if chosen is None:
                    skips["LOT_FEASIBLE_BUT_CAPACITY_BLOCKED"]+=1
                    decisions.append({"symbol":s,"date":date,"signal_date":norm_date(row["signal_date"]),
                                      "target_notional":target_each,"entry_price":px,"lot_feasible":True,"executed":False})
                    continue
                sh,notional,err=chosen; fee=notional*half
                if notional+fee>cash+1e-8:
                    skips["CASH"]+=1; continue
                cash-=notional+fee; remaining=max(0.0,remaining-notional)
                positions[s]={
                    "shares":sh,"entry_price":px,"entry_date":date,
                    "exit_date":norm_date(row["exit_date"]),
                    "exit_price":float(row["exit_close"]) if pd.notna(row.get("exit_close")) else None,
                    "signal_date":norm_date(row["signal_date"]),"entry_notional":notional,"entry_fee":fee,
                    "last_value_price":px,"relative_target_error":err,"target_notional":target_each
                }
                decisions.append({"symbol":s,"date":date,"signal_date":norm_date(row["signal_date"]),
                                  "target_notional":target_each,"actual_notional":notional,
                                  "relative_target_error":err,"lot_feasible":True,"executed":True,
                                  "shares":sh,"entry_price":px})

        for s,pos in positions.items():
            cp=_price(histories,s,date,"close",last_close.get(s,pos["last_value_price"]))
            pos["last_value_price"]=cp; last_close[s]=cp

        due=[s for s,pos in positions.items() if pos["exit_date"]==date]
        for s in sorted(due):
            pos=positions[s]
            px=pos["exit_price"] if pos["exit_price"] is not None else _price(histories,s,date,"close",pos["last_value_price"])
            proceeds=pos["shares"]*px; fee=proceeds*half; cash+=proceeds-fee
            pnl=proceeds-fee-pos["entry_notional"]-pos["entry_fee"]
            trades.append({
                "symbol":s,"signal_date":pos["signal_date"],"entry_date":pos["entry_date"],"exit_date":date,
                "entry_notional":pos["entry_notional"],"exit_notional":proceeds,"net_pnl":pnl,
                "net_return_on_entry_notional":pnl/pos["entry_notional"],
                "shares":pos["shares"],"relative_target_error":pos["relative_target_error"]
            })
            del positions[s]

        close_mv=sum(p["shares"]*p["last_value_price"] for p in positions.values())
        equity=cash+close_mv
        curve.append({"date":date,"equity":equity,"cash":cash,"gross_market_value":close_mv,
                      "gross_weight":close_mv/equity if equity>0 else 0.0,
                      "active_positions":len(positions)})

    if positions: raise RuntimeError(f"DEEP positions remain {len(positions)}")
    dec=pd.DataFrame(decisions); tr=pd.DataFrame(trades); cv=pd.DataFrame(curve)
    return {
        "curve":cv,"trades_df":tr,"decisions":dec,"skip_counts":dict(skips),
        "entry_fidelity":lot_fidelity(lot_feasible_attempts,sizing_attempts),
        "sizing_attempts":int(sizing_attempts),
        "lot_feasible_attempts":int(lot_feasible_attempts),
        "executed_entries":int((dec["executed"]==True).sum()) if len(dec) and "executed" in dec else 0,
    }

def compare_curves_exact(a,b,tol=1e-10):
    aa=a[["date","equity"]].copy(); bb=b[["date","equity"]].copy()
    aa["date"]=aa["date"].map(norm_date); bb["date"]=bb["date"].map(norm_date)
    z=aa.merge(bb,on="date",suffixes=("_a","_b"))
    if len(z)!=len(aa) or len(z)!=len(bb): return {"pass":False,"rows_a":len(aa),"rows_b":len(bb),"common":len(z)}
    rel=((z.equity_a-z.equity_b).abs()/z.equity_a.abs().clip(lower=1)).max()
    return {"pass":bool(rel<=tol),"max_relative_equity_diff":float(rel),"rows":len(z)}

def run():
    p1=load_module(P1_SCRIPT,"px_p1_ret")
    q5,ret_calendar,open_map,close_map,panel=ret120_data(p1)
    # Original frozen P1 baseline.
    p1_res=p1.simulate(q5,ret_calendar,open_map,close_map,POLICY.main_cost,None)
    p1_curve,p1_trades,p1_diag,p1_metrics=normalize_result(p1_res,p1.curve_metrics)
    ref=json.loads(RET_SUMMARY.read_text(encoding="utf-8-sig"))["main_20bps"]["metrics"]
    ret_baseline_summary_check={
      "cagr":abs(p1_metrics["cagr"]-ref["cagr"])<=1e-12,
      "mdd":abs(p1_metrics["max_drawdown"]-ref["max_drawdown"])<=1e-12,
      "sharpe":abs(p1_metrics["sharpe"]-ref["sharpe"])<=1e-12,
    }

    ret_lot20=ret_sim_lot(q5,ret_calendar,open_map,close_map,POLICY.main_cost,POLICY.ret_initial)
    ret_lot50=ret_sim_lot(q5,ret_calendar,open_map,close_map,POLICY.stress_cost,POLICY.ret_initial)
    ret_base_metrics=p1_metrics
    ret_lot_metrics=curve_metrics(ret_lot20["curve"],POLICY.ret_initial)
    ret_lot50_metrics=curve_metrics(ret_lot50["curve"],POLICY.ret_initial)

    deep_events,histories,deep_dates,profile,deep_original=load_deep_inputs()
    deep_orig20=deep_original(deep_events,histories,deep_dates,profile,POLICY.main_cost,initial_equity=POLICY.deep_initial)
    deep_orig50=deep_original(deep_events,histories,deep_dates,profile,POLICY.stress_cost,initial_equity=POLICY.deep_initial)
    deep_frac_metrics={k:deep_orig20[k] for k in ("cagr","max_drawdown","sharpe")}
    deep_frozen_equiv={
      "trade_count":abs(int(deep_orig20["trades"])-FROZEN_DEEP["trades"])<=DEEP_EQUIV_THRESH["trade_count"],
      "cagr":abs(deep_orig20["cagr"]-FROZEN_DEEP["cagr"])<=DEEP_EQUIV_THRESH["cagr"],
      "mdd":abs(deep_orig20["max_drawdown"]-FROZEN_DEEP["max_drawdown"])<=DEEP_EQUIV_THRESH["max_drawdown"],
      "sharpe":abs(deep_orig20["sharpe"]-FROZEN_DEEP["sharpe"])<=DEEP_EQUIV_THRESH["sharpe"],
    }
    deep_lot20=deep_sim_lot(deep_events,histories,deep_dates,POLICY.main_cost,POLICY.deep_initial)
    deep_lot50=deep_sim_lot(deep_events,histories,deep_dates,POLICY.stress_cost,POLICY.deep_initial)
    deep_lot_metrics=curve_metrics(deep_lot20["curve"],POLICY.deep_initial)
    deep_lot50_metrics=curve_metrics(deep_lot50["curve"],POLICY.deep_initial)

    # 50/50 fixed initial-capital sleeves, no rebalance. Preserve common-start first-day return.
    combo_base=combine_fixed_initial_sleeves(
        deep_orig20["curve"],p1_curve,POLICY.total_pilot_capital,
        POLICY.deep_initial,1_000_000.0,
    )
    combo_lot=combine_fixed_initial_sleeves(
        deep_lot20["curve"],ret_lot20["curve"],POLICY.total_pilot_capital,
        POLICY.deep_initial,POLICY.ret_initial,
    )
    combo_base_m=curve_metrics(combo_base,POLICY.total_pilot_capital)
    combo_lot_m=curve_metrics(combo_lot,POLICY.total_pilot_capital)

    # Reproduce the already-frozen P3 common-period result before judging nearest-lot.
    p3_repro={
      "deep_cagr":abs(curve_metrics(
          combine_fixed_initial_sleeves(deep_orig20["curve"],p1_curve,POLICY.total_pilot_capital,
                                        POLICY.deep_initial,1_000_000.0),
          POLICY.total_pilot_capital
      )["cagr"]-P3_FROZEN_APPROX["combo_cagr"]) <= 1.0,  # placeholder overwritten below
    }
    # Explicit component/common-period checks using the same fixed-sleeve window.
    common_start=combo_base["date"].min(); common_end=combo_base["date"].max()
    def common_metrics(curve, original_initial):
        x=curve.copy()
        dc="date" if "date" in x.columns else "trade_date"
        x[dc]=x[dc].map(norm_date); x=x.sort_values(dc)
        prior=x[x[dc] < common_start]
        denom=float(prior.iloc[-1]["equity"]) if len(prior) else float(original_initial)
        y=x[(x[dc]>=common_start)&(x[dc]<=common_end)][[dc,"equity"]].copy()
        y["equity"]=y["equity"].astype(float)/denom*POLICY.total_pilot_capital
        y=y.rename(columns={dc:"date"})
        return curve_metrics(y,POLICY.total_pilot_capital)
    deep_common_base=common_metrics(deep_orig20["curve"],POLICY.deep_initial)
    ret_common_base=common_metrics(p1_curve,1_000_000.0)
    p3_repro={
      "deep_cagr":abs(deep_common_base["cagr"]-P3_FROZEN_APPROX["deep_cagr"])<=P3_REPRO_TOL["cagr"],
      "deep_mdd":abs(deep_common_base["max_drawdown"]-P3_FROZEN_APPROX["deep_mdd"])<=P3_REPRO_TOL["mdd"],
      "deep_sharpe":abs(deep_common_base["sharpe"]-P3_FROZEN_APPROX["deep_sharpe"])<=P3_REPRO_TOL["sharpe"],
      "ret_cagr":abs(ret_common_base["cagr"]-P3_FROZEN_APPROX["ret_cagr"])<=P3_REPRO_TOL["cagr"],
      "ret_mdd":abs(ret_common_base["max_drawdown"]-P3_FROZEN_APPROX["ret_mdd"])<=P3_REPRO_TOL["mdd"],
      "ret_sharpe":abs(ret_common_base["sharpe"]-P3_FROZEN_APPROX["ret_sharpe"])<=P3_REPRO_TOL["sharpe"],
      "combo_cagr":abs(combo_base_m["cagr"]-P3_FROZEN_APPROX["combo_cagr"])<=P3_REPRO_TOL["cagr"],
      "combo_mdd":abs(combo_base_m["max_drawdown"]-P3_FROZEN_APPROX["combo_mdd"])<=P3_REPRO_TOL["mdd"],
      "combo_sharpe":abs(combo_base_m["sharpe"]-P3_FROZEN_APPROX["combo_sharpe"])<=P3_REPRO_TOL["sharpe"],
    }

    deep_drift=drift_pass(
      {"cagr":deep_orig20["cagr"],"max_drawdown":deep_orig20["max_drawdown"],"sharpe":deep_orig20["sharpe"]},
      deep_lot_metrics)
    ret_drift=drift_pass(ret_base_metrics,ret_lot_metrics)
    combo_drift=drift_pass(combo_base_m,combo_lot_m)

    # Nearest-lot incremental value relative to Deep on the exact combined common period.
    # Deep-only common-period comparator on the exact Portfolio common window.
    deep_common_m=common_metrics(deep_lot20["curve"],POLICY.deep_initial)
    incr={
      "delta_cagr":combo_lot_m["cagr"]-deep_common_m["cagr"],
      "delta_max_drawdown":combo_lot_m["max_drawdown"]-deep_common_m["max_drawdown"],
      "delta_sharpe":combo_lot_m["sharpe"]-deep_common_m["sharpe"],
    }
    incr_checks={
      "delta_cagr_gt_0":incr["delta_cagr"]>0,
      "delta_sharpe_gt_0":incr["delta_sharpe"]>0,
      "delta_mdd_ge_0":incr["delta_max_drawdown"]>=0,
    }

    checks={
      "ret_frozen_fractional_baseline_reproduced":all(ret_baseline_summary_check.values()),
      "deep_current_fractional_within_frozen_economic_equivalence":all(deep_frozen_equiv.values()),
      "p3_fractional_50_50_baseline_reproduced":all(p3_repro.values()),
      "deep_entry_fidelity_ge_90pct":deep_lot20["entry_fidelity"]>=POLICY.fidelity_min,
      "ret_entry_fidelity_ge_90pct":ret_lot20["entry_fidelity"]>=POLICY.fidelity_min,
      "deep_economic_drift_within_guardrails":deep_drift["pass"],
      "ret_economic_drift_within_guardrails":ret_drift["pass"],
      "combined_economic_drift_within_guardrails":combo_drift["pass"],
      "deep_50bps_cagr_positive":deep_lot50_metrics["cagr"]>0,
      "ret_50bps_cagr_positive":ret_lot50_metrics["cagr"]>0,
      "nearest_lot_combined_incremental_strong_pass":all(incr_checks.values()),
    }
    passed=all(checks.values())
    result={
      "status":"PASS_G5_NEAREST_LOT_ECONOMIC_REPLAY_V0_1" if passed else "FAIL_G5_NEAREST_LOT_ECONOMIC_REPLAY_V0_1",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "implementation_version":"V0.1.1_HOTFIX",
      "hotfix_scope":["RET120_CALENDAR_CROP_TO_FROZEN_665","EXIT_MATCH_CURRENT_POSITION_OWN_EXIT_DATE",
                      "NON_TAUTOLOGICAL_LOT_FIDELITY","P3_FIXED_INITIAL_SLEEVE_BASELINE_REPRODUCTION"],
      "policy":POLICY.__dict__,
      "predeclared_guardrails":{
        "entry_fidelity_each_strategy":">=90%",
        "abs_cagr_drift":"<=0.005",
        "abs_mdd_drift":"<=0.005",
        "abs_sharpe_drift":"<=0.10",
        "stress_50bps_cagr":"positive for both",
        "combined_vs_deep":"delta CAGR>0, delta Sharpe>0, delta MDD>=0",
      },
      "input_hashes":{"p1_script":sha(P1_SCRIPT),"ret_events":sha(RET_EVENTS),"deep_events":sha(DEEP_EVENTS),"catalog":sha(CATALOG)},
      "baseline_validation":{"ret120":ret_baseline_summary_check,"deep":deep_frozen_equiv,
                             "p3_50_50":p3_repro,
                             "p3_recomputed":{"deep":deep_common_base,"ret120":ret_common_base,"combined":combo_base_m},
                             "p3_frozen_approx":P3_FROZEN_APPROX},
      "deep":{"fractional_20bps":{k:deep_orig20[k] for k in ("trades","cagr","max_drawdown","sharpe")},
              "nearest_20bps":{**deep_lot_metrics,"trades":len(deep_lot20["trades_df"]),"entry_fidelity":deep_lot20["entry_fidelity"],
                               "sizing_attempts":deep_lot20["sizing_attempts"],"lot_feasible_attempts":deep_lot20["lot_feasible_attempts"],
                               "skip_counts":deep_lot20["skip_counts"]},
              "nearest_50bps":deep_lot50_metrics,"drift_20bps":deep_drift},
      "ret120":{"fractional_20bps":ret_base_metrics,
                "nearest_20bps":{**ret_lot_metrics,"trades":len(ret_lot20["trades_df"]),"entry_fidelity":ret_lot20["entry_fidelity"],
                                 "sizing_attempts":ret_lot20["sizing_attempts"],"lot_feasible_attempts":ret_lot20["lot_feasible_attempts"],
                                 "skip_counts":ret_lot20["skip_counts"]},
                "nearest_50bps":ret_lot50_metrics,"drift_20bps":ret_drift},
      "portfolio_50_50":{"fractional_20bps":combo_base_m,"nearest_20bps":combo_lot_m,
                         "drift_20bps":combo_drift,"nearest_vs_deep_incremental":incr,
                         "incremental_checks":incr_checks},
      "checks":checks,
      "g5_decision":"PASS_300W_NEAREST_LOT_CANDIDATE_FOR_POLICY_FREEZE" if passed else "KEEP_STRICT_ROUND_DOWN_POLICY; 300W_NEAREST_LOT_NOT_QUALIFIED",
      "safety":{"qmt_read_only_no_refresh":True,"broker":False,"production_db":False,"research_only":True},
    }
    OUT.mkdir(parents=True,exist_ok=True)
    deep_lot20["curve"].to_csv(OUT/"deep_nearest_20bps_curve.csv",index=False)
    deep_lot20["trades_df"].to_csv(OUT/"deep_nearest_20bps_trades.csv",index=False)
    ret_lot20["curve"].to_csv(OUT/"ret120_nearest_20bps_curve.csv",index=False)
    ret_lot20["trades_df"].to_csv(OUT/"ret120_nearest_20bps_trades.csv",index=False)
    combo_lot.to_csv(OUT/"portfolio_50_50_nearest_20bps_curve.csv",index=False)
    write_json_atomic(OUT/"g5_nearest_lot_economic_replay_v01.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    print(f"OUTPUT: {OUT}")
    return 0 if passed else 4

def check():
    required=[P1_SCRIPT,RET_EVENTS,RET_SUMMARY,DEEP_EVENTS,CATALOG]
    x={"status":"PASS_G5_NEAREST_LOT_ECONOMIC_REPLAY_V0_1_CHECK" if all(p.exists() for p in required) else "BLOCKED_G5_NEAREST_LOT_ECONOMIC_REPLAY_V0_1_CHECK",
       "required":{str(p):p.exists() for p in required},"policy":POLICY.__dict__,
       "qmt_history_policy":"READ CACHE ONLY; NO refresh/download","broker":False,"production_db":False}
    print(json.dumps(x,ensure_ascii=False,indent=2)); return 0 if x["status"].startswith("PASS") else 3

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"],nargs="?",default="check")
    a=p.parse_args()
    try:return check() if a.command=="check" else run()
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        return 3
if __name__=="__main__":raise SystemExit(main())
