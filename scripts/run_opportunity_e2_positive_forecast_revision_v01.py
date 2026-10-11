
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from opportunity.e2_positive_forecast_revision_v01 import (
    prepare_events,prepare_market,synthetic_adjust_ohlc,attach_entry,add_labels,evaluate
)

CFG=ROOT/"config/opportunity/e2_positive_forecast_revision_v0_1.yaml"

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding="utf-8"))
def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""): h.update(c)
    return h.hexdigest()
def qt(x): return ".".join('"' + p.replace('"','""') + '"' for p in x.split("."))

def verify(cfg):
    sp=Path(cfg["shadow_union"]["path"])
    qp=Path(cfg["qmt_verification"]["path"])
    cat=Path(cfg["catalog"])
    if not sp.exists() or not qp.exists() or not cat.exists():
        raise FileNotFoundError("required frozen input missing")
    if sha256_file(sp)!=cfg["shadow_union"]["sha256"]:
        raise RuntimeError("shadow union SHA mismatch")
    q=json.loads(qp.read_text(encoding="utf-8"))
    if q.get("summary",{}).get("accepted") is not True:
        raise RuntimeError("QMT independent verification not accepted")
    if q.get("summary",{}).get("verdict")!=cfg["qmt_verification"]["required_verdict"]:
        raise RuntimeError("unexpected QMT verification verdict")
    if q.get("safety",{}).get("canonical_promotion") is not False:
        raise RuntimeError("canonical promotion state changed")
    return sp,qp,cat,q

def asset_registry(con,cfg):
    r=con.execute("""select asset_version,source_sha256,table_name,row_count
                     from research.asset_registry_v1 where asset_id=?""",
                  [cfg["event_asset"]["asset_id"]]).fetchone()
    if r is None: raise RuntimeError("event asset registry missing")
    if r[0]!=cfg["event_asset"]["asset_version"] or r[1]!=cfg["event_asset"]["source_sha256"] or r[2]!=cfg["event_table"]:
        raise RuntimeError(f"event asset mismatch {r}")
    return {"asset_version":r[0],"source_sha256":r[1],"table_name":r[2],"row_count":int(r[3])}

def check(cfg):
    try:
        sp,qp,cat,q=verify(cfg)
        import duckdb
        con=duckdb.connect(str(cat),read_only=True)
        try: ar=asset_registry(con,cfg)
        finally: con.close()
        out={"status":"PASS_E2_POSITIVE_FORECAST_REVISION_V0_1_CHECK",
             "event_asset":ar,"shadow_union":str(sp),"shadow_sha256":sha256_file(sp),
             "qmt_verdict":q["summary"]["verdict"],"mechanism":cfg["mechanism"],
             "baseline":cfg["baseline"],"labels":cfg["labels"],"fast_reject":cfg["fast_reject"]}
        print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
    except Exception as e:
        print(json.dumps({"status":"BLOCKED_E2_POSITIVE_FORECAST_REVISION_V0_1_CHECK","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2)); return 3

def run(cfg):
    try:
        sp,qp,cat,q=verify(cfg)
        import duckdb
        con=duckdb.connect(str(cat),read_only=True)
        try:
            ar=asset_registry(con,cfg)
            events=con.execute(f"select * from {qt(cfg['event_table'])}").fetchdf()
        finally: con.close()

        shadow=pd.read_parquet(sp)
        ev=prepare_events(events,cfg)
        allowed=set(ev["canonical_symbol"].dropna().astype(str))
        market,conflicts=prepare_market(shadow,allowed)

        bars=synthetic_adjust_ohlc(market)
        factor_rate=float(bars["_factor_valid"].mean()) if len(bars) else 0.0
        ev=attach_entry(ev,bars)
        mapping=float(ev["entry_date"].notna().mean()) if len(ev) else 0.0

        dq=cfg["data_quality_gate"]
        dq_checks={
            "entry_mapping_coverage_min":mapping>=float(dq["entry_mapping_coverage_min"]),
            "market_factor_valid_rate_min":factor_rate>=float(dq["market_factor_valid_rate_min"]),
            "canonicalization_conflict_rows_max":conflicts<=int(dq["canonicalization_conflict_rows_max"]),
        }
        if not all(dq_checks.values()):
            raise RuntimeError(f"data quality gate failed {dq_checks}")

        panel=add_labels(ev,bars,cfg["labels"]["primary"],cfg["labels"]["secondary"])
        ms=int(cfg["baseline"]["min_setup_per_stratum"])
        primary=evaluate(panel,"E2_TREND25_60_label",cfg["fast_reject"],ms)
        secondary=evaluate(panel,"E2_TREND40_120_label",cfg["fast_reject"],ms)

        out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)
        pp=out/"e2_positive_forecast_revision_v0_1_panel.parquet"
        panel.to_parquet(pp,index=False)

        result={
            "status":"PASS_E2_POSITIVE_FORECAST_REVISION_V0_1_RUN",
            "generated_at":datetime.now().isoformat(timespec="seconds"),
            "research_id":cfg["research_id"],
            "event_asset":ar,
            "mechanism":cfg["mechanism"],
            "baseline":cfg["baseline"],
            "labels":cfg["labels"],
            "coverage":{
                "candidate_event_rows":int(len(ev)),
                "baseline_original_rows":int(ev["e2_baseline"].sum()),
                "first_revision_signal_rows":int(ev["e2_signal"].sum()),
                "entry_mapping_coverage":mapping,
                "market_factor_valid_rate":factor_rate,
                "canonicalization_conflict_rows":conflicts,
            },
            "data_quality_checks":dq_checks,
            "primary":primary,
            "secondary":secondary,
            "overall_verdict":primary["verdict"],
            "artifacts":{"panel":str(pp),"panel_sha256":sha256_file(pp)},
            "safety":cfg["safety"],
        }
        spath=out/"e2_positive_forecast_revision_v0_1_summary.json"
        spath.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2)); print(f"OUTPUT: {out}"); return 0
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_E2_POSITIVE_FORECAST_REVISION_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2)); return 3

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    raise SystemExit(check(cfg) if a.command=="check" else run(cfg))

if __name__=="__main__": main()
