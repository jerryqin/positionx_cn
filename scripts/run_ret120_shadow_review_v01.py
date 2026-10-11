
from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pandas as pd
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from research import ret120_shadow_review as eng
CFG=ROOT/"config/review/ret120_shadow_review_v0_1.json"
def C():return json.loads(CFG.read_text(encoding="utf-8"))
def now_cst():return datetime.now(timezone(timedelta(hours=8)))
def iso_now():return now_cst().isoformat(timespec="seconds")
def load_fire_rows(c):
    root=Path(c["paths"]["fire_root"]);rows=[]
    for s in root.glob("*/fire_gate/*/summary.json"):
        try:
            j=json.loads(s.read_text(encoding="utf-8"))
            if j.get("status")!="PASS_RET120_MANUAL_FIRE_SHADOW_V0_1_GATE":continue
            f=s.parent/"fire_gate.csv"
            if not f.exists():continue
            x=pd.read_csv(f);x["_gate_run"]=s.parent.name;x["_gate_summary"]=str(s);x["_gate_summary_sha256"]=eng.sha256_file(s);rows.append(x)
        except Exception:continue
    if not rows:return pd.DataFrame()
    return eng.canonicalize_gate_rows(pd.concat(rows,ignore_index=True))
def load_decisions(c):return eng.read_jsonl(c["paths"]["human_decision_ledger"])
def load_trades(c):return eng.read_jsonl(c["paths"]["human_trade_ledger"])
def check(c):
    out={"status":"PASS_RET120_SHADOW_REVIEW_V0_1_CHECK","market_db_exists":Path(c["paths"]["market_db"]).exists(),"fire_root_exists":Path(c["paths"]["fire_root"]).exists(),"human_decision_ledger_exists":Path(c["paths"]["human_decision_ledger"]).exists(),"human_trade_ledger_exists":Path(c["paths"]["human_trade_ledger"]).exists(),"accounts":c["accounts"],"markouts":c["markouts"],"guardrails":c["guardrails"]}
    if not out["market_db_exists"] or not out["fire_root_exists"]:out["status"]="BLOCKED_RET120_SHADOW_REVIEW_V0_1_CHECK"
    print(json.dumps(out,ensure_ascii=False,indent=2));return 0 if out["status"].startswith("PASS") else 3
def record_trade(c,recommendation_id,action,trade_date,fill_price,fill_shares,fees,note):
    action=action.upper()
    if action not in c["trade_ledger"]["actions"]:raise RuntimeError(f"action must be one of {c['trade_ledger']['actions']}")
    gates=load_fire_rows(c)
    if gates.empty or recommendation_id not in set(gates["recommendation_id"].astype(str)):raise RuntimeError("unknown recommendation_id")
    d=eng.first_terminal_decisions(load_decisions(c));z=d[d["recommendation_id"].astype(str).eq(recommendation_id)]
    if z.empty or z.iloc[0]["canonical_human_decision"]!="APPROVE":raise RuntimeError("trade events require a canonical APPROVE")
    if not np.isfinite(float(fill_price)) or float(fill_price)<=0 or int(fill_shares)<=0:raise RuntimeError("invalid fill")
    if not np.isfinite(float(fees or 0.0)) or float(fees or 0.0)<0:raise RuntimeError("fees must be finite and >= 0")
    payload={"event_type":"HUMAN_TRADE","recorded_at":iso_now(),"recommendation_id":recommendation_id,"action":action,"trade_date":trade_date,"fill_price":float(fill_price),"fill_shares":int(fill_shares),"fees":float(fees or 0.0),"note":note or ""}
    existing=load_trades(c)
    candidate=pd.concat([existing,pd.DataFrame([payload])],ignore_index=True) if not existing.empty else pd.DataFrame([payload])
    eng.human_live_from_ledgers(gates,load_decisions(c),candidate,{})
    event=eng.append_hash_chain_jsonl(c["paths"]["human_trade_ledger"],payload)
    print(json.dumps({"status":"HUMAN_TRADE_RECORDED","event":event},ensure_ascii=False,indent=2));return 0
