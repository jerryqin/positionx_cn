
from __future__ import annotations
import argparse, json, sys, hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from research import ret120_manual_fire_shadow as eng

CFG=ROOT/"config/execution/ret120_manual_fire_shadow_v0_1.json"

def C():
    return json.loads(CFG.read_text(encoding="utf-8"))

def now_cst():
    return datetime.now(timezone(timedelta(hours=8)))

def iso_now():
    return now_cst().isoformat(timespec="seconds")

def latest_funnel_run(c,signal_date):
    base=Path(c["paths"]["funnel_output_root"])/signal_date.replace("-","")
    if not base.exists():
        raise RuntimeError(f"funnel date directory missing: {base}")
    good=[]
    for s in base.glob("*/summary.json"):
        try:
            j=json.loads(s.read_text(encoding="utf-8"))
            if j.get("status")==c["input_policy"]["funnel_pass_status"] and j.get("signal_date")==signal_date:
                p=s.parent/"prepare_manual.csv"
                if p.exists():
                    good.append((s.parent.name,s,j,p))
        except Exception:
            pass
    if not good:
        raise RuntimeError(f"no successful canonical funnel run for {signal_date}")
    good.sort(key=lambda x:x[0])
    return good[-1][1],good[-1][2],good[-1][3]

def check(c):
    fr=Path(c["paths"]["funnel_output_root"])
    out={
      "status":"PASS_RET120_MANUAL_FIRE_SHADOW_V0_1_CHECK" if fr.exists() else "BLOCKED_RET120_MANUAL_FIRE_SHADOW_V0_1_CHECK",
      "funnel_output_root":str(fr),
      "funnel_output_root_exists":fr.exists(),
      "input_policy":c["input_policy"],
      "fire_gate":c["fire_gate"],
      "sizing":c["sizing"],
      "guardrails":c["guardrails"],
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if fr.exists() else 3

def make_template(c,signal_date,fire_date):
    sp,sj,pp=latest_funnel_run(c,signal_date)
    p=pd.read_csv(pp)
    if p.empty:
        raise RuntimeError("prepare_manual is empty")
    source_sha=sj["source_snapshot_sha256"]
    rows=[]
    for _,r in p.iterrows():
        rid=eng.recommendation_id(r,signal_date,fire_date,source_sha)
        rows.append({
          "recommendation_id":rid,
          "signal_date":signal_date,
          "fire_date":fire_date,
          "symbol":r["symbol"],
          "lane":r["lane"],
          "lane_rank":int(r["lane_rank"]),
          "open_valid_low":float(r["open_valid_low"]),
          "open_valid_high":float(r["open_valid_high"]),
          "stop_raw":float(r["stop_raw"]),
          "actual_open":"",
          "open_source":"",
          "observed_at":"",
        })
    outroot=Path(c["paths"]["output_root"])/signal_date.replace("-","")/"open_inputs"
    outroot.mkdir(parents=True,exist_ok=True)
    dest=outroot/f"open_input_{fire_date.replace('-','')}.csv"
    pd.DataFrame(rows).to_csv(dest,index=False,encoding="utf-8-sig")
    meta={
      "status":"OPEN_INPUT_TEMPLATE_CREATED",
      "signal_date":signal_date,
      "fire_date":fire_date,
      "prepare_manual_path":str(pp),
      "prepare_manual_sha256":eng.sha256_file(pp),
      "template":str(dest),
      "rows":len(rows),
      "instruction":"Fill actual_open and open_source for every row. Do not change recommendation_id or frozen zones."
    }
    print(json.dumps(meta,ensure_ascii=False,indent=2))
    return 0

def load_open_csv(path):
    x=pd.read_csv(path,dtype={"recommendation_id":str,"symbol":str})
    req={"recommendation_id","symbol","actual_open","open_source"}
    miss=req-set(x.columns)
    if miss:
        raise RuntimeError(f"open csv missing columns: {sorted(miss)}")
    x["actual_open"]=pd.to_numeric(x["actual_open"],errors="coerce")
    x["open_source"]=x["open_source"].fillna("").astype(str).str.strip()
    return x

def gate(c,signal_date,fire_date,nav,open_csv):
    sp,sj,pp=latest_funnel_run(c,signal_date)
    prep=pd.read_csv(pp)
    opens=load_open_csv(open_csv)
    source_sha=sj["source_snapshot_sha256"]
    expected={}
    for _,r in prep.iterrows():
        rid=eng.recommendation_id(r,signal_date,fire_date,source_sha)
        expected[rid]=r
    if opens["recommendation_id"].duplicated().any():
        raise RuntimeError("duplicate recommendation_id in open csv")
    unknown=set(opens["recommendation_id"])-set(expected)
    if unknown:
        raise RuntimeError(f"open csv has unknown recommendation ids: {sorted(unknown)[:3]}")
    om={str(r["recommendation_id"]):r for _,r in opens.iterrows()}
    records=[]
    for rid,r in expected.items():
        o=om.get(rid)
        actual_open=float(o["actual_open"]) if o is not None and pd.notna(o["actual_open"]) else float("nan")
        open_source=str(o["open_source"]) if o is not None else ""
        fire=eng.classify_open(actual_open,r["open_valid_low"],r["open_valid_high"],r["stop_raw"])
        sz=eng.recalc_structural_size(r,actual_open,nav,c["sizing"]) if fire=="FIRE_MANUAL" else eng.recalc_structural_size(r,float("nan"),nav,c["sizing"])
        rec={
          "recommendation_id":rid,
          "signal_date":signal_date,
          "fire_date":fire_date,
          "symbol":r["symbol"],
          "name":r.get("name",""),
          "lane":r["lane"],
          "lane_rank":int(r["lane_rank"]),
          "universe_tier":r["universe_tier"],
          "ret120":r["ret120"],
          "bucket":r["bucket"],
          "setup_state":r["setup_state"],
          "breakout_level_raw":r["breakout_level_raw"],
          "stop_raw":r["stop_raw"],
          "open_valid_low":r["open_valid_low"],
          "open_valid_high":r["open_valid_high"],
          "actual_open":actual_open,
          "open_source":open_source,
          "fire_state":fire,
          "shadow_action":"SHADOW_BUY_AT_OPEN" if fire=="FIRE_MANUAL" else "NO_SHADOW_TRADE",
          **sz,
          "t1_risk_model_status":c["sizing"]["t1_risk_model_status"],
          "manual_approval_required":True,
        }
        records.append(rec)
    out=pd.DataFrame(records)
    # Missing source labels are audit weakness but not a price-gate failure.
    rid=now_cst().strftime("%Y%m%dT%H%M%S+0800")
    dest=Path(c["paths"]["output_root"])/signal_date.replace("-","")/"fire_gate"/rid
    summary={
      "status":"PASS_RET120_MANUAL_FIRE_SHADOW_V0_1_GATE",
      "generated_at":iso_now(),
      "signal_date":signal_date,
      "fire_date":fire_date,
      "nav":float(nav),
      "source_funnel_summary":str(sp),
      "source_funnel_summary_sha256":eng.sha256_file(sp),
      "source_prepare_manual":str(pp),
      "source_prepare_manual_sha256":eng.sha256_file(pp),
      "source_open_csv":str(open_csv),
      "source_open_csv_sha256":eng.sha256_file(Path(open_csv)),
      "counts":out["fire_state"].value_counts(dropna=False).to_dict(),
      "shadow_trade_count":int(out["shadow_action"].eq("SHADOW_BUY_AT_OPEN").sum()),
      "shadow_notional_total":float(out.loc[out["shadow_action"].eq("SHADOW_BUY_AT_OPEN"),"suggested_notional"].sum()),
      "human_approval_required":True,
      "t1_risk_model_status":c["sizing"]["t1_risk_model_status"],
      "important":[
        "FIRE_MANUAL is a recommendation gate, not a broker order.",
        "Open is manual input in V0.1 because live-quote freshness is not yet qualified as execution truth.",
        "Sizing is structural-stop preview only; A-share T+1 unexitable-gap risk is not yet qualified.",
        "Suggested shares never exceed the prior PREPARE preview shares.",
        "This module does not modify or bypass the existing G4 execution chain."
      ],
      "guardrails":c["guardrails"],
    }
    summary_text=json.dumps(summary,ensure_ascii=False,indent=2,default=str)
    dest.mkdir(parents=True,exist_ok=False)
    out.to_csv(dest/"fire_gate.csv",index=False,encoding="utf-8-sig")
    shadow=out[out["shadow_action"].eq("SHADOW_BUY_AT_OPEN")].copy()
    shadow.to_csv(dest/"system_shadow_entries.csv",index=False,encoding="utf-8-sig")
    decisions=out[out["fire_state"].eq("FIRE_MANUAL")][[
        "recommendation_id","signal_date","fire_date","symbol","lane","lane_rank","actual_open","suggested_shares","suggested_notional"
    ]].copy()
    decisions["human_decision"]=""
    decisions["human_note"]=""
    decisions.to_csv(dest/"human_decision_template.csv",index=False,encoding="utf-8-sig")
    (dest/"summary.json").write_text(summary_text,encoding="utf-8")
    print(summary_text)
    print("OUTPUT:",dest)
    return 0

def find_gate_run(c,gate_run):
    p=Path(gate_run)
    if p.is_file():
        p=p.parent
    if not (p/"summary.json").exists() or not (p/"fire_gate.csv").exists():
        raise RuntimeError("gate run must contain summary.json and fire_gate.csv")
    sj=json.loads((p/"summary.json").read_text(encoding="utf-8"))
    if sj.get("status")!="PASS_RET120_MANUAL_FIRE_SHADOW_V0_1_GATE":
        raise RuntimeError("gate run is not PASS")
    return p,sj,pd.read_csv(p/"fire_gate.csv")

def record_human(c,gate_run,recommendation_id,decision,note,fill_price,fill_shares,fees):
    decision=decision.upper()
    if decision not in c["human_decisions"]["allowed"]:
        raise RuntimeError(f"decision must be one of {c['human_decisions']['allowed']}")
    p,sj,g=find_gate_run(c,gate_run)
    z=g[g["recommendation_id"].eq(recommendation_id)]
    if len(z)!=1:
        raise RuntimeError("recommendation_id not unique/found in gate run")
    r=z.iloc[0]
    if r["fire_state"]!="FIRE_MANUAL" and decision=="APPROVE":
        raise RuntimeError(f"cannot APPROVE non-FIRE recommendation: {r['fire_state']}")
    fp,fs,ff=eng.normalize_human_fill(decision,fill_price,fill_shares,fees)
    payload={
      "event_type":"HUMAN_DECISION",
      "recorded_at":iso_now(),
      "recommendation_id":recommendation_id,
      "signal_date":str(r["signal_date"]),
      "fire_date":str(r["fire_date"]),
      "symbol":str(r["symbol"]),
      "lane":str(r["lane"]),
      "system_fire_state":str(r["fire_state"]),
      "system_shadow_action":str(r["shadow_action"]),
      "system_suggested_shares":int(r["suggested_shares"]),
      "system_actual_open":float(r["actual_open"]) if pd.notna(r["actual_open"]) else None,
      "human_decision":decision,
      "human_note":note or "",
      "human_fill_price":fp,
      "human_fill_shares":fs,
      "human_fill_fees":ff,
      "gate_run":str(p),
      "gate_summary_sha256":eng.sha256_file(p/"summary.json"),
    }
    event=eng.append_hash_chain_jsonl(c["paths"]["ledger_path"],payload)
    print(json.dumps({"status":"HUMAN_DECISION_RECORDED","event":event},ensure_ascii=False,indent=2,default=str))
    return 0

def ledger_status(c):
    p=Path(c["paths"]["ledger_path"])
    rows=[]
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    df=pd.DataFrame(rows)
    if df.empty:
        out={"status":"RET120_HUMAN_LEDGER_STATUS","events":0,"latest_decisions":[]}
    else:
        df=df.sort_values("recorded_at")
        latest=df.groupby("recommendation_id",as_index=False).tail(1)
        out={
          "status":"RET120_HUMAN_LEDGER_STATUS",
          "events":len(df),
          "unique_recommendations":int(df["recommendation_id"].nunique()),
          "latest_decision_counts":latest["human_decision"].value_counts().to_dict(),
          "latest_decisions":latest[["recommendation_id","symbol","lane","system_fire_state","human_decision","human_fill_price","human_fill_shares","human_fill_fees","recorded_at"]].to_dict(orient="records")
        }
    print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
    return 0

def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest="command",required=True)
    sub.add_parser("check")
    p=sub.add_parser("make-open-template")
    p.add_argument("--signal-date",required=True);p.add_argument("--fire-date",required=True)
    p=sub.add_parser("gate")
    p.add_argument("--signal-date",required=True);p.add_argument("--fire-date",required=True)
    p.add_argument("--nav",type=float,required=True);p.add_argument("--open-csv",required=True)
    p=sub.add_parser("record-human")
    p.add_argument("--gate-run",required=True);p.add_argument("--recommendation-id",required=True)
    p.add_argument("--decision",required=True,choices=["APPROVE","REJECT","SKIP"])
    p.add_argument("--note",default="")
    p.add_argument("--fill-price",type=float);p.add_argument("--fill-shares",type=int);p.add_argument("--fees",type=float,default=0.0)
    sub.add_parser("ledger-status")
    a=ap.parse_args();c=C()
    try:
        if a.command=="check": rc=check(c)
        elif a.command=="make-open-template": rc=make_template(c,a.signal_date,a.fire_date)
        elif a.command=="gate": rc=gate(c,a.signal_date,a.fire_date,a.nav,a.open_csv)
        elif a.command=="record-human": rc=record_human(c,a.gate_run,a.recommendation_id,a.decision,a.note,a.fill_price,a.fill_shares,a.fees)
        else: rc=ledger_status(c)
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_RET120_MANUAL_FIRE_SHADOW_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        rc=3
    raise SystemExit(rc)

if __name__=="__main__":
    main()
