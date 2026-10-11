
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from opportunity.e1_forecast_turnaround_v02 import (
    prepare_identity_events, prepare_shadow_market, synthetic_adjust_ohlc,
    attach_entry, add_matured_labels, evaluate
)

CFG=ROOT/"config/opportunity/e1_forecast_turnaround_v0_2.yaml"

def load_cfg(): return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""): h.update(c)
    return h.hexdigest()

def verify_inputs(cfg):
    ip=Path(cfg["identity_panel"]["path"])
    sp=Path(cfg["shadow_union"]["path"])
    tp=Path(cfg["trust_finalize"]["path"])
    for p in [ip,sp,tp]:
        if not p.exists(): raise FileNotFoundError(p)
    if sha256_file(ip)!=cfg["identity_panel"]["sha256"]:
        raise RuntimeError("identity panel SHA mismatch")
    if sha256_file(sp)!=cfg["shadow_union"]["sha256"]:
        raise RuntimeError("shadow union SHA mismatch")
    trust=json.loads(tp.read_text(encoding="utf-8"))
    if trust.get("accepted_for_e1_research") is not True:
        raise RuntimeError("trust finalize is not accepted for E1 research")
    if trust.get("verdict")!=cfg["trust_finalize"]["required_verdict"]:
        raise RuntimeError(f"unexpected trust verdict {trust.get('verdict')}")
    if trust.get("canonical_promotion_allowed") is not False:
        raise RuntimeError("canonical promotion state unexpectedly changed")
    return ip,sp,tp,trust

def check(cfg):
    try:
        ip,sp,tp,trust=verify_inputs(cfg)
        out={
            "status":"PASS_E1_FORECAST_TURNAROUND_V0_2_CHECK",
            "identity_panel":str(ip),"identity_sha256":sha256_file(ip),
            "shadow_union":str(sp),"shadow_sha256":sha256_file(sp),
            "trust_finalize":str(tp),"trust_verdict":trust.get("verdict"),
            "evaluation_population":cfg["labels"]["evaluation_population"],
            "mechanism":cfg["mechanism"],
            "baseline":cfg["baseline"],
            "fast_reject":cfg["fast_reject"],
        }
        print(json.dumps(out,ensure_ascii=False,indent=2)); return 0
    except Exception as e:
        print(json.dumps({"status":"BLOCKED_E1_FORECAST_TURNAROUND_V0_2_CHECK","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2)); return 3

def run(cfg):
    try:
        ip,sp,tp,trust=verify_inputs(cfg)
        identity=pd.read_parquet(ip)
        shadow=pd.read_parquet(sp)

        ev=prepare_identity_events(identity)
        allowed=set(ev["symbol"].dropna().astype(str))
        market,conflicts=prepare_shadow_market(shadow,allowed)

        dq=cfg["data_quality_gate"]
        if conflicts>int(dq["canonicalization_conflict_rows_max"]):
            raise RuntimeError(f"canonicalization conflicts={conflicts}")

        bars=synthetic_adjust_ohlc(market)
        factor_rate=float(bars["_factor_valid"].mean()) if len(bars) else 0.0
        ev=attach_entry(ev,bars)
        mapping=float(ev["entry_date"].notna().mean()) if len(ev) else 0.0

        dq_checks={
            "entry_mapping_coverage_min":mapping>=float(dq["entry_mapping_coverage_min"]),
            "market_factor_valid_rate_min":factor_rate>=float(dq["market_factor_valid_rate_min"]),
            "canonicalization_conflict_rows_max":conflicts<=int(dq["canonicalization_conflict_rows_max"]),
        }
        if not all(dq_checks.values()):
            raise RuntimeError(f"data quality gate failed {dq_checks}")

        panel=add_matured_labels(ev,bars,cfg["labels"]["primary"],cfg["labels"]["secondary"])
        gates=cfg["fast_reject"]
        ms=int(cfg["baseline"]["min_setup_per_stratum"])
        primary=evaluate(panel,"E1_TREND25_60_label",gates,ms)
        secondary=evaluate(panel,"E1_TREND40_120_label",gates,ms)

        out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)
        pp=out/"e1_forecast_turnaround_v0_2_panel.parquet"
        panel.to_parquet(pp,index=False)

        maturity={
            "primary_matured_setup_rows":int(panel["E1_TREND25_60_label"].notna().sum()),
            "primary_matured_signal_rows":int((panel["e1_signal"] & panel["E1_TREND25_60_label"].notna()).sum()),
            "secondary_matured_setup_rows":int(panel["E1_TREND40_120_label"].notna().sum()),
            "secondary_matured_signal_rows":int((panel["e1_signal"] & panel["E1_TREND40_120_label"].notna()).sum()),
        }

        result={
            "status":"PASS_E1_FORECAST_TURNAROUND_V0_2_RUN",
            "generated_at":datetime.now().isoformat(timespec="seconds"),
            "research_id":cfg["research_id"],
            "frozen_inputs":{
                "identity_panel":str(ip),"identity_sha256":sha256_file(ip),
                "shadow_union":str(sp),"shadow_sha256":sha256_file(sp),
                "trust_verdict":trust.get("verdict"),
            },
            "mechanism":cfg["mechanism"],
            "baseline":cfg["baseline"],
            "labels":cfg["labels"],
            "coverage":{
                "identity_eligible_setup_rows":int(len(ev)),
                "signal_rows":int(ev["e1_signal"].sum()),
                "entry_mapping_coverage":mapping,
                "market_rows_after_identity_filter":int(len(bars)),
                "market_factor_valid_rate":factor_rate,
                "canonicalization_conflict_rows":conflicts,
            },
            "maturity":maturity,
            "data_quality_checks":dq_checks,
            "primary":primary,
            "secondary":secondary,
            "overall_verdict":primary["verdict"],
            "artifacts":{
                "panel":str(pp),
                "panel_sha256":sha256_file(pp),
            },
            "safety":cfg["safety"],
        }
        spath=out/"e1_forecast_turnaround_v0_2_summary.json"
        spath.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2))
        print(f"OUTPUT: {out}")
        return 0
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_E1_FORECAST_TURNAROUND_V0_2","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        return 3

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    raise SystemExit(check(cfg) if a.command=="check" else run(cfg))

if __name__=="__main__": main()
