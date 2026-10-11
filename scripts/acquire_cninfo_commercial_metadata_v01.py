
from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config/data/cninfo_commercial_metadata_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def sha256_file(path: Path):
    h=hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda:f.read(1024*1024), b""):
            h.update(b)
    return h.hexdigest()

def slug(s: str):
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+","_",str(s)).strip("_")

def month_windows(start_s: str, end_s: str):
    start=pd.Timestamp(start_s).date()
    end=pd.Timestamp(end_s).date()
    y,m=start.year,start.month
    out=[]
    while (y,m) <= (end.year,end.month):
        first=date(y,m,1)
        last=date(y,m,calendar.monthrange(y,m)[1])
        a=max(first,start)
        b=min(last,end)
        if a<=b:
            out.append((a,b))
        if m==12:
            y,m=y+1,1
        else:
            m+=1
    return out

def clean_title(x):
    return re.sub(r"</?em>","",str(x or ""), flags=re.I).strip()

def parse_event_time(v):
    if v is None or (isinstance(v,float) and pd.isna(v)):
        return pd.NaT
    try:
        iv=int(v)
        if iv>10**11:
            return pd.to_datetime(iv,unit="ms",errors="coerce")
        if iv>10**9:
            return pd.to_datetime(iv,unit="s",errors="coerce")
    except Exception:
        pass
    return pd.to_datetime(v,errors="coerce")

def canonical_symbol(sec_code):
    m=re.search(r"(\d{6})",str(sec_code or ""))
    if not m:
        return None
    code=m.group(1)
    p2=code[:2]
    if p2 in {"20","90"}:
        return None
    if p2 in {"43","83","87","88","92"}:
        return f"{code}.BJ"
    if p2 in {"00","30"}:
        return f"{code}.SZ"
    if p2 in {"60","68"}:
        return f"{code}.SH"
    return None

def title_group(title, cfg):
    t=clean_title(title)
    hits=[]
    for group,pats in cfg["title_patterns"].items():
        if any(re.search(p,t,re.I) for p in pats):
            hits.append(group)
    if len(hits)==1:
        return hits[0]
    if len(hits)>1:
        return "AMBIGUOUS"
    return "NO_TITLE_HIT"

def headers(cfg):
    return {
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Referer":cfg["source"]["referer"],
        "X-Requested-With":"XMLHttpRequest",
        "Accept":"application/json, text/javascript, */*; q=0.01",
    }

def cninfo_post(cfg, se_date, page_num, searchkey):
    body=urllib.parse.urlencode({
        "pageNum":int(page_num),
        "pageSize":int(cfg["network"]["page_size"]),
        "column":"",
        "tabName":"fulltext",
        "plate":"",
        "stock":"",
        "searchkey":searchkey,
        "secid":"",
        "category":"",
        "trade":"",
        "seDate":se_date,
        "sortName":"",
        "sortType":"",
        "isHLtitle":"true",
    }).encode("utf-8")

    last=None
    for attempt in range(int(cfg["network"]["retries"])):
        for url in cfg["source"]["api_urls"]:
            try:
                req=urllib.request.Request(url,data=body,method="POST",headers=headers(cfg))
                with urllib.request.urlopen(req,timeout=int(cfg["network"]["timeout_seconds"])) as r:
                    raw=r.read()
                obj=json.loads(raw.decode("utf-8","replace"))
                if not isinstance(obj,dict):
                    raise RuntimeError("CNINFO response is not a JSON object")
                return obj,url
            except Exception as e:
                last=e
        time.sleep(float(cfg["network"]["retry_backoff_seconds"])*(attempt+1))
    raise RuntimeError(f"CNINFO failed seDate={se_date} page={page_num} kw={searchkey}: {last!r}")

def raw_paths(out_root: Path, month_key: str, group: str, kw: str):
    base=out_root/"raw"/month_key
    base.mkdir(parents=True,exist_ok=True)
    stem=f"{group}__{slug(kw)}"
    return (
        base/f"{stem}.json.gz",
        base/f"{stem}.parquet",
        base/f"{stem}.manifest.json",
    )

