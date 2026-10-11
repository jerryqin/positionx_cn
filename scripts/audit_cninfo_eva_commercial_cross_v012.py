
from __future__ import annotations
import argparse, json, re, hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/cninfo_eva_commercial_cross_audit_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def safe_text(v):
    if v is None: return ""
    try:
        if pd.isna(v): return ""
    except Exception:
        pass
    return str(v)

def first_col(df,names):
    low={str(c).lower():c for c in df.columns}
    for n in names:
        if n.lower() in low: return low[n.lower()]
    return None

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
    return re.sub(r"\s+","",s).strip()

def parse_cninfo_epoch_local(v):
    """CNINFO announcementTime epoch -> Asia/Shanghai wall-clock, tz-naive."""
    try:
        if pd.isna(v): return pd.NaT
    except Exception:
        pass
    try:
        iv=int(v)
        if iv>10**11:
            return pd.to_datetime(iv,unit="ms",utc=True,errors="coerce").tz_convert("Asia/Shanghai").tz_localize(None)
        if iv>10**9:
            return pd.to_datetime(iv,unit="s",utc=True,errors="coerce").tz_convert("Asia/Shanghai").tz_localize(None)
    except Exception:
        pass
    z=pd.to_datetime(v,errors="coerce")
    if pd.isna(z): return pd.NaT
    if getattr(z,"tzinfo",None) is not None:
        return z.tz_convert("Asia/Shanghai").tz_localize(None)
    return z

def parse_eva_local(v):
    """Eva report states event_time is CNINFO announcementTime expressed in CST."""
    try:
        if pd.isna(v): return pd.NaT
    except Exception:
        pass
    z=pd.to_datetime(v,errors="coerce")
    if pd.isna(z): return pd.NaT
    if getattr(z,"tzinfo",None) is not None:
        return z.tz_convert("Asia/Shanghai").tz_localize(None)
    return z

def normalize_px(df, source_name):
    x=df.copy()
    ann=first_col(x,["announcement_id","announcementId","id"])
    sym=first_col(x,["canonical_symbol","symbol","secCode","stock_code"])
    title=first_col(x,["title","announcementTitle","announcement_title","shortTitle"])
    # Prefer raw CNINFO epoch field over derived event_time.
    et=first_col(x,["announcementTime","announcement_time","event_time"])
    qk=first_col(x,["query_keywords","query_keyword"])
    if ann is None or sym is None or et is None:
        raise RuntimeError(f"{source_name}: id/symbol/time column missing")
    out=pd.DataFrame(index=x.index)
    out["announcement_id"]=x[ann].astype("string").str.strip()
    out["canonical_symbol"]=x[sym].map(canonical_shsz)
    out["title"]=x[title].map(clean_title) if title else ""
    out["event_time_local"]=x[et].map(parse_cninfo_epoch_local)
    out["query_keywords"]=x[qk].astype("string") if qk else pd.NA
    return out[out["announcement_id"].notna() & out["announcement_id"].ne("")].copy()

def normalize_eva(df, source_name):
    x=df.copy()
    ann=first_col(x,["announcement_id","announcementId","id"])
    sym=first_col(x,["canonical_symbol","symbol","secCode","stock_code"])
    title=first_col(x,["title","announcementTitle","announcement_title","shortTitle"])
    et=first_col(x,["event_time","event_time_iso","announcementTime","announcement_time"])
    subtype=first_col(x,["event_subtype","subtype","event_kind"])
    eclass=first_col(x,["event_class","class"])
    qk=first_col(x,["query_keyword","query_keywords","discovery_keyword"])
    if ann is None or sym is None or et is None:
        raise RuntimeError(f"{source_name}: id/symbol/time column missing")
    out=pd.DataFrame(index=x.index)
    out["announcement_id"]=x[ann].astype("string").str.strip()
    out["canonical_symbol"]=x[sym].map(canonical_shsz)
    out["title"]=x[title].map(clean_title) if title else ""
    out["event_time_local"]=x[et].map(parse_eva_local)
    out["event_subtype"]=x[subtype].astype("string").str.upper().str.strip() if subtype else pd.NA
    out["event_class"]=x[eclass].astype("string").str.upper().str.strip() if eclass else pd.NA
    out["query_keyword"]=x[qk].astype("string").str.strip() if qk else pd.NA
    return out[out["announcement_id"].notna() & out["announcement_id"].ne("")].copy()

