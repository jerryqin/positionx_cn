
from __future__ import annotations
import argparse, hashlib, json, re
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/opportunity/e1_backfill_trust_audit_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(p: Path):
    h=hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""):
            h.update(c)
    return h.hexdigest()

def suffix_of(s):
    if s is None or pd.isna(s): return None
    x=str(s).strip().upper()
    m=re.search(r"\.(SH|SZ|BJ)$",x)
    if m: return m.group(1)
    m=re.match(r"^(SH|SZ|BJ)[\.\-_]?",x)
    if m: return m.group(1)
    return None

def normalize_status_rows(obj):
    rows=obj.get("per_symbol_status",[])
    if not isinstance(rows,list):
        raise RuntimeError("SOURCE_MANIFEST per_symbol_status is not a list")
    m=pd.DataFrame(rows)
    if "symbol" not in m.columns or "status" not in m.columns:
        raise RuntimeError("per_symbol_status requires symbol/status")
    m["symbol"]=m["symbol"].astype("string").str.upper().str.strip()
    m["status"]=m["status"].astype("string").str.upper().str.strip()
    return m

def pick_col(df,*candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None

def deterministic_symbol_rank(symbol):
    return hashlib.sha256(str(symbol).encode("utf-8")).hexdigest()

def choose_reference_symbols(req, manifest, max_symbols):
    success=set(manifest.loc[manifest["status"].eq("SUCCESS"),"symbol"])
    r=req[req["symbol"].isin(success)].copy()
    r["setup_events"]=pd.to_numeric(r.get("setup_events"),errors="coerce").fillna(0)
    r["board_bucket"]=r.get("board_bucket",pd.Series("UNKNOWN",index=r.index)).astype("string")

    chosen=[]

    # All B-shares because Eva used a different source for them.
    for b in ["SZ_B","SH_B"]:
        chosen += r.loc[r["board_bucket"].eq(b),"symbol"].tolist()

    # Highest research-impact symbols per major board.
    for b in ["SZ_MAIN","SH_MAIN","CHINEXT","STAR","BEIJING"]:
        g=r[r["board_bucket"].eq(b)].sort_values(["setup_events","symbol"],ascending=[False,True])
        chosen += g.head(8)["symbol"].tolist()

    # Deterministic broad sample, independent of price outcomes.
    remaining=sorted(set(r["symbol"])-set(chosen),key=deterministic_symbol_rank)
    chosen += remaining[:max(0,max_symbols-len(set(chosen)))]

    out=[]
    seen=set()
    for s in chosen:
        if s not in seen:
            out.append(s); seen.add(s)
        if len(out)>=max_symbols: break
    return out

def sample_dates(backfill, symbols, dates_per_symbol):
    rows=[]
    for sym in symbols:
        g=backfill[backfill["symbol"].eq(sym)].sort_values("trade_date")
        if g.empty: continue
        n=len(g)
        if dates_per_symbol <= 1:
            idxs=[n//2]
        else:
            fracs=[(i+1)/(dates_per_symbol+1) for i in range(dates_per_symbol)]
            idxs=sorted(set(min(n-1,max(0,int(round(f*(n-1))))) for f in fracs))
        for i in idxs:
            row=g.iloc[i]
            rows.append({
                "symbol":sym,
                "trade_date":row["trade_date"],
                "eva_open":row["open"],
                "eva_high":row["high"],
                "eva_low":row["low"],
                "eva_close":row["close"],
                "eva_pre_close":row["pre_close"],
                "eva_source_name":row.get("source_name"),
                "eva_source_symbol":row.get("source_symbol"),
            })
    return pd.DataFrame(rows)

def run(cfg):
    reqp=Path(cfg["request_csv"]); bp=Path(cfg["backfill_parquet"]); mp=Path(cfg["source_manifest"])
    for p in [reqp,bp,mp]:
        if not p.exists(): raise FileNotFoundError(p)

    shas={"request":sha256_file(reqp),"backfill":sha256_file(bp),"manifest":sha256_file(mp)}
    sha_checks={
        "request_sha_exact":shas["request"]==cfg["expected"]["request_sha256"],
        "backfill_sha_exact":shas["backfill"]==cfg["expected"]["backfill_sha256"],
        "manifest_sha_exact":shas["manifest"]==cfg["expected"]["manifest_sha256"],
    }

    req=pd.read_csv(reqp)
    req["symbol"]=req["symbol"].astype("string").str.upper().str.strip()
    req["board_bucket"]=req.get("board_bucket",pd.Series("UNKNOWN",index=req.index)).astype("string")

    back=pd.read_parquet(bp)
    back["symbol"]=back["symbol"].astype("string").str.upper().str.strip()
    back["trade_date"]=pd.to_datetime(back["trade_date"],errors="coerce").dt.normalize()

    obj=json.loads(mp.read_text(encoding="utf-8"))
    man=normalize_status_rows(obj)

    srcsym_col=pick_col(man,"source_symbol","sourceSymbol","source_code")
    srcname_col=pick_col(man,"source_name","source","provider")
    notes_col=pick_col(man,"notes","note","message","reason")

    j=req.merge(man,on="symbol",how="left",suffixes=("_request","_manifest"),validate="one_to_one")
    j["request_suffix"]=j["symbol"].map(suffix_of)
    j["source_suffix"]=j[srcsym_col].map(suffix_of) if srcsym_col else None
    j["suffix_mismatch"]=(
        j["request_suffix"].notna()
        & pd.Series(j["source_suffix"],index=j.index).notna()
        & j["request_suffix"].ne(j["source_suffix"])
    ) if srcsym_col else False

    j["non_equity_request"]=j["board_bucket"].eq("OTHER")
    if notes_col:
        n=j[notes_col].astype("string").fillna("").str.lower()
        j["manifest_mentions_non_equity"]=n.str.contains("债|bond|可转",regex=True)
    else:
        j["manifest_mentions_non_equity"]=False

    identity_issues=j[
        j["suffix_mismatch"] | j["non_equity_request"] | j["manifest_mentions_non_equity"]
    ].copy()

    source_counts={}
    if srcname_col:
        source_counts={str(k):int(v) for k,v in man.loc[man["status"].eq("SUCCESS"),srcname_col].astype("string").value_counts(dropna=False).items()}

    # provenance completeness among SUCCESS rows
    succ=man[man["status"].eq("SUCCESS")].copy()
    provenance={
        "success_rows":int(len(succ)),
        "source_name_nonnull_rate":(
            float(succ[srcname_col].astype("string").fillna("").str.strip().ne("").mean())
            if srcname_col else 0.0
        ),
        "source_symbol_nonnull_rate":(
            float(succ[srcsym_col].astype("string").fillna("").str.strip().ne("").mean())
            if srcsym_col else 0.0
        ),
    }

    selected=choose_reference_symbols(req,man,int(cfg["reference_sample"]["max_symbols"]))
    ref=sample_dates(back,selected,int(cfg["reference_sample"]["dates_per_symbol"]))

    out=Path(cfg["output_dir"]); out.mkdir(parents=True,exist_ok=True)
    ip=out/"instrument_identity_issues_v0_1.csv"
    cols=[c for c in [
        "symbol","board_bucket","setup_events","status",
        srcname_col,srcsym_col,notes_col,
        "request_suffix","source_suffix","suffix_mismatch",
        "non_equity_request","manifest_mentions_non_equity"
    ] if c and c in identity_issues.columns]
    identity_issues[cols].to_csv(ip,index=False,encoding="utf-8-sig")

    rp=out/"independent_price_check_request_v0_1.csv"
    ref.to_csv(rp,index=False,encoding="utf-8-sig")

    report={
        "status":"PASS_E1_BACKFILL_TRUST_AUDIT_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "research_id":cfg["research_id"],
        "input_sha_checks":sha_checks,
        "request_rows":int(len(req)),
        "manifest_rows":int(len(man)),
        "backfill_rows":int(len(back)),
        "backfill_symbols":int(back["symbol"].nunique()),
        "manifest_status_counts":{str(k):int(v) for k,v in man["status"].value_counts(dropna=False).items()},
        "successful_source_counts":source_counts,
        "provenance_completeness":provenance,
        "identity": {
            "suffix_mismatch_rows":int(j["suffix_mismatch"].sum()) if "suffix_mismatch" in j else 0,
            "non_equity_request_rows":int(j["non_equity_request"].sum()),
            "manifest_non_equity_mentions":int(j["manifest_mentions_non_equity"].sum()),
            "identity_issue_rows":int(len(identity_issues)),
        },
        "independent_reference_sample":{
            "symbols":int(len(selected)),
            "price_points":int(len(ref)),
            "selection_policy":"all B-shares + top-impact per board + deterministic hash sample; no price/outcome conditioning",
            "request_csv":str(rp),
            "request_csv_sha256":sha256_file(rp),
        },
        "gates":{
            "structural_input_sha_pass":all(sha_checks.values()),
            "identity_resolution_required":bool(len(identity_issues)>0),
            "independent_price_verification_required":True,
            "e1_rerun_allowed":False,
            "canonical_promotion_allowed":False,
        },
        "interpretation":"Current backfill acceptance is STRUCTURAL ONLY. Resolve instrument identity issues and complete an independent cross-source price verification before E1 rerun or canonical promotion.",
        "artifacts":{
            "identity_issues_csv":str(ip),
            "identity_issues_sha256":sha256_file(ip),
            "independent_price_check_request_csv":str(rp),
        }
    }
    sp=out/"e1_backfill_trust_audit_v0_1_summary.json"
    sp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {out}")
    return 0

def check(cfg):
    out={"status":"PASS_E1_BACKFILL_TRUST_AUDIT_V0_1_CHECK"}
    for key in ["request_csv","backfill_parquet","source_manifest"]:
        p=Path(cfg[key]); out[key]={"path":str(p),"exists":p.exists()}
        if not p.exists(): out["status"]="BLOCKED_E1_BACKFILL_TRUST_AUDIT_V0_1_CHECK"
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if out["status"].startswith("PASS") else 3

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try:
        raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_E1_BACKFILL_TRUST_AUDIT_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