def valid_completed(rawp, parqp, manp):
    if not (rawp.exists() and parqp.exists() and manp.exists()):
        return False
    try:
        m=json.loads(manp.read_text(encoding="utf-8"))
        return (
            m.get("status")=="SUCCESS"
            and m.get("raw_sha256")==sha256_file(rawp)
            and m.get("parquet_sha256")==sha256_file(parqp)
        )
    except Exception:
        return False

def fetch_shard(cfg, a: date, b: date, group: str, kw: str, force=False):
    out_root=Path(cfg["output_root"])
    month_key=f"{a.year:04d}-{a.month:02d}"
    rawp,parqp,manp=raw_paths(out_root,month_key,group,kw)
    if not force and valid_completed(rawp,parqp,manp):
        m=json.loads(manp.read_text(encoding="utf-8"))
        return {"status":"SKIP_EXISTING","manifest":m}

    se_date=f"{a.isoformat()}~{b.isoformat()}"
    first,url_used=cninfo_post(cfg,se_date,1,kw)
    total_pages=int(first.get("totalpages") or first.get("totalPages") or 0)
    if total_pages<0 or total_pages>int(cfg["network"]["max_pages_guard"]):
        raise RuntimeError(f"page guard failed {month_key} {kw}: {total_pages}")

    payloads=[first]
    for p in range(2,total_pages+1):
        time.sleep(float(cfg["network"]["delay_seconds"]))
        obj,_=cninfo_post(cfg,se_date,p,kw)
        payloads.append(obj)

    rows=[]
    for page_num,payload in enumerate(payloads,1):
        for row in payload.get("announcements") or []:
            r=dict(row)
            r["query_group"]=group
            r["query_keyword"]=kw
            r["query_month"]=month_key
            r["query_se_date"]=se_date
            r["query_page"]=page_num
            rows.append(r)

    with gzip.open(rawp,"wt",encoding="utf-8") as f:
        json.dump({
            "query":{
                "group":group,"keyword":kw,"seDate":se_date,
                "page_size":cfg["network"]["page_size"],
                "total_pages":total_pages,
                "api_url_used":url_used,
            },
            "payloads":payloads,
        },f,ensure_ascii=False)

    df=pd.DataFrame(rows)
    if df.empty:
        df=pd.DataFrame(columns=[
            "announcementId","secCode","secName","announcementTitle","announcementTime",
            "storageTime","adjunctUrl","query_group","query_keyword","query_month","query_se_date","query_page"
        ])
    df.to_parquet(parqp,index=False)

    man={
        "status":"SUCCESS",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "source":"CNINFO",
        "group":group,
        "keyword":kw,
        "seDate":se_date,
        "month":month_key,
        "total_pages":total_pages,
        "rows":int(len(df)),
        "distinct_announcement_ids":int(df["announcementId"].astype("string").nunique()) if "announcementId" in df else 0,
        "api_url_used":url_used,
        "raw_path":str(rawp),
        "raw_sha256":sha256_file(rawp),
        "parquet_path":str(parqp),
        "parquet_sha256":sha256_file(parqp),
    }
    manp.write_text(json.dumps(man,ensure_ascii=False,indent=2),encoding="utf-8")
    return {"status":"FETCHED","manifest":man}

