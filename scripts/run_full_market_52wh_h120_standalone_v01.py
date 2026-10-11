
from __future__ import annotations
import argparse, hashlib, json, math
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/full_market_52wh_h120_standalone_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def qpath(p):
    return str(Path(p)).replace("\\","/").replace("'","''")

def safe(v):
    if v is None or isinstance(v,(str,bool,int)): return v
    if isinstance(v,float): return v if math.isfinite(v) else None
    if isinstance(v,(np.integer,)): return int(v)
    if isinstance(v,(np.floating,)): return safe(float(v))
    if isinstance(v,pd.Timestamp): return v.strftime("%Y-%m-%d")
    if hasattr(v,"item"):
        try:return safe(v.item())
        except Exception:pass
    return str(v)

def json_write(path,obj):
    Path(path).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=safe),encoding="utf-8")

def date_str(v):
    return pd.Timestamp(v).strftime("%Y-%m-%d")

def deterministic_random_key(seed,date,row):
    raw=f"{seed}|{date}|{row['symbol']}|{date_str(row['signal_date'])}".encode()
    return hashlib.sha256(raw).hexdigest()

def pct_summary(vals):
    a=np.asarray([float(x) for x in vals if x is not None and math.isfinite(float(x))],dtype=float)
    if len(a)==0:return {"n":0}
    return {
      "n":int(len(a)),"min":float(np.min(a)),"p10":float(np.quantile(a,.10)),
      "p25":float(np.quantile(a,.25)),"median":float(np.quantile(a,.50)),
      "p75":float(np.quantile(a,.75)),"p90":float(np.quantile(a,.90)),
      "max":float(np.max(a)),"mean":float(np.mean(a))
    }

def load_phase1(cfg):
    p=Path(cfg["source"]["phase1_summary"])
    x=json.loads(p.read_text(encoding="utf-8"))
    return x

def validate_inputs(cfg):
    checks=[]
    for name,key,ekey in [
        ("phase1_summary","phase1_summary","expected_phase1_summary_sha256"),
        ("candidate_events","candidate_events","expected_candidate_events_sha256"),
        ("event_returns","event_returns","expected_event_returns_sha256"),
    ]:
        p=Path(cfg["source"][key]); exp=cfg["source"][ekey]
        actual=sha256_file(p) if p.exists() else None
        checks.append({"name":name,"path":str(p),"exists":p.exists(),
                       "sha256":actual,"expected_sha256":exp,"sha_match":actual==exp})
    for name,key in [("features","features"),("calendar","calendar")]:
        p=Path(cfg["source"][key])
        checks.append({"name":name,"path":str(p),"exists":p.exists(),
                       "sha256":sha256_file(p) if p.exists() else None})
    ok=all(r["exists"] and r.get("sha_match",True) for r in checks)
    return checks,ok

def phase1_gate_check(cfg,summary):
    d=summary.get("decision",{})
    c=cfg["phase1_contract"]
    return {
      "verdict":{"expected":c["required_verdict"],"actual":d.get("verdict"),
                 "pass":d.get("verdict")==c["required_verdict"]},
      "h120_pass":{"expected":bool(c["required_h120_pass"]),
                   "actual":bool(d.get("h120",{}).get("horizon_pass")),
                   "pass":bool(d.get("h120",{}).get("horizon_pass"))==bool(c["required_h120_pass"])},
      "h60_pass":{"expected":bool(c["required_h60_pass"]),
                  "actual":bool(d.get("h60",{}).get("horizon_pass")),
                  "pass":bool(d.get("h60",{}).get("horizon_pass"))==bool(c["required_h60_pass"])},
    }