def _duck(c):
    import duckdb
    return duckdb.connect(c["paths"]["market_db"],read_only=True)
def market_sessions_and_prices(c,gates,as_of):
    if gates.empty:return [],{},{}
    con=_duck(c)
    try:
        sessions=[str(x[0])[:10] for x in con.execute(f"SELECT DISTINCT trade_date FROM {c['paths']['market_table']} WHERE trade_date<=? ORDER BY trade_date",[as_of]).fetchall()]
        symbols=sorted(set(gates["symbol"].astype(str)))
        if symbols:
            con.register("px_symbols",pd.DataFrame({"symbol":symbols}));bars=con.execute(f"SELECT b.symbol,b.trade_date,b.close FROM {c['paths']['market_table']} b JOIN px_symbols s USING(symbol) WHERE b.trade_date<=? ORDER BY b.symbol,b.trade_date",[as_of]).df()
        else:bars=pd.DataFrame(columns=["symbol","trade_date","close"])
    finally:con.close()
    if not bars.empty:bars["trade_date"]=bars["trade_date"].astype(str).str[:10]
    px={(str(r.symbol),str(r.trade_date)):float(r.close) for r in bars.itertuples(index=False) if pd.notna(r.close) and float(r.close)>0};last={}
    if not bars.empty:
        for sym,g in bars.groupby("symbol"):
            g=g[pd.to_numeric(g["close"],errors="coerce")>0]
            if not g.empty:last[str(sym)]=float(g.iloc[-1]["close"])
    return sessions,px,last
def build_markouts(c,accounts,as_of,sessions,px):
    rows=[]
    for _,r in accounts.iterrows():
        fire_date=str(r["fire_date"])[:10];future=[d for d in sessions if d>fire_date]
        for h in [int(x) for x in c["markouts"]["horizons_sessions"]]:
            target=future[h-1] if len(future)>=h else None;close=px.get((str(r["symbol"]),target),np.nan) if target else np.nan;entry=float(r["actual_open"]) if pd.notna(r["actual_open"]) else np.nan;ret=close/entry-1.0 if np.isfinite(entry) and entry>0 and np.isfinite(close) and close>0 else np.nan
            rows.append({"account":r["account"],"recommendation_id":r["recommendation_id"],"symbol":r["symbol"],"lane":r.get("lane",""),"universe_tier":r.get("universe_tier",""),"canonical_human_decision":r.get("canonical_human_decision","NO_DECISION"),"fire_date":fire_date,"entry_open":entry,"horizon_sessions":h,"target_date":target,"target_close":close,"gross_return":ret,"matured":bool(target is not None and np.isfinite(close))})
    return pd.DataFrame(rows)
def aggregate_markouts(markouts):
    rows=[]
    if markouts.empty:return pd.DataFrame()
    dims=[("ACCOUNT",["account"]),("ACCOUNT_LANE",["account","lane"]),("DECISION",["canonical_human_decision"]),("DECISION_LANE",["canonical_human_decision","lane"]),("UNIVERSE",["universe_tier"])]
    for label,cols in dims:
        for keys,g in markouts.groupby(cols+["horizon_sessions"],dropna=False):
            if not isinstance(keys,tuple):keys=(keys,)
            rec={"group_type":label}
            for cc,v in zip(cols+["horizon_sessions"],keys):rec[cc]=v
            rec.update(eng.cohort_metrics(g));rows.append(rec)
    return pd.DataFrame(rows)
