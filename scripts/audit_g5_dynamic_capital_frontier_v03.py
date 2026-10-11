from __future__ import annotations
import argparse, importlib.util, json, os, sys, hashlib
from pathlib import Path
from datetime import datetime
import pandas as pd

PROJECT_ROOT=Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0,str(PROJECT_ROOT))

from research.g5_4m_nearest_lot_economic_replay import (
    G5ReplayPolicy, POLICY as BASE_POLICY, curve_metrics,
    combine_fixed_initial_sleeves, drift_pass, norm_date,
)

REPLAY_SCRIPT=PROJECT_ROOT/"scripts/audit_g5_4m_nearest_lot_economic_replay_v02.py"
OUT=Path(r"D:\positionx_data\research\pilot_live_readiness\g5_dynamic_capital_frontier_v0_3")
CAPITAL_GRID=[4_000_000,5_000_000,6_000_000,8_000_000,10_000_000,12_000_000,15_000_000,20_000_000]

def load_module(path:Path,name:str):
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None: raise RuntimeError(f"cannot import {path}")
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def write_json_atomic(path:Path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
    os.replace(tmp,path)

def common_metrics(curve:pd.DataFrame, original_initial:float, common_start:str, common_end:str, report_initial:float):
    x=curve.copy()
    dc="date" if "date" in x.columns else "trade_date"
    x[dc]=x[dc].map(norm_date); x=x.sort_values(dc)
    prior=x[x[dc] < common_start]
    denom=float(prior.iloc[-1]["equity"]) if len(prior) else float(original_initial)
    y=x[(x[dc]>=common_start)&(x[dc]<=common_end)][[dc,"equity"]].copy()
    y["equity"]=y["equity"].astype(float)/denom*report_initial
    y=y.rename(columns={dc:"date"})
    return curve_metrics(y,report_initial)

def preflight(mod):
    # Reproduce immutable baselines once before scanning capital.
    p1=mod.load_module(mod.P1_SCRIPT,"px_p1_frontier")
    q5,ret_calendar,open_map,close_map,panel=mod.ret120_data(p1)
    p1_res=p1.simulate(q5,ret_calendar,open_map,close_map,BASE_POLICY.main_cost,None)
    p1_curve,p1_trades,p1_diag,p1_metrics=mod.normalize_result(p1_res,p1.curve_metrics)
    ref=json.loads(mod.RET_SUMMARY.read_text(encoding="utf-8-sig"))["main_20bps"]["metrics"]
    ret_ok=(
        abs(p1_metrics["cagr"]-ref["cagr"])<=1e-12 and
        abs(p1_metrics["max_drawdown"]-ref["max_drawdown"])<=1e-12 and
        abs(p1_metrics["sharpe"]-ref["sharpe"])<=1e-12
    )

    deep_events,histories,deep_dates,profile,deep_original=mod.load_deep_inputs()
    deep_orig20=deep_original(deep_events,histories,deep_dates,profile,BASE_POLICY.main_cost,initial_equity=2_000_000.0)
    frozen=mod.FROZEN_DEEP; th=mod.DEEP_EQUIV_THRESH
    deep_ok=(
        abs(int(deep_orig20["trades"])-frozen["trades"])<=th["trade_count"] and
        abs(deep_orig20["cagr"]-frozen["cagr"])<=th["cagr"] and
        abs(deep_orig20["max_drawdown"]-frozen["max_drawdown"])<=th["max_drawdown"] and
        abs(deep_orig20["sharpe"]-frozen["sharpe"])<=th["sharpe"]
    )

    combo_base=combine_fixed_initial_sleeves(
        deep_orig20["curve"],p1_curve,4_000_000.0,2_000_000.0,1_000_000.0
    )
    combo_base_m=curve_metrics(combo_base,4_000_000.0)
    common_start=combo_base["date"].min(); common_end=combo_base["date"].max()
    deep_common=common_metrics(deep_orig20["curve"],2_000_000.0,common_start,common_end,4_000_000.0)
    ret_common=common_metrics(p1_curve,1_000_000.0,common_start,common_end,4_000_000.0)

    p3=mod.P3_FROZEN_APPROX; tol=mod.P3_REPRO_TOL
    p3_checks={
      "deep_cagr":abs(deep_common["cagr"]-p3["deep_cagr"])<=tol["cagr"],
      "deep_mdd":abs(deep_common["max_drawdown"]-p3["deep_mdd"])<=tol["mdd"],
      "deep_sharpe":abs(deep_common["sharpe"]-p3["deep_sharpe"])<=tol["sharpe"],
      "ret_cagr":abs(ret_common["cagr"]-p3["ret_cagr"])<=tol["cagr"],
      "ret_mdd":abs(ret_common["max_drawdown"]-p3["ret_mdd"])<=tol["mdd"],
      "ret_sharpe":abs(ret_common["sharpe"]-p3["ret_sharpe"])<=tol["sharpe"],
      "combo_cagr":abs(combo_base_m["cagr"]-p3["combo_cagr"])<=tol["cagr"],
      "combo_mdd":abs(combo_base_m["max_drawdown"]-p3["combo_mdd"])<=tol["mdd"],
      "combo_sharpe":abs(combo_base_m["sharpe"]-p3["combo_sharpe"])<=tol["sharpe"],
    }
    if not (ret_ok and deep_ok and all(p3_checks.values())):
        raise RuntimeError(f"BASELINE_REPRODUCTION_FAILED ret={ret_ok} deep={deep_ok} p3={p3_checks}")
    return {
      "p1":p1,"q5":q5,"ret_calendar":ret_calendar,"open_map":open_map,"close_map":close_map,
      "p1_curve":p1_curve,"p1_metrics":p1_metrics,
      "deep_events":deep_events,"histories":histories,"deep_dates":deep_dates,
      "profile":profile,"deep_original":deep_original,"deep_orig20":deep_orig20,
      "common_start":common_start,"common_end":common_end,
      "baseline":{"ret120":ret_ok,"deep":deep_ok,"p3_50_50":p3_checks,
                  "p3_recomputed":{"deep":deep_common,"ret120":ret_common,"combined":combo_base_m}}
    }

def policy_for(capital:int):
    return G5ReplayPolicy(
      policy_id=f"G5_DYNAMIC_CAPITAL_{capital}_NEAREST_LOT_V0_3",
      total_pilot_capital=float(capital),
      deep_sleeve_weight=BASE_POLICY.deep_sleeve_weight,
      ret120_sleeve_weight=BASE_POLICY.ret120_sleeve_weight,
      strategy_target_weight=BASE_POLICY.strategy_target_weight,
      max_target_gross=BASE_POLICY.max_target_gross,
      ret120_max_positions=BASE_POLICY.ret120_max_positions,
      max_relative_target_error=BASE_POLICY.max_relative_target_error,
      fidelity_min=BASE_POLICY.fidelity_min,
      max_abs_cagr_drift=BASE_POLICY.max_abs_cagr_drift,
      max_abs_mdd_drift=BASE_POLICY.max_abs_mdd_drift,
      max_abs_sharpe_drift=BASE_POLICY.max_abs_sharpe_drift,
      main_cost=BASE_POLICY.main_cost,
      stress_cost=BASE_POLICY.stress_cost,
    )

def evaluate_capital(mod,ctx,capital:int):
    pol=policy_for(capital)
    mod.POLICY=pol

    # Fractional baselines are scale-invariant; use existing curves/metrics.
    deep_base=ctx["deep_orig20"]
    ret_base_m=ctx["p1_metrics"]

    deep20=mod.deep_sim_lot(ctx["deep_events"],ctx["histories"],ctx["deep_dates"],pol.main_cost,pol.deep_initial)
    ret20=mod.ret_sim_lot(ctx["q5"],ctx["ret_calendar"],ctx["open_map"],ctx["close_map"],pol.main_cost,pol.ret_initial)

    deep20m=curve_metrics(deep20["curve"],pol.deep_initial)
    ret20m=curve_metrics(ret20["curve"],pol.ret_initial)

    combo_base=combine_fixed_initial_sleeves(
        deep_base["curve"],ctx["p1_curve"],pol.total_pilot_capital,2_000_000.0,1_000_000.0
    )
    combo20=combine_fixed_initial_sleeves(
        deep20["curve"],ret20["curve"],pol.total_pilot_capital,pol.deep_initial,pol.ret_initial
    )
    combo_base_m=curve_metrics(combo_base,pol.total_pilot_capital)
    combo20m=curve_metrics(combo20,pol.total_pilot_capital)

    deep_drift=drift_pass(
      {"cagr":deep_base["cagr"],"max_drawdown":deep_base["max_drawdown"],"sharpe":deep_base["sharpe"]},
      deep20m,pol)
    ret_drift=drift_pass(ret_base_m,ret20m,pol)
    combo_drift=drift_pass(combo_base_m,combo20m,pol)

    common_start=combo20["date"].min(); common_end=combo20["date"].max()
    deep_common_lot=common_metrics(deep20["curve"],pol.deep_initial,common_start,common_end,pol.total_pilot_capital)
    incr={
      "delta_cagr":combo20m["cagr"]-deep_common_lot["cagr"],
      "delta_max_drawdown":combo20m["max_drawdown"]-deep_common_lot["max_drawdown"],
      "delta_sharpe":combo20m["sharpe"]-deep_common_lot["sharpe"],
    }
    incr_ok=incr["delta_cagr"]>0 and incr["delta_sharpe"]>0 and incr["delta_max_drawdown"]>=0

    core_checks={
      "deep_fidelity_ge_90pct":deep20["entry_fidelity"]>=pol.fidelity_min,
      "ret_fidelity_ge_90pct":ret20["entry_fidelity"]>=pol.fidelity_min,
      "deep_20bp_drift_pass":deep_drift["pass"],
      "ret_20bp_drift_pass":ret_drift["pass"],
      "combo_20bp_drift_pass":combo_drift["pass"],
      "combo_incremental_vs_deep_pass":incr_ok,
    }

    row={
      "capital":capital,
      "deep":{"entry_fidelity":deep20["entry_fidelity"],"sizing_attempts":deep20["sizing_attempts"],
              "lot_feasible_attempts":deep20["lot_feasible_attempts"],"trades":len(deep20["trades_df"]),
              "metrics_20bps":deep20m,"drift_20bps":deep_drift,"skip_counts":deep20["skip_counts"]},
      "ret120":{"entry_fidelity":ret20["entry_fidelity"],"sizing_attempts":ret20["sizing_attempts"],
                "lot_feasible_attempts":ret20["lot_feasible_attempts"],"trades":len(ret20["trades_df"]),
                "metrics_20bps":ret20m,"drift_20bps":ret_drift,"skip_counts":ret20["skip_counts"]},
      "portfolio_50_50":{"metrics_20bps":combo20m,"drift_20bps":combo_drift,
                         "incremental_vs_deep":incr},
      "core_checks":core_checks,
      "core_pass":all(core_checks.values()),
    }

    # Only a core-passing capital earns the costlier stress replay.
    if row["core_pass"]:
        deep50=mod.deep_sim_lot(ctx["deep_events"],ctx["histories"],ctx["deep_dates"],pol.stress_cost,pol.deep_initial)
        ret50=mod.ret_sim_lot(ctx["q5"],ctx["ret_calendar"],ctx["open_map"],ctx["close_map"],pol.stress_cost,pol.ret_initial)
        deep50m=curve_metrics(deep50["curve"],pol.deep_initial)
        ret50m=curve_metrics(ret50["curve"],pol.ret_initial)
        stress={"deep_50bps_cagr_positive":deep50m["cagr"]>0,
                "ret_50bps_cagr_positive":ret50m["cagr"]>0}
        row["stress_50bps"]={"deep":deep50m,"ret120":ret50m,"checks":stress}
        row["full_pass"]=all(stress.values())
    else:
        row["stress_50bps"]="NOT_RUN_CORE_FAILED"
        row["full_pass"]=False
    return row

def check():
    req=[REPLAY_SCRIPT,PROJECT_ROOT/"research/g5_4m_nearest_lot_economic_replay.py"]
    x={"status":"PASS_G5_DYNAMIC_CAPITAL_FRONTIER_V0_3_CHECK" if all(p.exists() for p in req) else "BLOCKED_G5_DYNAMIC_CAPITAL_FRONTIER_V0_3_CHECK",
       "capital_grid":CAPITAL_GRID,
       "only_variable":"total_pilot_capital",
       "frozen_rules":{
         "sleeves":"50/50 initial capital; no rebalance",
         "target_weight":0.01,"max_target_gross":0.30,
         "nearest_lot_tolerance":"+/-25%","fidelity_min":0.90,
         "max_abs_cagr_drift":0.005,"max_abs_mdd_drift":0.005,"max_abs_sharpe_drift":0.10,
         "stress":"both 50bps CAGR >0",
         "incremental":"50/50 vs Deep dCAGR>0 dSharpe>0 dMDD>=0",
       },
       "selection_rule":"scan ascending; first FULL PASS is minimum tested dynamic capital; stop after first FULL PASS",
       "qmt":"cached history read only; no refresh/download","broker":False,"production_db":False}
    print(json.dumps(x,ensure_ascii=False,indent=2))
    return 0 if x["status"].startswith("PASS") else 3

def run():
    mod=load_module(REPLAY_SCRIPT,"px_g5_replay_v02")
    ctx=preflight(mod)
    rows=[]; first=None
    for cap in CAPITAL_GRID:
        print(f"[G5 FRONTIER] capital={cap:,}")
        row=evaluate_capital(mod,ctx,cap)
        rows.append(row)
        print(json.dumps({
          "capital":cap,
          "deep_fidelity":row["deep"]["entry_fidelity"],
          "ret120_fidelity":row["ret120"]["entry_fidelity"],
          "core_pass":row["core_pass"],
          "full_pass":row["full_pass"],
          "failed_core":[k for k,v in row["core_checks"].items() if not v],
        },ensure_ascii=False))
        if row["full_pass"]:
            first=cap
            break

    result={
      "status":"PASS_G5_DYNAMIC_CAPITAL_FRONTIER_V0_3" if first is not None else "NO_PASS_IN_PREDECLARED_CAPITAL_GRID_G5_DYNAMIC_FRONTIER_V0_3",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "capital_grid_predeclared":CAPITAL_GRID,
      "selection_rule":"ascending; stop at first full pass",
      "baseline_validation":ctx["baseline"],
      "tested":rows,
      "minimum_tested_dynamic_capital_full_pass":first,
      "decision":(
        f"CANDIDATE_G5_CAPITAL_FLOOR_RMB_{first}" if first is not None
        else "DO_NOT_RELAX_G5; CONSIDER_STRUCTURAL_SIZING_POLICY_REDESIGN_OR_LARGER_CAPITAL"
      ),
      "note":"Prior static 4.0m first-pass / 6.2048m robust-floor figures are retained as static references only and are not G5 qualification evidence.",
      "safety":{"research_only":True,"qmt_cache_read_only_no_refresh":True,"broker":False,"production_db":False}
    }
    OUT.mkdir(parents=True,exist_ok=True)
    write_json_atomic(OUT/"g5_dynamic_capital_frontier_v03.json",result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    print(f"OUTPUT: {OUT}")
    return 0 if first is not None else 4

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"],nargs="?",default="check")
    a=p.parse_args()
    try:return check() if a.command=="check" else run()
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        return 3
if __name__=="__main__":raise SystemExit(main())
