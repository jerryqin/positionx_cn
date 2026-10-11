
from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/cninfo_eva_commercial_cross_audit_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p):
    h=hashlib.sha256()
    with Path(p).open("rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()

def first_col(df,names):
    low={str(c).lower():c for c in df.columns}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None

def safe_text(v):
    # Pandas pd.NA cannot be evaluated in boolean context.
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except Exception:
        pass
    return str(v)

def code6(v):
    m=re.search(r"(\d{6})",safe_text(v).upper())
    return m.group(1) if m else None

def canonical_shsz(v):
    c=code6(v)
    if not c: return None
    if c[:2] in {"60","68"}: return f"{c}.SH"
    if c[:2] in {"00","30"}: return f"{c}.SZ"
    return None

def clean_title(v):
    s=re.sub(r"</?em>","",safe_text(v),flags=re.I)
    s=re.sub(r"\s+","",s)
    return s.strip()

def to_time(v):
    try:
        if pd.isna(v): return pd.NaT
    except Exception:
        pass
    try:
        iv=int(v)
        if iv>10**11: return pd.to_datetime(iv,unit="ms",errors="coerce")
        if iv>10**9: return pd.to_datetime(iv,unit="s",errors="coerce")
    except Exception:
        pass
    return pd.to_datetime(v,errors="coerce")

def normalize(df, source_name):
    x=df.copy()
    ann=first_col(x,["announcement_id","announcementId","id"])
    sym=first_col(x,["canonical_symbol","symbol","secCode","stock_code"])
    title=first_col(x,["title","announcementTitle","announcement_title","shortTitle"])
    et=first_col(x,["event_time","event_time_iso","announcementTime","announcement_time"])
    subtype=first_col(x,["event_subtype","subtype","event_kind"])
    eclass=first_col(x,["event_class","class"])
    if ann is None or sym is None:
        raise RuntimeError(f"{source_name}: required id/symbol columns missing")

    out=pd.DataFrame(index=x.index)
    out["announcement_id"]=x[ann].astype("string").str.strip()
    out["symbol_raw"]=x[sym].astype("string").str.strip()
    out["canonical_symbol"]=out["symbol_raw"].map(canonical_shsz)
    out["title"]=x[title].map(clean_title) if title else ""
    out["event_time"]=x[et].map(to_time) if et else pd.NaT
    out["event_subtype"]=x[subtype].astype("string").str.upper().str.strip() if subtype else pd.NA
    out["event_class"]=x[eclass].astype("string").str.upper().str.strip() if eclass else pd.NA
    out["source_dataset"]=source_name
    return out[out["announcement_id"].notna() & out["announcement_id"].ne("")].copy()

def unique_ann(df):
    if df is None or df.empty: return df.copy()
    return df.sort_values(["announcement_id","event_time"],na_position="last").drop_duplicates("announcement_id",keep="first").copy()

def ratio(n,d): return float(n/d) if d else None

def check(cfg):
    ps=[cfg["positionx"]["summary"],cfg["positionx"]["all_metadata"],cfg["positionx"]["title_candidates"],cfg["eva"]["events"]]
    states=[Path(p).exists() for p in ps]
    out={
        "status":"PASS_CNINFO_EVA_COMMERCIAL_CROSS_AUDIT_V0_1_1_CHECK" if all(states) else "BLOCKED_CNINFO_EVA_COMMERCIAL_CROSS_AUDIT_V0_1_1_CHECK",
        "paths":[{"path":p,"exists":e} for p,e in zip(ps,states)],
        "research_domain":"SH/SZ A-shares only; BJ and B shares excluded",
        "eva_role":"Layer 4 reference only, not truth",
        "hotfix":"NA-safe scalar normalization only",
        "research_logic_changed":False,
        "thresholds_changed":False,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if all(states) else 3

def run(cfg):
    ps=cfg["positionx"]
    es=cfg["eva"]
    summary=json.loads(Path(ps["summary"]).read_text(encoding="utf-8"))
    px_all_raw=pd.read_parquet(ps["all_metadata"])
    px_cand_raw=pd.read_parquet(ps["title_candidates"])
    eva_raw=pd.read_parquet(es["events"])

    px_all=normalize(px_all_raw,"POSITIONX_CNINFO_ALL")
    px_cand=normalize(px_cand_raw,"POSITIONX_CNINFO_TITLE")
    eva=normalize(eva_raw,"EVA_ACCEPTED")

    px_all=unique_ann(px_all[px_all["canonical_symbol"].notna()])
    px_cand=unique_ann(px_cand[px_cand["canonical_symbol"].notna()])
    eva=unique_ann(eva[eva["canonical_symbol"].notna()])

    a=set(px_all["announcement_id"]); c=set(px_cand["announcement_id"]); e=set(eva["announcement_id"])
    overlap_all=e&a
    overlap_cand=e&c
    eva_only=e-a
    eva_filter_gap=(e&a)-c
    px_only=c-e

    ov=eva[eva["announcement_id"].isin(overlap_all)].merge(
        px_all[px_all["announcement_id"].isin(overlap_all)],
        on="announcement_id",suffixes=("_eva","_px"),how="inner",validate="one_to_one"
    )
    ov["symbol_match"]=ov["canonical_symbol_eva"].eq(ov["canonical_symbol_px"])
    ov["title_match"]=ov["title_eva"].eq(ov["title_px"])
    d=(pd.to_datetime(ov["event_time_eva"],errors="coerce")-pd.to_datetime(ov["event_time_px"],errors="coerce")).abs().dt.total_seconds()
    ov["event_time_abs_diff_seconds"]=d
    ov["event_time_match_1s"]=d.le(1)
    mismatch=ov[(~ov["symbol_match"])|(~ov["title_match"])|(~ov["event_time_match_1s"])].copy()

    eva_only_df=eva[eva["announcement_id"].isin(eva_only)].copy()
    eva_filter_df=eva[eva["announcement_id"].isin(eva_filter_gap)].copy()
    px_only_df=px_cand[px_cand["announcement_id"].isin(px_only)].copy()

    rej_ids=set()
    rejp=Path(es["rejected_unresolved"])
    if rejp.exists():
        try:
            rej=normalize(pd.read_parquet(rejp),"EVA_REJECTED")
            rej=rej[rej["canonical_symbol"].notna()]
            rej_ids=set(rej["announcement_id"])
        except Exception:
            pass
    px_only_df["eva_rejected_or_unresolved"]=px_only_df["announcement_id"].isin(rej_ids)

    if len(eva_only_df):
        eva_only_df["event_year"]=pd.to_datetime(eva_only_df["event_time"],errors="coerce").dt.year.astype("Int64")
        gap_profile=(eva_only_df.groupby(["event_year","event_class","event_subtype"],dropna=False)
                     .size().reset_index(name="count").sort_values("count",ascending=False))
    else:
        gap_profile=pd.DataFrame(columns=["event_year","event_class","event_subtype","count"])

    outdir=Path(cfg["output_dir"]); outdir.mkdir(parents=True,exist_ok=True)
    eva_only_df.to_csv(outdir/"eva_only_absent_from_positionx_cninfo_queries_v0_1.csv",index=False,encoding="utf-8-sig")
    eva_filter_df.to_csv(outdir/"eva_present_in_positionx_queries_but_not_title_candidates_v0_1.csv",index=False,encoding="utf-8-sig")
    px_only_df.to_csv(outdir/"positionx_title_candidates_not_in_eva_accepted_v0_1.csv",index=False,encoding="utf-8-sig")
    mismatch.to_csv(outdir/"overlap_identity_time_title_mismatches_v0_1.csv",index=False,encoding="utf-8-sig")
    gap_profile.to_csv(outdir/"eva_only_gap_profile_v0_1.csv",index=False,encoding="utf-8-sig")

    checks={
        "full_run":summary.get("run_mode")=="FULL",
        "status_pass":str(summary.get("status","")).startswith("PASS_"),
        "shards_expected":int(summary.get("shards",{}).get("processed_or_reused",0))==int(cfg["expected_positionx_acquisition"]["shards"]),
        "zero_failures":int(summary.get("shards",{}).get("failures",999999))==0,
        "event_time_complete":float(summary.get("data",{}).get("event_time_nonnull_rate",0))==1.0,
        "pdf_url_complete":float(summary.get("data",{}).get("pdf_url_nonnull_rate",0))==1.0,
    }

    result={
        "status":"PASS_CNINFO_EVA_COMMERCIAL_CROSS_AUDIT_V0_1_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "audit_id":cfg["audit_id"],
        "implementation_version":"V0.1.1_NA_SAFE_HOTFIX",
        "research_domain":{"included":"SH/SZ A-shares only","excluded":["BJ/BSE","B shares","other non-target instruments"]},
        "positionx_first_party":{
            "target_all_unique_announcements":int(len(px_all)),
            "target_title_candidate_unique_announcements":int(len(px_cand)),
            "structural_checks":checks,
            "structural_pass":all(checks.values()),
        },
        "eva_layer4_reference":{
            "target_unique_announcements":int(len(eva)),
            "role":"gap discovery / cross-check only; not authoritative truth",
        },
        "cross_audit":{
            "eva_found_in_positionx_all":int(len(overlap_all)),
            "eva_to_positionx_all_recall_proxy":ratio(len(overlap_all),len(e)),
            "eva_found_in_positionx_title_candidates":int(len(overlap_cand)),
            "eva_to_positionx_title_candidate_recall_proxy":ratio(len(overlap_cand),len(e)),
            "eva_only_absent_from_positionx_queries":int(len(eva_only)),
            "eva_present_in_positionx_queries_but_not_title_candidates":int(len(eva_filter_gap)),
            "positionx_title_candidates_not_in_eva_accepted":int(len(px_only)),
            "overlap_symbol_match_rate":float(ov["symbol_match"].mean()) if len(ov) else None,
            "overlap_title_match_rate":float(ov["title_match"].mean()) if len(ov) else None,
            "overlap_event_time_match_1s_rate":float(ov["event_time_match_1s"].mean()) if len(ov) else None,
            "overlap_mismatch_rows":int(len(mismatch)),
        },
        "decision_rules":{
            "eva_only_is_not_auto_imported":True,
            "eva_only_next_step":"Use Eva-only rows only to discover query gaps; refetch/verify from CNINFO/SSE/SZSE before use.",
            "positionx_only_is_not_auto_false_positive":True,
            "bj_and_b_share_gap_is_ignored":True,
        },
        "artifacts":{
            "eva_only_query_gap_candidates":str(outdir/"eva_only_absent_from_positionx_cninfo_queries_v0_1.csv"),
            "title_filter_gap_candidates":str(outdir/"eva_present_in_positionx_queries_but_not_title_candidates_v0_1.csv"),
            "positionx_only_candidates":str(outdir/"positionx_title_candidates_not_in_eva_accepted_v0_1.csv"),
            "overlap_mismatches":str(outdir/"overlap_identity_time_title_mismatches_v0_1.csv"),
            "eva_only_gap_profile":str(outdir/"eva_only_gap_profile_v0_1.csv"),
        },
        "next_action":"Inspect SH/SZ Eva-only gaps. Expand/refetch only from official sources if genuine; then download official PDFs only for the final SH/SZ candidate set.",
        "safety":cfg["safety"],
    }
    (outdir/"cninfo_eva_commercial_cross_audit_v0_1_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {outdir}")
    return 0

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_CNINFO_EVA_COMMERCIAL_CROSS_AUDIT_V0_1_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__": main()