def build_canonical(cfg):
    out_root=Path(cfg["output_root"])
    shards=sorted((out_root/"raw").glob("*/*.parquet"))
    dfs=[]
    for p in shards:
        try:
            z=pd.read_parquet(p)
            if len(z):
                dfs.append(z)
        except Exception:
            pass

    if dfs:
        hits=pd.concat(dfs,ignore_index=True,sort=False)
    else:
        hits=pd.DataFrame()

    out_root.mkdir(parents=True,exist_ok=True)
    hits_path=out_root/"cninfo_commercial_query_hits_all.parquet"
    hits.to_parquet(hits_path,index=False)

    if hits.empty:
        canonical=pd.DataFrame()
    else:
        for c in ["announcementId","secCode","secName","announcementTitle","announcementTime","storageTime","adjunctUrl","query_group","query_keyword"]:
            if c not in hits.columns:
                hits[c]=None
        hits["announcementId"]=hits["announcementId"].astype("string")
        hits["announcementTitle"]=hits["announcementTitle"].map(clean_title)
        hits["event_time"]=hits["announcementTime"].map(parse_event_time)
        hits["canonical_symbol"]=hits["secCode"].map(canonical_symbol)
        hits["source_name"]="CNINFO"
        hits["pdf_url"]=hits["adjunctUrl"].fillna("").astype(str).map(
            lambda x: cfg["source"]["static_base"] + x.lstrip("/") if x else None
        )
        hits["title_group"]=hits["announcementTitle"].map(lambda x:title_group(x,cfg))

        base_cols=[c for c in hits.columns if c not in {"query_group","query_keyword","query_month","query_se_date","query_page"}]
        first=(hits.sort_values(["announcementId","event_time"],na_position="last")
               .drop_duplicates("announcementId",keep="first")[base_cols].copy())
        agg=(hits.groupby("announcementId",dropna=False)
             .agg(
                 query_keywords=("query_keyword",lambda s:"|".join(sorted(set(str(x) for x in s.dropna())))),
                 query_groups=("query_group",lambda s:"|".join(sorted(set(str(x) for x in s.dropna())))),
                 query_hit_count=("query_keyword","size"),
             ).reset_index())
        canonical=first.merge(agg,on="announcementId",how="left",validate="one_to_one")
        canonical["strategy_domain_equity"]=canonical["canonical_symbol"].notna()
        canonical["candidate_by_title"]=canonical["title_group"].isin(["HARD_CONFIRMATION","SOFT_INTENT","AMBIGUOUS"])

    canonical_path=out_root/"cninfo_commercial_metadata_all.parquet"
    canonical.to_parquet(canonical_path,index=False)

    candidate=canonical[
        canonical.get("strategy_domain_equity",pd.Series(False,index=canonical.index))
        & canonical.get("candidate_by_title",pd.Series(False,index=canonical.index))
    ].copy() if len(canonical) else canonical.copy()
    candidate_path=out_root/"cninfo_commercial_title_candidates.parquet"
    candidate.to_parquet(candidate_path,index=False)

    return hits_path,canonical_path,candidate_path,hits,canonical,candidate

def summarize(cfg, run_mode, start_s, end_s, shard_results, failures):
    out_root=Path(cfg["output_root"])
    hits_path,canonical_path,candidate_path,hits,canonical,candidate=build_canonical(cfg)

    bse_count=int(canonical["canonical_symbol"].astype("string").str.endswith(".BJ",na=False).sum()) if len(canonical) and "canonical_symbol" in canonical else 0
    sh_count=int(canonical["canonical_symbol"].astype("string").str.endswith(".SH",na=False).sum()) if len(canonical) and "canonical_symbol" in canonical else 0
    sz_count=int(canonical["canonical_symbol"].astype("string").str.endswith(".SZ",na=False).sum()) if len(canonical) and "canonical_symbol" in canonical else 0

    title_counts={}
    if len(canonical) and "title_group" in canonical:
        title_counts={str(k):int(v) for k,v in canonical["title_group"].value_counts(dropna=False).items()}

    summary={
        "status":"PASS_CNINFO_COMMERCIAL_METADATA_V0_1_RUN" if not failures else "PARTIAL_CNINFO_COMMERCIAL_METADATA_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "asset_id":cfg["asset_id"],
        "run_mode":run_mode,
        "requested_range":{"start":start_s,"end":end_s},
        "source":{
            "name":"CNINFO",
            "first_party":True,
            "eva_dependency":False,
        },
        "shards":{
            "processed_or_reused":int(len(shard_results)),
            "failures":int(len(failures)),
            "failure_examples":failures[:20],
        },
        "data":{
            "query_hit_rows":int(len(hits)),
            "distinct_announcements":int(len(canonical)),
            "title_candidates":int(len(candidate)),
            "title_group_counts":title_counts,
            "equity_symbols":{
                "SH_rows":sh_count,
                "SZ_rows":sz_count,
                "BJ_rows":bse_count,
            },
            "event_time_nonnull_rate":float(canonical["event_time"].notna().mean()) if len(canonical) and "event_time" in canonical else 0.0,
            "pdf_url_nonnull_rate":float(canonical["pdf_url"].astype("string").fillna("").str.strip().ne("").mean()) if len(canonical) and "pdf_url" in canonical else 0.0,
        },
        "artifacts":{
            "query_hits_all":str(hits_path),
            "canonical_metadata":str(canonical_path),
            "title_candidates":str(candidate_path),
        },
        "next_action":(
            "Audit completeness and title-candidate quality; then download only candidate PDFs. "
            "If BJ coverage is insufficient, add direct BSE acquisition before any Eva use."
        ),
        "safety":cfg["safety"],
    }
    sp=out_root/"cninfo_commercial_metadata_v0_1_summary.json"
    sp.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {out_root}")
    return 0 if not failures else 3

