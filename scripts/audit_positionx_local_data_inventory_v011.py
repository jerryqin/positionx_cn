
from __future__ import annotations
import argparse, json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/positionx_local_data_inventory_v0_1_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def norm_path(x):
    p=str(x or "").lower().replace("/","\\")
    while "\\\\" in p:
        p=p.replace("\\\\","\\")
    return p

def provenance(path, cfg=None):
    p=norm_path(path)
    if "\\incoming\\eva\\" in p:
        return "EVA_INCOMING"
    if "\\incoming\\" in p:
        return "OTHER_INCOMING"
    if any(tok in p for tok in ["\\events\\","\\research\\","\\reference\\","\\catalog\\"]):
        return "POSITIONX_NATIVE"
    return "OTHER_LOCAL"

def path_family(path):
    p=norm_path(path)
    if "forecast_semantic" in p or "cninfo_earnings" in p or "earnings" in p:
        return "EARNINGS_FORECAST_FOCUSED"
    if "cninfo" in p:
        return "CNINFO_OTHER_OR_UNKNOWN"
    if "announcement" in p or "event" in p:
        return "EVENT_OR_ANNOUNCEMENT"
    return "OTHER"

def number(s):
    return pd.to_numeric(s, errors="coerce").fillna(0)

def run(cfg):
    sp=Path(cfg["input"]["structured_inventory_csv"])
    pp=Path(cfg["input"]["pdf_inventory_json"])
    if not sp.exists():
        raise FileNotFoundError(sp)
    if not pp.exists():
        raise FileNotFoundError(pp)

    df=pd.read_csv(sp)
    required={"path","hard_hits","soft_hits","earnings_hits"}
    missing=required-set(df.columns)
    if missing:
        raise RuntimeError(f"structured inventory missing columns: {sorted(missing)}")

    df["provenance"]=df["path"].map(provenance)
    df["path_family"]=df["path"].map(path_family)
    for c in ["hard_hits","soft_hits","earnings_hits"]:
        df[c]=number(df[c]).astype(int)
    df["commercial_hits"]=df["hard_hits"]+df["soft_hits"]
    df["is_commercial_candidate"]=(
        (df["hard_hits"]>=int(cfg["candidate"]["hard_hits_min"]))
        | (df["soft_hits"]>=int(cfg["candidate"]["soft_hits_min"]))
    )

    pdf=json.loads(pp.read_text(encoding="utf-8"))
    pdfdf=pd.DataFrame(pdf)
    if not pdfdf.empty:
        pdfdf["provenance"]=pdfdf["directory"].map(provenance)
        pdfdf["path_family"]=pdfdf["directory"].map(path_family)
        pdfdf["count"]=number(pdfdf["count"]).astype(int)
        pdfdf["bytes"]=number(pdfdf["bytes"]).astype("int64")

    def agg_rows(g):
        return {
            "structured_assets":int(len(g)),
            "candidate_assets":int(g["is_commercial_candidate"].sum()),
            "hard_hits":int(g["hard_hits"].sum()),
            "soft_hits":int(g["soft_hits"].sum()),
            "earnings_hits":int(g["earnings_hits"].sum()),
        }

    structured_by_prov={prov:agg_rows(g) for prov,g in df.groupby("provenance",dropna=False)}

    pdf_by_prov={}
    if not pdfdf.empty:
        for prov,g in pdfdf.groupby("provenance",dropna=False):
            pdf_by_prov[prov]={
                "pdf_count":int(g["count"].sum()),
                "pdf_bytes":int(g["bytes"].sum()),
                "directories":int(len(g)),
            }

    def ranked(prov_values):
        z=df[df["provenance"].isin(prov_values) & df["is_commercial_candidate"]].copy()
        z["score"]=z["hard_hits"]*10 + z["soft_hits"]*5 - z["earnings_hits"]
        return z.sort_values(["score","commercial_hits"],ascending=[False,False])

    native=ranked(["POSITIONX_NATIVE"])
    eva=ranked(["EVA_INCOMING"])
    other=ranked(["OTHER_INCOMING","OTHER_LOCAL"])

    # Forecast-focused corpora do not establish existence of reusable commercial-catalyst data.
    native_nonforecast=native[native["path_family"]!="EARNINGS_FORECAST_FOCUSED"].copy()

    outdir=Path(cfg["output_dir"])
    outdir.mkdir(parents=True,exist_ok=True)

    native.to_csv(outdir/"positionx_native_candidate_assets_v0_1_1.csv",index=False,encoding="utf-8-sig")
    eva.to_csv(outdir/"eva_incoming_candidate_assets_v0_1_1.csv",index=False,encoding="utf-8-sig")
    other.to_csv(outdir/"other_local_candidate_assets_v0_1_1.csv",index=False,encoding="utf-8-sig")
    df.to_csv(outdir/"all_structured_assets_with_provenance_v0_1_1.csv",index=False,encoding="utf-8-sig")
    if not pdfdf.empty:
        pdfdf.to_csv(outdir/"pdf_directories_with_provenance_v0_1_1.csv",index=False,encoding="utf-8-sig")

    def recs(z,n=30):
        keep=[c for c in [
            "source_type","path","table","row_count","rows_sampled",
            "hard_hits","soft_hits","earnings_hits","commercial_hits",
            "candidate_text_columns","provenance","path_family","score","error"
        ] if c in z.columns]
        q=z[keep].head(n).copy()
        return q.where(pd.notna(q),None).to_dict("records")

    native_confirmed=bool(len(native_nonforecast)>0)
    result={
        "status":"PASS_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "inventory_id":cfg["inventory_id"],
        "structured_by_provenance":structured_by_prov,
        "pdf_by_provenance":pdf_by_prov,
        "positionx_native":{
            "commercial_candidate_assets":int(len(native)),
            "nonforecast_commercial_candidate_assets":int(len(native_nonforecast)),
            "commercial_catalyst_data_confirmed_present":native_confirmed,
            "top_candidates":recs(native_nonforecast,30),
        },
        "eva_incoming":{
            "commercial_candidate_assets":int(len(eva)),
            "top_candidates":recs(eva,10),
            "trust_state":"UNTRUSTED_EXTERNAL_INCOMING",
            "priority_layer":4,
        },
        "other_local":{
            "commercial_candidate_assets":int(len(other)),
            "top_candidates":recs(other,20),
        },
        "decision":{
            "if_positionx_native_present":"Inspect and reuse POSITIONX_NATIVE candidates before any acquisition.",
            "if_positionx_native_absent":"Proceed to direct first-party CNINFO/SSE/SZSE/BSE acquisition; do not wait for Eva.",
            "eva_role":"Layer 4 only: gap filler / independent assistant after local, first-party and free-public routes are exhausted.",
            "paid_professional_role":"Layer 5.",
        },
        "data_priority":[
            "LOCAL_ASSETS",
            "DIRECT_FIRST_PARTY",
            "FREE_PUBLIC_THIRD_PARTY",
            "EVA",
            "PAID_PROFESSIONAL"
        ],
        "artifacts":{
            "positionx_native_candidates":str(outdir/"positionx_native_candidate_assets_v0_1_1.csv"),
            "eva_incoming_candidates":str(outdir/"eva_incoming_candidate_assets_v0_1_1.csv"),
            "all_assets_with_provenance":str(outdir/"all_structured_assets_with_provenance_v0_1_1.csv"),
        },
        "safety":cfg["safety"],
    }

    spath=outdir/"positionx_local_data_inventory_v0_1_1_summary.json"
    spath.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {outdir}")
    return 0

def check(cfg):
    ps=[Path(cfg["input"]["structured_inventory_csv"]),Path(cfg["input"]["pdf_inventory_json"])]
    ok=all(p.exists() for p in ps)
    out={
        "status":"PASS_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_1_CHECK" if ok else "BLOCKED_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_1_CHECK",
        "inputs":[{"path":str(p),"exists":p.exists()} for p in ps],
        "read_only":True,
        "network":False,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if ok else 3

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    a=p.parse_args()
    cfg=load_cfg()
    try:
        raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_POSITIONX_LOCAL_DATA_INVENTORY_V0_1_1",
            "error":f"{type(e).__name__}: {e}",
        },ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