def report(c,as_of):
    gates=load_fire_rows(c)
    if gates.empty:raise RuntimeError("no canonical fire gate records yet")
    decisions=load_decisions(c);trades=load_trades(c);accounts,gate_with_decision=eng.build_account_entries(gates,decisions);sessions,px,last=market_sessions_and_prices(c,gates,as_of);markouts=build_markouts(c,accounts,as_of,sessions,px);agg=aggregate_markouts(markouts);live_events,live_positions,live_realized=eng.human_live_from_ledgers(gates,decisions,trades,last)
    system=accounts[accounts["account"].eq("SYSTEM_ALL_FIRE")].copy() if not accounts.empty else pd.DataFrame();selection=accounts[accounts["account"].eq("HUMAN_SELECTION_SHADOW")].copy() if not accounts.empty else pd.DataFrame()
    def mtm_shadow(df):
        if df.empty:return {"entries":0,"entry_notional":0.0,"current_value":0.0,"mtm_pnl":0.0,"mtm_return_on_entry_notional":None}
        entry=(pd.to_numeric(df["actual_open"],errors="coerce")*pd.to_numeric(df["suggested_shares"],errors="coerce")).sum();cur=0.0
        for _,r in df.iterrows():
            p=last.get(str(r["symbol"]),np.nan)
            if np.isfinite(p):cur+=p*float(r["suggested_shares"])
        pnl=cur-entry;return {"entries":int(len(df)),"entry_notional":float(entry),"current_value":float(cur),"mtm_pnl":float(pnl),"mtm_return_on_entry_notional":float(pnl/entry) if entry>0 else None}
    sys_mtm=mtm_shadow(system);sel_mtm=mtm_shadow(selection);live_summary={"positions":int((live_positions["shares"]>0).sum()) if not live_positions.empty else 0,"realized_pnl":float(live_positions["realized_pnl"].sum()) if not live_positions.empty else 0.0,"unrealized_pnl":float(pd.to_numeric(live_positions["unrealized_pnl"],errors="coerce").fillna(0).sum()) if not live_positions.empty else 0.0,"total_pnl":float(live_positions["total_pnl"].sum()) if not live_positions.empty else 0.0,"fees":float(live_positions["fees"].sum()) if not live_positions.empty else 0.0}
    sel_alpha=[]
    if not markouts.empty:
        sm=markouts[markouts["account"].eq("SYSTEM_ALL_FIRE")].groupby("horizon_sessions")["gross_return"].mean();hm=markouts[markouts["account"].eq("HUMAN_SELECTION_SHADOW")].groupby("horizon_sessions")["gross_return"].mean()
        for h in sorted(set(markouts["horizon_sessions"])):
            a=sm.get(h,np.nan);b=hm.get(h,np.nan);sel_alpha.append({"horizon_sessions":int(h),"system_avg_return":None if pd.isna(a) else float(a),"human_selection_avg_return":None if pd.isna(b) else float(b),"selection_alpha":None if pd.isna(a) or pd.isna(b) else float(b-a)})
    sel_alpha_df=pd.DataFrame(sel_alpha)
    summary={"status":"PASS_RET120_SHADOW_REVIEW_V0_1_REPORT","generated_at":iso_now(),"as_of":as_of,"canonical_fire_recommendations":int(len(gates)),"system_fire_entries":int(len(system)),"human_selection_entries":int(len(selection)),"human_decision_counts":gate_with_decision["canonical_human_decision"].value_counts().to_dict() if not gate_with_decision.empty else {},"decision_conflicts":int(gate_with_decision["decision_conflict"].fillna(False).sum()) if "decision_conflict" in gate_with_decision.columns else 0,"system_all_fire_mtm_hold_only":sys_mtm,"human_selection_shadow_mtm_hold_only":sel_mtm,"human_live":live_summary,"full_portfolio_metrics_status":"BLOCKED_UNTIL_SYSTEM_EXIT_POLICY_QUALIFIED","markout_semantics":c["markouts"],"selection_alpha_status":"AVAILABLE_ONLY_WHEN_BOTH_SYSTEM_AND_SELECTION_HAVE_MATURED_MARKOUTS","guardrails":c["guardrails"]}
    outroot=Path(c["paths"]["output_root"])/"reports"/as_of.replace("-","");rid=now_cst().strftime("%Y%m%dT%H%M%S+0800");dest=outroot/rid;summary_text=json.dumps(summary,ensure_ascii=False,indent=2,default=str);dest.mkdir(parents=True,exist_ok=False)
    gates.to_csv(dest/"canonical_fire_recommendations.csv",index=False,encoding="utf-8-sig");accounts.to_csv(dest/"account_entries.csv",index=False,encoding="utf-8-sig");gate_with_decision.to_csv(dest/"fire_with_human_decision.csv",index=False,encoding="utf-8-sig");markouts.to_csv(dest/"markouts_h20_h60_h120.csv",index=False,encoding="utf-8-sig");agg.to_csv(dest/"markout_comparison.csv",index=False,encoding="utf-8-sig");sel_alpha_df.to_csv(dest/"selection_alpha.csv",index=False,encoding="utf-8-sig");live_events.to_csv(dest/"human_live_events.csv",index=False,encoding="utf-8-sig");live_positions.to_csv(dest/"human_live_positions.csv",index=False,encoding="utf-8-sig");live_realized.to_csv(dest/"human_live_realized.csv",index=False,encoding="utf-8-sig");(dest/"summary.json").write_text(summary_text,encoding="utf-8")
    lines=["# RET120 Shadow Review V0.1","",f"- As of: **{as_of}**",f"- Canonical FIRE recommendations: **{len(gates)}**",f"- System All-Fire entries: **{len(system)}**",f"- Human-Selection Shadow entries: **{len(selection)}**",f"- Human Live open positions: **{live_summary['positions']}**","","## Important status","","**System exit policy is not qualified. Therefore CAGR / MDD / Sharpe for System Shadow are intentionally not reported.**","System and Human-Selection MTM are hold-only diagnostics, not qualified portfolio performance.","","## Human decisions","",json.dumps(summary["human_decision_counts"],ensure_ascii=False),"","## Human Live P&L","",f"- Realized P&L: {live_summary['realized_pnl']:.2f}",f"- Unrealized P&L: {live_summary['unrealized_pnl']:.2f}",f"- Total P&L: {live_summary['total_pnl']:.2f}",f"- Recorded fees: {live_summary['fees']:.2f}","","## Fixed-horizon evidence","","Use `markout_comparison.csv` and `selection_alpha.csv` for H20/H60/H120.","These compare selection quality without inventing a System exit rule.","","## Three-account interpretation","","- SYSTEM_ALL_FIRE: every FIRE_MANUAL.","- HUMAN_SELECTION_SHADOW: only canonical APPROVE names, but system entry price/shares.","- HUMAN_LIVE: actual approved fills plus recorded ADD/REDUCE/EXIT events."]
    (dest/"report.md").write_text("\n".join(lines),encoding="utf-8");print(summary_text);print("OUTPUT:",dest);return 0