def acquire(cfg, start_s, end_s, run_mode, force=False):
    results=[]
    failures=[]
    for a,b in month_windows(start_s,end_s):
        for group,kws in cfg["query_groups"].items():
            for kw in kws:
                try:
                    r=fetch_shard(cfg,a,b,group,kw,force=force)
                    results.append(r)
                    m=r["manifest"]
                    print(f"{r['status']:>13} {a:%Y-%m} {group:<17} {kw:<8} rows={m.get('rows')} pages={m.get('total_pages')}")
                except Exception as e:
                    msg={"month":f"{a:%Y-%m}","group":group,"keyword":kw,"error":f"{type(e).__name__}: {e}"}
                    failures.append(msg)
                    print(f"FAILED {msg}")
    return summarize(cfg,run_mode,start_s,end_s,results,failures)

def status(cfg):
    out_root=Path(cfg["output_root"])
    manifests=sorted((out_root/"raw").glob("*/*.manifest.json")) if (out_root/"raw").exists() else []
    rows=[]
    for p in manifests:
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    out={
        "status":"PASS_CNINFO_COMMERCIAL_METADATA_V0_1_STATUS",
        "output_root":str(out_root),
        "successful_shards":len([x for x in rows if x.get("status")=="SUCCESS"]),
        "total_rows_across_shards":sum(int(x.get("rows") or 0) for x in rows),
        "months":sorted(set(x.get("month") for x in rows if x.get("month"))),
        "summary_exists":(out_root/"cninfo_commercial_metadata_v0_1_summary.json").exists(),
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0

def check(cfg):
    out={
        "status":"PASS_CNINFO_COMMERCIAL_METADATA_V0_1_CHECK",
        "source":"CNINFO",
        "api_urls":cfg["source"]["api_urls"],
        "full_range":cfg["date_range"],
        "pilot_range":cfg["pilot"],
        "keyword_count":sum(len(v) for v in cfg["query_groups"].values()),
        "output_root":cfg["output_root"],
        "first_party_only":True,
        "eva_dependency":False,
        "network_required_for_acquire":True,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0

def main():
    p=argparse.ArgumentParser()
    p.add_argument("command",choices=["check","pilot","run","status"])
    p.add_argument("--force",action="store_true")
    a=p.parse_args()
    cfg=load_cfg()
    try:
        if a.command=="check":
            rc=check(cfg)
        elif a.command=="status":
            rc=status(cfg)
        elif a.command=="pilot":
            rc=acquire(cfg,cfg["pilot"]["start"],cfg["pilot"]["end"],"PILOT",force=a.force)
        else:
            rc=acquire(cfg,cfg["date_range"]["start"],cfg["date_range"]["end"],"FULL",force=a.force)
        raise SystemExit(rc)
    except Exception as e:
        print(json.dumps({
            "status":"FAIL_CLOSED_CNINFO_COMMERCIAL_METADATA_V0_1",
            "error":f"{type(e).__name__}: {e}",
        },ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
