
from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/e1_identity_qmt_verification_v0_2.yaml"
KNOWN={"IMPROVING","DETERIORATING","TURN_TO_LOSS","TURNAROUND","LOSS_NARROWING","LOSS_WIDENING","FLAT"}

def duckdb_mod():
    import duckdb
    return duckdb

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""): h.update(c)
    return h.hexdigest()

def qt(x):
    return ".".join('"' + p.replace('"','""') + '"' for p in x.split("."))

def nb(s):
    return s.astype("string").str.strip().str.lower().isin(["true","1","yes","y","eligible"])

def code6(symbol):
    s=str(symbol or "").upper().strip()
    m=re.search(r"(\d{6})",s)
    return m.group(1) if m else None

def suffix(symbol):
    s=str(symbol or "").upper().strip()
    m=re.search(r"\.(SH|SZ|BJ)$",s)
    return m.group(1) if m else None

def classify_equity(symbol, non_equity_symbols, policy):
    code=code6(symbol)
    if not code:
        return None,"EXCLUDE_INVALID_SYMBOL"
    if str(symbol).upper().strip() in non_equity_symbols:
        return None,"EXCLUDE_NON_EQUITY_REQUEST"
    p2=code[:2]
    if p2 in set(policy["exclude_b_share_prefixes"]):
        return None,"EXCLUDE_B_SHARE"
    if p2 in set(policy["include_bse_prefixes"]):
        return f"{code}.{policy['bse_suffix']}","BSE_CANONICALIZED"
    if p2 in {"00","30"}:
        return f"{code}.SZ","A_SHARE_CANONICAL"
    if p2 in {"60","68"}:
        return f"{code}.SH","A_SHARE_CANONICAL"
    return None,"EXCLUDE_OUTSIDE_E1_EQUITY_DOMAIN"

def setup_events(df):
    x=df.copy()
    x["event_time"]=pd.to_datetime(x["event_time"],errors="coerce")
    kind=x["event_kind"].astype("string").str.upper().str.strip()
    change=x["change_type"].astype("string").str.upper().str.strip()
    m=nb(x["strategy_eligible_semantics"]) & kind.eq("ORIGINAL_FORECAST") & change.isin(KNOWN) & x["event_time"].notna()
    return x[m].copy()

def mapping_coverage(panel, shadow):
    b=shadow.copy()
    b["symbol"]=b["symbol"].astype("string").str.upper().str.strip()
    b["trade_date"]=pd.to_datetime(b["trade_date"],errors="coerce").dt.normalize()
    mx=b.groupby("symbol")["trade_date"].max()
    e=panel[panel["identity_eligible"]].copy()
    e["event_date"]=pd.to_datetime(e["event_time"],errors="coerce").dt.normalize()
    maxd=e["canonical_symbol"].map(mx)
    possible=maxd.notna() & (e["event_date"] < maxd)
    return {
        "identity_eligible_setup_rows":int(len(e)),
        "identity_eligible_distinct_symbols":int(e["canonical_symbol"].nunique()),
        "next_bar_possible_rows":int(possible.sum()),
        "next_bar_possible_rate":float(possible.mean()) if len(e) else 0.0,
        "unmapped_rows":int((~possible).sum()),
    }

def run(cfg):
    req=pd.read_csv(cfg["request_csv"])
    req["symbol"]=req["symbol"].astype("string").str.upper().str.strip()
    non_eq=set(req.loc[req["board_bucket"].astype("string").eq(cfg["identity_policy"]["exclude_request_board_bucket"]),"symbol"])

    duckdb=duckdb_mod()
    con=duckdb.connect(cfg["catalog"],read_only=True)
    try:
        events=con.execute(f"select * from {qt(cfg['event_table'])}").fetchdf()
    finally:
        con.close()

    x=setup_events(events)
    x["original_symbol"]=x["symbol"].astype("string").str.upper().str.strip()
    mapped=x["original_symbol"].map(lambda s: classify_equity(s,non_eq,cfg["identity_policy"]))
    x["canonical_symbol"]=[v[0] for v in mapped]
    x["identity_status"]=[v[1] for v in mapped]
    x["identity_eligible"]=x["canonical_symbol"].notna()

    shadow=pd.read_parquet(cfg["shadow_union"])
    coverage=mapping_coverage(x,shadow)

    counts={str(k):int(v) for k,v in x["identity_status"].value_counts(dropna=False).items()}
    bse=x[x["identity_status"].eq("BSE_CANONICALIZED")].copy()

    out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)
    panel_path=out/"e1_equity_identity_panel_v0_2.parquet"
    keep=[c for c in [
        "announcementId","secCode","secName","event_time","event_precision","report_period",
        "change_type","original_symbol","canonical_symbol","identity_status","identity_eligible"
    ] if c in x.columns]
    x[keep].to_parquet(panel_path,index=False)

    excluded_path=out/"e1_identity_excluded_events_v0_2.csv"
    x.loc[~x["identity_eligible"],keep].to_csv(excluded_path,index=False,encoding="utf-8-sig")

    bse_path=out/"e1_bse_canonicalization_v0_2.csv"
    bse[keep].to_csv(bse_path,index=False,encoding="utf-8-sig")

    result={
        "status":"PASS_E1_IDENTITY_FREEZE_V0_2_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "original_setup_rows":int(len(x)),
        "identity_eligible_setup_rows":int(x["identity_eligible"].sum()),
        "identity_excluded_setup_rows":int((~x["identity_eligible"]).sum()),
        "identity_status_counts":counts,
        "bse_canonicalized_rows":int(len(bse)),
        "coverage_on_frozen_shadow_union":coverage,
        "policy":cfg["identity_policy"],
        "identity_frozen":True,
        "e1_outcomes_read":False,
        "artifacts":{
            "identity_panel":str(panel_path),
            "identity_panel_sha256":sha256_file(panel_path),
            "excluded_events_csv":str(excluded_path),
            "bse_canonicalization_csv":str(bse_path)
        },
        "next_action":"Run independent QMT price verification before any E1 outcome test."
    }
    sp=out/"e1_identity_freeze_v0_2_summary.json"
    sp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2)); print(f"OUTPUT: {out}")
    return 0

def check(cfg):
    ps=[cfg["catalog"],cfg["request_csv"],cfg["shadow_union"]]
    out={"status":"PASS_E1_IDENTITY_FREEZE_V0_2_CHECK","paths":{}}
    for p in ps:
        ex=Path(p).exists()
        out["paths"][p]=ex
        if not ex: out["status"]="BLOCKED_E1_IDENTITY_FREEZE_V0_2_CHECK"
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out["status"].startswith("PASS") else 3

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_E1_IDENTITY_FREEZE_V0_2","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__": main()