def unique_ann(df):
    return df.sort_values(["announcement_id","event_time_local"],na_position="last").drop_duplicates("announcement_id",keep="first").copy()

def split_keywords(series):
    out=set()
    for v in series.dropna():
        for x in re.split(r"[|;,，；]+",str(v)):
            x=x.strip()
            if x: out.add(x)
    return sorted(out)

def contains_any(title, kws):
    t=safe_text(title)
    return any(k in t for k in kws)

def check(cfg):
    paths=[
        cfg["positionx"]["all_metadata"],cfg["positionx"]["title_candidates"],
        cfg["eva"]["events"],cfg["positionx"]["summary"]
    ]
    ok=all(Path(p).exists() for p in paths)
    print(json.dumps({
        "status":"PASS_CNINFO_EVA_CROSS_AUDIT_V0_1_2_CHECK" if ok else "BLOCKED_CNINFO_EVA_CROSS_AUDIT_V0_1_2_CHECK",
        "paths":[{"path":p,"exists":Path(p).exists()} for p in paths],
        "research_domain":"SH/SZ A-shares only",
        "time_semantics":"CNINFO epoch and Eva CST normalized to Asia/Shanghai wall-clock",
        "eva_role":"Layer 4 gap discovery only",
        "market_outcomes":False
    },ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(cfg):
    px_raw=pd.read_parquet(cfg["positionx"]["all_metadata"])
    pc_raw=pd.read_parquet(cfg["positionx"]["title_candidates"])
    ev_raw=pd.read_parquet(cfg["eva"]["events"])

    px=unique_ann(normalize_px(px_raw,"PX")[lambda z:z["canonical_symbol"].notna()])
    pc=unique_ann(normalize_px(pc_raw,"PC")[lambda z:z["canonical_symbol"].notna()])
    ev=unique_ann(normalize_eva(ev_raw,"EVA")[lambda z:z["canonical_symbol"].notna()])

    A=set(px["announcement_id"]); C=set(pc["announcement_id"]); E=set(ev["announcement_id"])
    overlap=E&A
    eva_only=E-A
    filter_gap=(E&A)-C

    ov=ev[ev["announcement_id"].isin(overlap)].merge(
        px[px["announcement_id"].isin(overlap)],
        on="announcement_id",suffixes=("_eva","_px"),validate="one_to_one"
    )
    ov["symbol_match"]=ov["canonical_symbol_eva"].eq(ov["canonical_symbol_px"])
    ov["title_match"]=ov["title_eva"].eq(ov["title_px"])
    diff=(ov["event_time_local_eva"]-ov["event_time_local_px"]).abs().dt.total_seconds()
    ov["event_time_diff_seconds"]=diff
    ov["event_time_match_1s"]=diff.le(1)

    # Query vocabulary actually used by PositionX.
    px_query_keywords=split_keywords(px_raw[first_col(px_raw,["query_keywords","query_keyword"])]) \
        if first_col(px_raw,["query_keywords","query_keyword"]) else []

    eo=ev[ev["announcement_id"].isin(eva_only)].copy()
    eo["title_contains_any_positionx_query_keyword"]=eo["title"].map(lambda t:contains_any(t,px_query_keywords))
    eo["event_year"]=eo["event_time_local"].dt.year.astype("Int64")

    fg=ev[ev["announcement_id"].isin(filter_gap)].copy()

    gap_by_subtype=(eo.groupby(["event_class","event_subtype"],dropna=False)
                    .size().reset_index(name="count").sort_values("count",ascending=False))
    gap_by_year=(eo.groupby(["event_year","event_class"],dropna=False)
                 .size().reset_index(name="count").sort_values(["event_year","count"],ascending=[True,False]))
    if "query_keyword" in eo.columns:
        gap_by_eva_keyword=(eo.groupby(["query_keyword","event_class","event_subtype"],dropna=False)
                            .size().reset_index(name="count").sort_values("count",ascending=False))
    else:
        gap_by_eva_keyword=pd.DataFrame()

    out=Path(cfg["output_dir"])
    out.mkdir(parents=True,exist_ok=True)

    eo.to_csv(out/"eva_only_query_gap_diagnostic_v0_1_2.csv",index=False,encoding="utf-8-sig")
    fg.to_csv(out/"title_filter_gap_diagnostic_v0_1_2.csv",index=False,encoding="utf-8-sig")
    gap_by_subtype.to_csv(out/"eva_only_gap_by_subtype_v0_1_2.csv",index=False,encoding="utf-8-sig")
    gap_by_year.to_csv(out/"eva_only_gap_by_year_v0_1_2.csv",index=False,encoding="utf-8-sig")
    gap_by_eva_keyword.to_csv(out/"eva_only_gap_by_eva_keyword_v0_1_2.csv",index=False,encoding="utf-8-sig")
    ov[~ov["event_time_match_1s"]].to_csv(out/"normalized_time_mismatches_v0_1_2.csv",index=False,encoding="utf-8-sig")

    title_contains=int(eo["title_contains_any_positionx_query_keyword"].sum()) if len(eo) else 0
    result={
        "status":"PASS_CNINFO_EVA_CROSS_AUDIT_V0_1_2_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "implementation_version":"V0.1.2_TIME_SEMANTICS_AND_QUERY_GAP_DIAGNOSTIC",
        "research_domain":{"included":"SH/SZ A-shares only","excluded":["BJ/BSE","B shares"]},
        "counts":{
            "positionx_all":len(A),
            "positionx_title_candidates":len(C),
            "eva_accepted":len(E),
            "overlap":len(overlap),
            "eva_only_query_gap":len(eva_only),
            "title_filter_gap":len(filter_gap)
        },
        "overlap_consistency":{
            "symbol_match_rate":float(ov["symbol_match"].mean()) if len(ov) else None,
            "title_match_rate":float(ov["title_match"].mean()) if len(ov) else None,
            "event_time_local_match_1s_rate":float(ov["event_time_match_1s"].mean()) if len(ov) else None,
            "time_mismatch_rows":int((~ov["event_time_match_1s"]).sum()) if len(ov) else 0,
            "event_time_diff_seconds_top":{str(k):int(v) for k,v in diff.round().value_counts().head(10).items()} if len(ov) else {}
        },
        "query_gap_diagnostic":{
            "positionx_query_keywords":px_query_keywords,
            "eva_only_title_contains_existing_positionx_keyword":title_contains,
            "eva_only_title_contains_existing_positionx_keyword_rate":float(title_contains/len(eo)) if len(eo) else None,
            "eva_only_title_without_existing_positionx_keyword":int(len(eo)-title_contains),
            "interpretation":"If title already contains a PositionX keyword but announcement was absent, investigate CNINFO search semantics/pagination/query behavior. Otherwise expand query vocabulary using official-source refetch, not direct Eva import."
        },
        "decision":{
            "download_pdfs_now":False,
            "auto_import_eva_only":False,
            "next":"Use the subtype/keyword/year gap profiles to design a small first-party query-expansion V0.2, refetch only official CNINFO/SSE/SZSE, then recompute recall before PDF download."
        },
        "artifacts":{
            "eva_only_gap_rows":str(out/"eva_only_query_gap_diagnostic_v0_1_2.csv"),
            "gap_by_subtype":str(out/"eva_only_gap_by_subtype_v0_1_2.csv"),
            "gap_by_year":str(out/"eva_only_gap_by_year_v0_1_2.csv"),
            "gap_by_eva_keyword":str(out/"eva_only_gap_by_eva_keyword_v0_1_2.csv"),
            "normalized_time_mismatches":str(out/"normalized_time_mismatches_v0_1_2.csv")
        }
    }
    (out/"cninfo_eva_commercial_cross_audit_v0_1_2_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {out}")
    return 0

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_CNINFO_EVA_CROSS_AUDIT_V0_1_2","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__": main()