def load_candidates(cfg):
    c=pd.read_parquet(cfg["source"]["candidate_events"])
    mask=(
      c["grp"].eq(cfg["phase1_contract"]["candidate_group"])
      & c["exclude_reason"].isna()
      & c["complete_h120_cohort"].fillna(False).astype(bool)
      & pd.to_numeric(c["entry_adj_open"],errors="coerce").gt(0)
      & pd.to_numeric(c["exit_adj_close_120"],errors="coerce").gt(0)
    )
    z=c[mask].copy()
    z=z[["symbol","signal_date","entry_date","exit_date_120",
         "entry_adj_open","exit_adj_close_120"]].rename(columns={
           "exit_date_120":"exit_date",
           "entry_adj_open":"entry_open",
           "exit_adj_close_120":"exit_close",
         })
    for d in ["signal_date","entry_date","exit_date"]:
        z[d]=pd.to_datetime(z[d]).dt.strftime("%Y-%m-%d")
    return z.sort_values(["entry_date","symbol","signal_date"]).reset_index(drop=True)

def build_price_panel(cfg,outdir,events):
    import duckdb
    p=outdir/"portfolio_price_panel.parquet"
    if p.exists():return p
    syms=pd.DataFrame({"symbol":sorted(events["symbol"].unique())})
    start=events["entry_date"].min(); end=events["exit_date"].max()
    con=duckdb.connect()
    try:
        con.register("syms",syms)
        f=qpath(cfg["source"]["features"]);dest=qpath(p)
        con.execute(f"""
        COPY (
          SELECT CAST(f.trade_date AS DATE) AS trade_date,
                 f.symbol,
                 CAST(f.adj_open AS DOUBLE) AS adj_open,
                 CAST(f.adj_close AS DOUBLE) AS adj_close
          FROM read_parquet('{f}') f
          INNER JOIN syms s ON s.symbol=f.symbol
          WHERE CAST(f.trade_date AS DATE) BETWEEN CAST('{start}' AS DATE) AND CAST('{end}' AS DATE)
          ORDER BY f.symbol,f.trade_date
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
    finally:con.close()
    return p

def load_histories(price_panel):
    z=pd.read_parquet(price_panel)
    z["trade_date"]=pd.to_datetime(z["trade_date"]).dt.strftime("%Y-%m-%d")
    out={}
    for sym,g in z.groupby("symbol",sort=False):
        h=g.set_index("trade_date")[["adj_open","adj_close"]].sort_index()
        out[str(sym)]=h
    return out

def load_calendar(cfg,events):
    cal=pd.read_parquet(cfg["source"]["calendar"],columns=["trade_date"])
    dates=pd.to_datetime(cal["trade_date"]).dt.strftime("%Y-%m-%d")
    start=events["entry_date"].min();end=events["exit_date"].max()
    return [d for d in dates.tolist() if start<=d<=end]

def lineage_check(events,histories):
    errs_entry=[];errs_exit=[];missing=0
    for r in events.to_dict("records"):
        h=histories.get(str(r["symbol"]))
        if h is None or r["entry_date"] not in h.index or r["exit_date"] not in h.index:
            missing+=1;continue
        ep=float(h.loc[r["entry_date"],"adj_open"])
        xp=float(h.loc[r["exit_date"],"adj_close"])
        for val,ref,acc in [
            (ep,float(r["entry_open"]),errs_entry),
            (xp,float(r["exit_close"]),errs_exit),
        ]:
            if not (math.isfinite(val) and math.isfinite(ref) and ref>0):
                acc.append(float("inf"))
            else:
                acc.append(abs(val/ref-1.0))
    mx=lambda a: max(a) if a else None
    return {"checked":len(events),"missing_history_keys":missing,
            "entry_max_rel_error":mx(errs_entry),
            "exit_max_rel_error":mx(errs_exit),
            "pass":missing==0 and (mx(errs_entry) or 0)<=1e-12 and (mx(errs_exit) or 0)<=1e-12}

def price(histories,symbol,date,col,fallback):
    h=histories.get(symbol)
    if h is not None and date in h.index:
        v=h.loc[date,col]
        if pd.notna(v) and float(v)>0:return float(v)
    return float(fallback)

def order_rows(rows,mode,seed,date):
    if mode=="SYMBOL_ASC":
        return sorted(rows,key=lambda r:(str(r["symbol"]),str(r["signal_date"])))
    return sorted(rows,key=lambda r:deterministic_random_key(seed,date,r))

def calendar_year_returns(curve,initial_equity):
    z=curve.copy()
    z["date"]=pd.to_datetime(z["date"])
    z["year"]=z["date"].dt.year
    result={}
    prior=float(initial_equity)
    for y,g in z.groupby("year",sort=True):
        end=float(g["equity"].iloc[-1])
        result[str(int(y))]=end/prior-1.0
        prior=end
    return result

def simulate(events,histories,dates,policy,cost,mode="SYMBOL_ASC",seed=0):
    half=float(cost)/2.0
    initial=float(policy["initial_equity"])
    cash=initial
    positions={}
    last_close={}
    curve=[];trades=[]
    skips=Counter()
    scaled_days=0;capacity_days=0;no_slot_dates=set()
    entry_notional_total=0.0;exit_notional_total=0.0

    entries_by={}
    for d,g in events.groupby("entry_date"):
        entries_by[str(d)]=g.to_dict("records")

    for date in dates:
        # Open mark BEFORE exits; close exits are not reusable at the same open.
        open_mv=0.0
        for sym,pos in positions.items():
            px=price(histories,sym,date,"adj_open",last_close.get(sym,pos["entry_price"]))
            open_mv+=pos["shares"]*px
        equity_open=cash+open_mv
        gross_cap=max(0.0,float(policy["max_target_gross"])*equity_open)
        remaining_gross=max(0.0,gross_cap-open_mv)
        remaining_cash=max(0.0,cash/(1.0+half))
        available=min(remaining_gross,remaining_cash)

        raw=order_rows(entries_by.get(date,[]),mode,seed,date)
        unique=[]
        for r in raw:
            if str(r["symbol"]) in positions:
                skips["DUPLICATE_WHILE_HELD"]+=1
            else:
                unique.append(r)

        slots=max(0,int(policy["max_positions"])-len(positions))
        selected=unique[:slots]
        if len(unique)>slots:
            n=len(unique)-slots
            skips["NO_SLOT"]+=n
            no_slot_dates.add(date)

        if selected:
            desired_each=float(policy["target_weight"])*equity_open
            desired_total=desired_each*len(selected)
            alloc_total=min(desired_total,available)
            scale=alloc_total/desired_total if desired_total>0 else 0.0
            if scale<0.999999:
                scaled_days+=1;capacity_days+=1
            if alloc_total<=0:
                skips["NO_GROSS_OR_CASH_CAPACITY"]+=len(selected)
            else:
                for r in selected:
                    sym=str(r["symbol"])
                    notional=desired_each*scale
                    if notional<=0:
                        skips["NO_GROSS_OR_CASH_CAPACITY"]+=1
                        continue
                    ep=float(r["entry_open"])
                    shares=notional/ep
                    fee=notional*half
                    if notional+fee>cash+1e-8:
                        raise AssertionError("cash invariant violated at entry")
                    cash-=notional+fee
                    positions[sym]={
                      "shares":shares,"entry_price":ep,"entry_date":date,
                      "exit_date":str(r["exit_date"]),"exit_price":float(r["exit_close"]),
                      "signal_date":str(r["signal_date"]),"entry_notional":notional,
                      "entry_fee":fee,"last_value_price":ep,
                    }
                    entry_notional_total+=notional

        # Close marks.
        for sym,pos in positions.items():
            px=price(histories,sym,date,"adj_close",last_close.get(sym,pos["last_value_price"]))
            pos["last_value_price"]=px;last_close[sym]=px

        # Exact S120 CLOSE exits, after same-day OPEN entries.
        exiting=[sym for sym,pos in positions.items() if pos["exit_date"]==date]
        for sym in sorted(exiting):
            pos=positions[sym]
            xp=float(pos["exit_price"])
            gross=pos["shares"]*xp
            fee=gross*half
            cash+=gross-fee
            pnl=gross-fee-pos["entry_notional"]-pos["entry_fee"]
            trades.append({
              "symbol":sym,"signal_date":pos["signal_date"],"entry_date":pos["entry_date"],
              "exit_date":date,"entry_notional":pos["entry_notional"],
              "exit_notional":gross,"net_pnl":pnl,
              "net_return_on_entry_notional":pnl/pos["entry_notional"]
            })
            exit_notional_total+=gross
            del positions[sym]

        close_mv=sum(pos["shares"]*pos["last_value_price"] for pos in positions.values())
        equity=cash+close_mv
        curve.append({"date":date,"equity":equity,"cash":cash,
                      "gross_market_value":close_mv,
                      "gross_weight":close_mv/equity if equity>0 else 0.0,
                      "active_positions":len(positions)})

    if positions:
        raise AssertionError(f"positions remain after final calendar date: {len(positions)}")

    curve=pd.DataFrame(curve);trades=pd.DataFrame(trades)
    eq=curve["equity"].astype(float)
    dr=eq.pct_change(fill_method=None).fillna(0.0)
    dd=eq/eq.cummax()-1.0
    years=max((pd.Timestamp(curve["date"].iloc[-1])-pd.Timestamp(curve["date"].iloc[0])).days/365.2425,1/365.2425)
    final=float(eq.iloc[-1]);total=final/initial-1.0
    cagr=(final/initial)**(1.0/years)-1.0
    std=float(dr.std(ddof=1))
    sharpe=float(dr.mean()/std*math.sqrt(252)) if std>0 else None
    avg_gross=float(curve["gross_weight"].mean())
    cyrs=calendar_year_returns(curve,initial)

    return {
      "metrics":{
        "sessions":len(curve),"start":str(curve["date"].iloc[0]),"end":str(curve["date"].iloc[-1]),
        "total_return":total,"cagr":cagr,"max_drawdown":float(dd.min()),"sharpe":sharpe,
        "avg_gross":avg_gross,"max_gross":float(curve["gross_weight"].max()),
        "avg_active_positions":float(curve["active_positions"].mean()),
        "max_active_positions":int(curve["active_positions"].max()),
        "invested_day_fraction":float((curve["active_positions"]>0).mean()),
        "capital_efficiency":cagr/avg_gross if avg_gross>0 else None,
        "calendar_year_returns":cyrs,
      },
      "diagnostics":{
        "candidate_signals":len(events),"entry_candidates_total":len(events),
        "executed_trades":len(trades),"coverage":len(trades)/len(events) if len(events) else None,
        "scaled_entry_days":scaled_days,"capacity_bind_days":capacity_days,
        "no_slot_days":len(no_slot_dates),"skip_counts":dict(skips),
        "trade_avg_net_return":float(trades["net_return_on_entry_notional"].mean()) if len(trades) else None,
        "trade_median_net_return":float(trades["net_return_on_entry_notional"].median()) if len(trades) else None,
        "trade_win_rate":float((trades["net_return_on_entry_notional"]>0).mean()) if len(trades) else None,
      },
      "curve":curve,"trades":trades
    }

def standalone_gate(main,stress,random_summary,cfg):
    g=cfg["standalone_gate"]
    years=main["metrics"]["calendar_year_returns"]
    positive=sum(float(v)>0 for v in years.values())
    checks={
      "cagr20_positive":main["metrics"]["cagr"]>0,
      "cagr50_positive":stress["metrics"]["cagr"]>0,
      "sharpe20_ge_0_5":main["metrics"]["sharpe"] is not None and main["metrics"]["sharpe"]>=float(g["sharpe20_min"]),
      "mdd20_within_20pct":main["metrics"]["max_drawdown"]>=float(g["mdd20_floor"]),
      "at_least_2_positive_calendar_years":positive>=int(g["positive_calendar_years_min"]),
      "random_seed_median_cagr_positive":random_summary["cagr"]["median"]>0,
      "random_seed_p10_cagr_positive":random_summary["cagr"]["p10"]>0,
    }
    return {"status":"PASS_STANDALONE_PORTFOLIO_GATE" if all(checks.values()) else "FAIL_STANDALONE_PORTFOLIO_GATE",
            "checks":checks,"positive_calendar_years":positive,
            "note":"PASS advances to same-family comparison with ret120; it is not Strategy #2 or Forward approval."}

def strip_sim(x):
    return {"metrics":x["metrics"],"diagnostics":x["diagnostics"]}

def check(cfg):
    inputs,ok=validate_inputs(cfg)
    phase=load_phase1(cfg) if ok else {}
    pg=phase1_gate_check(cfg,phase) if phase else {}
    ok=ok and pg and all(v["pass"] for v in pg.values())
    out={"status":"PASS_FULL_MARKET_52WH_H120_STANDALONE_V0_1_CHECK" if ok else "BLOCKED_FULL_MARKET_52WH_H120_STANDALONE_V0_1_CHECK",
         "inputs":inputs,"phase1_contract_check":pg,
         "portfolio_policy":cfg["portfolio_policy"],"standalone_gate":cfg["standalone_gate"],
         "safety":cfg["safety"]}
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(cfg,force=False):
    inputs,ok=validate_inputs(cfg)
    if not ok:raise RuntimeError("input existence/hash validation failed")
    phase=load_phase1(cfg);pg=phase1_gate_check(cfg,phase)
    if not all(v["pass"] for v in pg.values()):
        raise RuntimeError(f"phase1 contract mismatch: {pg}")

    outdir=Path(cfg["output_dir"]);outdir.mkdir(parents=True,exist_ok=True)
    if force:
        p=outdir/"portfolio_price_panel.parquet"
        if p.exists():p.unlink()

    events=load_candidates(cfg)
    if events.empty:raise RuntimeError("no eligible POS H120 candidates")
    price_panel=build_price_panel(cfg,outdir,events)
    histories=load_histories(price_panel)
    dates=load_calendar(cfg,events)
    lineage=lineage_check(events,histories)
    if not lineage["pass"]:raise RuntimeError(f"price lineage failed: {lineage}")

    pol=cfg["portfolio_policy"]
    main=simulate(events,histories,dates,pol,float(pol["main_round_trip_cost"]),"SYMBOL_ASC",0)
    stress=simulate(events,histories,dates,pol,float(pol["stress_round_trip_cost"]),"SYMBOL_ASC",0)

    random_rows=[]
    for seed in range(int(pol["random_seed_start"]),int(pol["random_seed_start"])+int(pol["random_same_day_tie_break_seeds"])):
        sim=simulate(events,histories,dates,pol,float(pol["main_round_trip_cost"]),"RANDOM",seed)
        m=sim["metrics"]
        random_rows.append({"seed":seed,"cagr":m["cagr"],"max_drawdown":m["max_drawdown"],
                            "sharpe":m["sharpe"],"avg_gross":m["avg_gross"],
                            "capital_efficiency":m["capital_efficiency"],
                            "trades":sim["diagnostics"]["executed_trades"],
                            "coverage":sim["diagnostics"]["coverage"]})
        if (seed+1)%10==0:print(f"[random] {seed+1}/{pol['random_same_day_tie_break_seeds']}")

    rdf=pd.DataFrame(random_rows)
    random_summary={k:pct_summary(rdf[k].dropna().tolist())
                    for k in ["cagr","max_drawdown","sharpe","avg_gross","capital_efficiency","trades","coverage"]}
    gate=standalone_gate(main,stress,random_summary,cfg)

    main["curve"].to_csv(outdir/"h120_equity_curve_20bps.csv",index=False)
    main["trades"].to_csv(outdir/"h120_trades_20bps.csv",index=False)
    stress["curve"].to_csv(outdir/"h120_equity_curve_50bps.csv",index=False)
    rdf.to_csv(outdir/"h120_random_tie_break_100.csv",index=False)

    result={
      "status":"PASS_FULL_MARKET_52WH_H120_STANDALONE_V0_1_RUN",
      "generated_at":datetime.now().isoformat(timespec="seconds"),
      "research_id":cfg["research_id"],
      "phase1":{
        "summary_sha256":sha256_file(cfg["source"]["phase1_summary"]),
        "verdict":phase["decision"]["verdict"],
        "h60_pass":phase["decision"]["h60"]["horizon_pass"],
        "h120_pass":phase["decision"]["h120"]["horizon_pass"],
        "candidate_events_sha256":sha256_file(cfg["source"]["candidate_events"]),
        "event_returns_sha256":sha256_file(cfg["source"]["event_returns"]),
        "complete_pos_h120_candidates":len(events),
      },
      "policy":pol,
      "price_lineage":{**lineage,"features_sha256":sha256_file(cfg["source"]["features"]),
                       "portfolio_price_panel":str(price_panel),"portfolio_price_panel_sha256":sha256_file(price_panel)},
      "full_market_h120":{
        "main_20bps":strip_sim(main),
        "stress_50bps":strip_sim(stress),
        "random_tie_break_20bps":{"seeds":len(rdf),**random_summary},
        "standalone_gate":gate,
      },
      "frozen_csi500_h120_reference":cfg["frozen_csi500_h120_reference"],
      "comparison_to_csi500":{
        "cagr_delta":main["metrics"]["cagr"]-float(cfg["frozen_csi500_h120_reference"]["cagr_20bps"]),
        "mdd_delta":main["metrics"]["max_drawdown"]-float(cfg["frozen_csi500_h120_reference"]["max_drawdown_20bps"]),
        "sharpe_delta":main["metrics"]["sharpe"]-float(cfg["frozen_csi500_h120_reference"]["sharpe_20bps"]),
        "random_p10_cagr_delta":random_summary["cagr"]["p10"]-float(cfg["frozen_csi500_h120_reference"]["random_cagr_p10_20bps"]),
        "random_median_cagr_delta":random_summary["cagr"]["median"]-float(cfg["frozen_csi500_h120_reference"]["random_cagr_median_20bps"]),
      },
      "next_if_pass":"Compare Full-Market 52WH H120 against frozen/comparable RET120 H120 as one trend/underreaction family; do not count both as independent Strategies.",
      "next_if_fail":"Retain CSI500 52WH evidence but reject Full-Market portfolio generalization; do not add filters or tune the 250-day lookback.",
      "safety":cfg["safety"],
      "artifacts":{
        "summary":str(outdir/"full_market_52wh_h120_standalone_v0_1_summary.json"),
        "curve20":str(outdir/"h120_equity_curve_20bps.csv"),
        "trades20":str(outdir/"h120_trades_20bps.csv"),
        "curve50":str(outdir/"h120_equity_curve_50bps.csv"),
        "random100":str(outdir/"h120_random_tie_break_100.csv"),
      }
    }
    sp=outdir/"full_market_52wh_h120_standalone_v0_1_summary.json"
    json_write(sp,result)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=safe))
    print("OUTPUT:",outdir)
    return 0

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("command",choices=["check","run"])
    ap.add_argument("--force",action="store_true")
    a=ap.parse_args();cfg=load_cfg()
    try:
        rc=check(cfg) if a.command=="check" else run(cfg,a.force)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_FULL_MARKET_52WH_H120_STANDALONE_V0_1",
                          "error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