def status(c):
    gates=load_fire_rows(c);decisions=load_decisions(c);trades=load_trades(c);d=eng.first_terminal_decisions(decisions);out={"status":"RET120_SHADOW_REVIEW_V0_1_STATUS","canonical_gate_records":int(len(gates)),"human_decision_events":int(len(decisions)),"canonical_decisions":int(len(d)),"canonical_decision_counts":d["canonical_human_decision"].value_counts().to_dict() if not d.empty else {},"human_trade_events":int(len(trades)),"human_trade_ledger":c["paths"]["human_trade_ledger"]};print(json.dumps(out,ensure_ascii=False,indent=2,default=str));return 0
def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest="command",required=True);sub.add_parser("check");sub.add_parser("status");p=sub.add_parser("record-trade");p.add_argument("--recommendation-id",required=True);p.add_argument("--action",required=True,choices=["ADD","REDUCE","EXIT"]);p.add_argument("--trade-date",required=True);p.add_argument("--fill-price",required=True,type=float);p.add_argument("--fill-shares",required=True,type=int);p.add_argument("--fees",type=float,default=0.0);p.add_argument("--note",default="");p=sub.add_parser("report");p.add_argument("--as-of",required=True);a=ap.parse_args();c=C()
    try:
        if a.command=="check":rc=check(c)
        elif a.command=="status":rc=status(c)
        elif a.command=="record-trade":rc=record_trade(c,a.recommendation_id,a.action,a.trade_date,a.fill_price,a.fill_shares,a.fees,a.note)
        else:rc=report(c,a.as_of)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_SHADOW_REVIEW_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2));rc=3
    raise SystemExit(rc)
if __name__=="__main__":main()
