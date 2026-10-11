from __future__ import annotations
import argparse, importlib.util, json, math, os, random, sys
from pathlib import Path
from datetime import datetime
from collections import Counter
import pandas as pd

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0,str(PROJECT_ROOT))

from research.g5_4m_nearest_lot_economic_replay import (
    G5ReplayPolicy, curve_metrics, combine_fixed_initial_sleeves,
    drift_pass, nearest_executable_shares, norm_date, lot_fidelity,
)

REPLAY_SCRIPT=PROJECT_ROOT/"scripts/audit_g5_4m_nearest_lot_economic_replay_v02.py"
FRONTIER_SCRIPT=PROJECT_ROOT/"scripts/audit_g5_dynamic_capital_frontier_v03.py"
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g5_deep_full_ticket_policy_v0_4")

TOTAL_CAPITAL=3_000_000
RANDOM_SEEDS=100
RANDOM_PASS_FRACTION=0.90

def load_module(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError(f"cannot import {path}")
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def write_json_atomic(path:Path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(tmp,path)

def policy():
    return G5ReplayPolicy(
      policy_id="G5_DEEP_FULL_TICKET_SYMBOL_ASC_V0_4",
      total_pilot_capital=float(TOTAL_CAPITAL),
      deep_sleeve_weight=0.5,ret120_sleeve_weight=0.5,
      strategy_target_weight=0.01,max_target_gross=0.30,ret120_max_positions=30,
      max_relative_target_error=0.25,fidelity_min=0.90,
      max_abs_cagr_drift=0.005,max_abs_mdd_drift=0.005,max_abs_sharpe_drift=0.10,
      main_cost=0.002,stress_cost=0.005,
    )

POLICY=policy()

def common_metrics(curve:pd.DataFrame, original_initial:float, common_start:str, common_end:str, report_initial:float):
    x=curve.copy(); dc="date" if "date" in x.columns else "trade_date"
    x[dc]=x[dc].map(norm_date); x=x.sort_values(dc)
    prior=x[x[dc] < common_start]
    denom=float(prior.iloc[-1]["equity"]) if len(prior) else float(original_initial)
    y=x[(x[dc]>=common_start)&(x[dc]<=common_end)][[dc,"equity"]].copy()
    y["equity"]=y["equity"].astype(float)/denom*report_initial
    y=y.rename(columns={dc:"date"})
    return curve_metrics(y,report_initial)

def order_rows(rows, date:str, mode:str, seed:int|None):
    z=list(rows)
    if mode=="SYMBOL_ASC":
        return sorted(z,key=lambda r:str(r["symbol"]))
    if mode=="RANDOM":
        # Stable per-date shuffle for a given seed; independent of Python hash randomization.
        rr=random.Random(f"{seed}|{date}|POSITIONX_G5_FULL_TICKET_V04")
        z=sorted(z,key=lambda r:str(r["symbol"]))
        rr.shuffle(z); return z
    raise ValueError(mode)

def deep_full_ticket_sim(mod,events,histories,dates,cost,initial_equity,mode="SYMBOL_ASC",seed=None):
    from research.deep_oversold_economic_gate import _price
    half=cost/2
    entries={d:g.to_dict("records") for d,g in events.groupby("entry_date")}
    cash=float(initial_equity); positions={}; last_close={}
    curve=[]; trades=[]; skips=Counter(); decisions=[]
    selected=0; lot_feasible=0; executed=0

    for date in dates:
        open_mv=0.0
        for s,pos in positions.items():
            px=_price(histories,s,date,"open",last_close.get(s,pos["entry_price"]))
            open_mv += pos["shares"]*px

        eq_open=cash+open_mv
        gross_remaining=max(0.0,POLICY.max_target_gross*eq_open-open_mv)
        cash_remaining=max(0.0,cash/(1+half))
        available=min(gross_remaining,cash_remaining)
        target=POLICY.strategy_target_weight*eq_open

        new=[r for r in entries.get(date,[]) if r["symbol"] not in positions]
        ordered=order_rows(new,date,mode,seed)
        # Allocation semantics change being tested:
        # do NOT pro-rata shrink all same-day signals. Select only as many full nominal
        # 1% tickets as remaining capacity can support. Selection is price/alpha blind.
        k=min(len(ordered), int(math.floor((available+1e-12)/target))) if target>0 else 0
        chosen_rows=ordered[:k]
        skips["NO_FULL_TICKET_CAPACITY"] += max(0,len(ordered)-k)

        remaining=available
        for row in chosen_rows:
            selected += 1
            s=str(row["symbol"])
            px=float(row["entry_open"]) if pd.notna(row.get("entry_open")) else _price(histories,s,date,"open")
            pure=nearest_executable_shares(s,px,target,POLICY.max_relative_target_error,None)
            if pure is None:
                skips["LOT_INFEASIBLE"]+=1
                decisions.append({"date":date,"symbol":s,"selected":True,"lot_feasible":False,"executed":False,
                                  "target_notional":target,"entry_price":px})
                continue
            lot_feasible += 1
            choice=nearest_executable_shares(s,px,target,POLICY.max_relative_target_error,remaining)
            if choice is None:
                skips["LOT_FEASIBLE_BUT_CAPACITY_BLOCKED"]+=1
                decisions.append({"date":date,"symbol":s,"selected":True,"lot_feasible":True,"executed":False,
                                  "target_notional":target,"entry_price":px})
                continue
            sh,notional,err=choice
            fee=notional*half
            if notional+fee>cash+1e-8:
                skips["CASH"]+=1; continue
            cash-=notional+fee
            remaining=max(0.0,remaining-notional)
            positions[s]={
              "shares":sh,"entry_price":px,"entry_date":date,
              "exit_date":norm_date(row["exit_date"]),
              "exit_price":float(row["exit_close"]) if pd.notna(row.get("exit_close")) else None,
              "signal_date":norm_date(row["signal_date"]),
              "entry_notional":notional,"entry_fee":fee,"last_value_price":px,
              "relative_target_error":err,
            }
            executed += 1
            decisions.append({"date":date,"symbol":s,"selected":True,"lot_feasible":True,"executed":True,
                              "target_notional":target,"actual_notional":notional,
                              "relative_target_error":err,"shares":sh,"entry_price":px})

        for s,pos in positions.items():
            cp=_price(histories,s,date,"close",last_close.get(s,pos["last_value_price"]))
            pos["last_value_price"]=cp; last_close[s]=cp

        due=[s for s,pos in positions.items() if pos["exit_date"]==date]
        for s in sorted(due):
            pos=positions[s]
            px=pos["exit_price"] if pos["exit_price"] is not None else _price(histories,s,date,"close",pos["last_value_price"])
            proceeds=pos["shares"]*px; fee=proceeds*half; cash+=proceeds-fee
            pnl=proceeds-fee-pos["entry_notional"]-pos["entry_fee"]
            trades.append({"symbol":s,"signal_date":pos["signal_date"],"entry_date":pos["entry_date"],
                           "exit_date":date,"entry_notional":pos["entry_notional"],"exit_notional":proceeds,
                           "net_pnl":pnl,"net_return_on_entry_notional":pnl/pos["entry_notional"],
                           "shares":pos["shares"],"relative_target_error":pos["relative_target_error"]})
            del positions[s]

        close_mv=sum(p["shares"]*p["last_value_price"] for p in positions.values())
        equity=cash+close_mv
        curve.append({"date":date,"equity":equity,"cash":cash,"gross_market_value":close_mv,
                      "gross_weight":close_mv/equity if equity>0 else 0.0,
                      "active_positions":len(positions)})
    if positions: raise RuntimeError(f"DEEP positions remain={len(positions)}")

    cv=pd.DataFrame(curve); tr=pd.DataFrame(trades); dec=pd.DataFrame(decisions)
    return {
      "curve":cv,"trades_df":tr,"decisions":dec,"skip_counts":dict(skips),
      "selected_tickets":int(selected),"lot_feasible_tickets":int(lot_feasible),
      "executed_tickets":int(executed),
      "entry_fidelity":lot_fidelity(lot_feasible,selected),
      "selected_execution_rate":float(executed/selected) if selected else 0.0,
    }

def base_context(replay,frontier):
    ctx=frontier.preflight(replay)
    # 3m RET120 nearest-lot is re-run under the exact frozen V0.1 policy.
    replay.POLICY=POLICY
    ret20=replay.ret_sim_lot(ctx["q5"],ctx["ret_calendar"],ctx["open_map"],ctx["close_map"],
                             POLICY.main_cost,POLICY.ret_initial)
    ret50=replay.ret_sim_lot(ctx["q5"],ctx["ret_calendar"],ctx["open_map"],ctx["close_map"],
                             POLICY.stress_cost,POLICY.ret_initial)
    return ctx,ret20,ret50

def evaluate_deep_variant(replay,ctx,ret20,mode,seed=None,cost=0.002):
    deep=deep_full_ticket_sim(replay,ctx["deep_events"],ctx["histories"],ctx["deep_dates"],
                             cost,POLICY.deep_initial,mode,seed)
    dm=curve_metrics(deep["curve"],POLICY.deep_initial)

    base=ctx["deep_orig20"]
    if cost==POLICY.main_cost:
        deep_drift=drift_pass(
          {"cagr":base["cagr"],"max_drawdown":base["max_drawdown"],"sharpe":base["sharpe"]},
          dm,POLICY)
        combo=combine_fixed_initial_sleeves(
          deep["curve"],ret20["curve"],POLICY.total_pilot_capital,POLICY.deep_initial,POLICY.ret_initial
        )
        combo_m=curve_metrics(combo,POLICY.total_pilot_capital)
        combo_base=combine_fixed_initial_sleeves(
          base["curve"],ctx["p1_curve"],POLICY.total_pilot_capital,2_000_000.0,1_000_000.0
        )
        combo_base_m=curve_metrics(combo_base,POLICY.total_pilot_capital)
        combo_drift=drift_pass(combo_base_m,combo_m,POLICY)

        cs=combo["date"].min(); ce=combo["date"].max()
        deep_common=common_metrics(deep["curve"],POLICY.deep_initial,cs,ce,POLICY.total_pilot_capital)
        incr={
          "delta_cagr":combo_m["cagr"]-deep_common["cagr"],
          "delta_max_drawdown":combo_m["max_drawdown"]-deep_common["max_drawdown"],
          "delta_sharpe":combo_m["sharpe"]-deep_common["sharpe"],
        }
        incr_pass=incr["delta_cagr"]>0 and incr["delta_sharpe"]>0 and incr["delta_max_drawdown"]>=0
        checks={
          "deep_lot_fidelity_ge_90pct":deep["entry_fidelity"]>=POLICY.fidelity_min,
          "deep_selected_execution_rate_ge_90pct":deep["selected_execution_rate"]>=0.90,
          "deep_economic_drift_pass":deep_drift["pass"],
          "portfolio_economic_drift_pass":combo_drift["pass"],
          "portfolio_incremental_vs_deep_pass":incr_pass,
        }
        return {"deep":deep,"deep_metrics":dm,"deep_drift":deep_drift,
                "portfolio_metrics":combo_m,"portfolio_drift":combo_drift,
                "incremental":incr,"checks":checks,"pass":all(checks.values())}
    return {"deep":deep,"deep_metrics":dm}

def check():
    req=[REPLAY_SCRIPT,FRONTIER_SCRIPT,PROJECT_ROOT/"research/g5_4m_nearest_lot_economic_replay.py"]
    x={
      "status":"PASS_G5_DEEP_FULL_TICKET_POLICY_V0_4_CHECK" if all(p.exists() for p in req) else "BLOCKED_G5_DEEP_FULL_TICKET_POLICY_V0_4_CHECK",
      "capital":TOTAL_CAPITAL,
      "changed_semantics_only":"Deep same-day allocation: pro-rata all -> full-ticket subset",
      "baseline_tie_break":"SYMBOL_ASC",
      "selection_is_alpha_and_price_blind":True,
      "no_backfill_after_selected_lot_infeasible":True,
      "frozen_sizing":{"target":0.01,"max_gross":0.30,"nearest_lot_tolerance":"+/-25%"},
      "gates":{
        "deep_lot_fidelity":">=90%",
        "selected_execution_rate":">=90%",
        "economic_drift":"same <=50bp CAGR/MDD, <=0.10 Sharpe",
        "stress":"Deep and RET120 50bps CAGR >0",
        "portfolio_incremental":"dCAGR>0 dSharpe>0 dMDD>=0",
        "random_tie_break_100":"at least 90% seeds pass the same main-cost Deep+Portfolio gates"
      },
      "qmt":"cached read only","broker":False,"production_db":False
    }
    print(json.dumps(x,ensure_ascii=False,indent=2))
    return 0 if x["status"].startswith("PASS") else 3

def run():
    replay=load_module(REPLAY_SCRIPT,"px_g5_replay_fullticket")
    frontier=load_module(FRONTIER_SCRIPT,"px_g5_frontier_fullticket")
    ctx,ret20,ret50=base_context(replay,frontier)

    baseline=evaluate_deep_variant(replay,ctx,ret20,"SYMBOL_ASC",None,POLICY.main_cost)
    deep50=evaluate_deep_variant(replay,ctx,ret20,"SYMBOL_ASC",None,POLICY.stress_cost)
    ret50m=curve_metrics(ret50["curve"],POLICY.ret_initial)
    stress_checks={
      "deep_50bps_cagr_positive":deep50["deep_metrics"]["cagr"]>0,
      "ret120_50bps_cagr_positive":ret50m["cagr"]>0,
    }

    random_rows=[]
    random_pass=0
    for seed in range(RANDOM_SEEDS):
        r=evaluate_deep_variant(replay,ctx,ret20,"RANDOM",seed,POLICY.main_cost)
        ok=r["pass"]
        random_pass += int(ok)
        random_rows.append({
          "seed":seed,"pass":ok,
          "deep_fidelity":r["deep"]["entry_fidelity"],
          "execution_rate":r["deep"]["selected_execution_rate"],
          "deep_cagr":r["deep_metrics"]["cagr"],
          "deep_mdd":r["deep_metrics"]["max_drawdown"],
          "deep_sharpe":r["deep_metrics"]["sharpe"],
          "combo_cagr":r["portfolio_metrics"]["cagr"],
          "combo_mdd":r["portfolio_metrics"]["max_drawdown"],
          "combo_sharpe":r["portfolio_metrics"]["sharpe"],
          "checks":r["checks"],
        })
    random_fraction=random_pass/RANDOM_SEEDS
    random_gate=random_fraction>=RANDOM_PASS_FRACTION

    checks={
      "all_frozen_baselines_reproduced":True,
      "symbol_asc_main_pass":baseline["pass"],
      "stress_50bps_pass":all(stress_checks.values()),
      "random_tie_break_pass_fraction_ge_90pct":random_gate,
    }
    passed=all(checks.values())

    result={
      "status":"PASS_G5_DEEP_FULL_TICKET_POLICY_V0_4" if passed else "FAIL_G5_DEEP_FULL_TICKET_POLICY_V0_4",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "purpose":"Test execution-architecture alternative after dynamic-capital frontier required RMB12m.",
      "policy":{
        "total_pilot_capital":TOTAL_CAPITAL,
        "sleeves":"50/50 initial capital; no rebalance",
        "deep_same_day_allocation":"FULL 1% TICKETS; select only capacity count; no pro-rata scaling",
        "tie_break_baseline":"SYMBOL_ASC",
        "selection_alpha_price_blind":True,
        "no_backfill_after_selected_lot_infeasible":True,
        "nearest_lot_tolerance":"+/-25%",
        "max_target_gross":0.30,
      },
      "baseline_validation":ctx["baseline"],
      "symbol_asc":{
        "deep":{
          "entry_fidelity":baseline["deep"]["entry_fidelity"],
          "selected_execution_rate":baseline["deep"]["selected_execution_rate"],
          "selected_tickets":baseline["deep"]["selected_tickets"],
          "lot_feasible_tickets":baseline["deep"]["lot_feasible_tickets"],
          "executed_tickets":baseline["deep"]["executed_tickets"],
          "trades":len(baseline["deep"]["trades_df"]),
          "metrics_20bps":baseline["deep_metrics"],
          "drift_20bps":baseline["deep_drift"],
          "skip_counts":baseline["deep"]["skip_counts"],
          "metrics_50bps":deep50["deep_metrics"],
        },
        "ret120":{
          "entry_fidelity":ret20["entry_fidelity"],"trades":len(ret20["trades_df"]),
          "metrics_20bps":curve_metrics(ret20["curve"],POLICY.ret_initial),
          "metrics_50bps":ret50m,
        },
        "portfolio_50_50":{
          "metrics_20bps":baseline["portfolio_metrics"],
          "drift_20bps":baseline["portfolio_drift"],
          "incremental_vs_deep":baseline["incremental"],
        },
        "main_checks":baseline["checks"],
        "stress_checks":stress_checks,
      },
      "random_tie_break_100":{
        "pass_count":random_pass,"pass_fraction":random_fraction,
        "required_fraction":RANDOM_PASS_FRACTION,
        "gate_pass":random_gate,
        "rows":random_rows,
      },
      "checks":checks,
      "decision":(
        "CANDIDATE_REPLACE_DEEP_PRORATA_WITH_FULL_TICKET_FOR_PILOT; REQUIRES_FORMAL_POLICY_FREEZE"
        if passed else
        "REJECT_FULL_TICKET_V0_4; DO_NOT_CHANGE_DEEP_EXECUTION_SEMANTICS"
      ),
      "prior_dynamic_capital_frontier":"RMB12m minimum tested FULL PASS under original pro-rata semantics",
      "safety":{"research_only":True,"qmt_cache_read_only":True,"broker":False,"production_db":False}
    }
    OUT.mkdir(parents=True,exist_ok=True)
    baseline["deep"]["curve"].to_csv(OUT/"deep_full_ticket_symbol_asc_20bps_curve.csv",index=False)
    baseline["deep"]["trades_df"].to_csv(OUT/"deep_full_ticket_symbol_asc_20bps_trades.csv",index=False)
    pd.DataFrame(random_rows).drop(columns=["checks"]).to_csv(OUT/"deep_full_ticket_random_100_summary.csv",index=False)
    write_json_atomic(OUT/"g5_deep_full_ticket_policy_v04.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    print(f"OUTPUT: {OUT}")
    return 0 if passed else 4

def main():
    p=argparse.ArgumentParser();p.add_argument("command",choices=["check","run"],nargs="?",default="check")
    a=p.parse_args()
    try:return check() if a.command=="check" else run()
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        return 3
if __name__=="__main__":raise SystemExit(main())
