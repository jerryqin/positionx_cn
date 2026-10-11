
from __future__ import annotations
import argparse, gzip, hashlib, json, re, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path
import pandas as pd, yaml

ROOT=Path(__file__).resolve().parents[1]
CFG=ROOT/"config/data/cninfo_blank_query_exact_date_pilot_v0_1.yaml"

def load_cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))

def safe_text(v):
    if v is None: return ""
    try:
        if pd.isna(v): return ""
    except Exception:
        pass
    return str(v)

def deterministic_rank(v):
    return hashlib.sha256(safe_text(v).encode("utf-8")).hexdigest()

def code6(v):
    m=re.search(r"(\d{6})",safe_text(v).upper())
    return m.group(1) if m else None

def canonical_shsz(v):
    c=code6(v)
    if not c: return None
    if c[:2] in {"60","68"}: return f"{c}.SH"
    if c[:2] in {"00","30"}: return f"{c}.SZ"
    return None

def exchange_params(symbol):
    s=canonical_shsz(symbol)
    if s is None: return None
    if s.endswith(".SH"): return ("sse","sh")
    if s.endswith(".SZ"): return ("szse","sz")
    return None

def parse_local_date(v):
    z=pd.to_datetime(v,errors="coerce")
    if pd.isna(z): return None
    if getattr(z,"tzinfo",None) is not None:
        z=z.tz_convert("Asia/Shanghai").tz_localize(None)
    return z.date().isoformat()

def find_col(df,names):
    low={str(c).lower():c for c in df.columns}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return None

def load_sample(path, cohort, limit):
    p=Path(path)
    if not p.exists(): raise FileNotFoundError(p)
    df=pd.read_csv(p)
    ann=find_col(df,["announcement_id","announcementId"])
    sym=find_col(df,["canonical_symbol","symbol"])
    et=find_col(df,["event_time_local","event_time","announcementTime"])
    title=find_col(df,["title","announcementTitle"])
    if ann is None or sym is None or et is None:
        raise RuntimeError(f"{cohort}: sample missing id/symbol/event_time")
    out=pd.DataFrame({
        "announcement_id":df[ann].astype("string").str.strip(),
        "canonical_symbol":df[sym].map(canonical_shsz),
        "event_date":df[et].map(parse_local_date),
        "title":df[title].astype("string") if title else "",
    })
    out["cohort"]=cohort
    out=out[out["announcement_id"].notna() & out["canonical_symbol"].notna() & out["event_date"].notna()].copy()
    out["_rank"]=out["announcement_id"].map(deterministic_rank)
    return out.sort_values("_rank").head(int(limit)).drop(columns=["_rank"])

def headers(cfg):
    return {
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
        "Content-Type":"application/x-www-form-urlencoded; charset=UTF-8",
        "Referer":cfg["cninfo"]["referer"],
        "X-Requested-With":"XMLHttpRequest",
        "Accept":"application/json, text/javascript, */*; q=0.01",
    }

def post(cfg, date_s, column, plate, page_num):
    body=urllib.parse.urlencode({
        "pageNum":int(page_num),
        "pageSize":int(cfg["cninfo"]["page_size"]),
        "column":column,
        "tabName":"fulltext",
        "plate":plate,
        "stock":"",
        "searchkey":"",
        "secid":"",
        "category":"",
        "trade":"",
        "seDate":f"{date_s}~{date_s}",
        "sortName":"",
        "sortType":"",
        "isHLtitle":"true",
    }).encode("utf-8")
    last=None
    for attempt in range(int(cfg["cninfo"]["retries"])):
        for url in cfg["cninfo"]["api_urls"]:
            try:
                req=urllib.request.Request(url,data=body,method="POST",headers=headers(cfg))
                with urllib.request.urlopen(req,timeout=int(cfg["cninfo"]["timeout_seconds"])) as r:
                    raw=r.read()
                obj=json.loads(raw.decode("utf-8","replace"))
                if not isinstance(obj,dict):
                    raise RuntimeError("response not JSON object")
                return obj,url
            except Exception as e:
                last=e
        time.sleep(float(cfg["cninfo"]["retry_backoff_seconds"])*(attempt+1))
    raise RuntimeError(f"CNINFO failed {date_s} {column}/{plate} page={page_num}: {last!r}")

def fetch_exact_day(cfg,date_s,column,plate):
    first,url=post(cfg,date_s,column,plate,1)
    pages=int(first.get("totalpages") or first.get("totalPages") or 0)
    payloads=[first]
    for p in range(2,pages+1):
        time.sleep(float(cfg["cninfo"]["delay_seconds"]))
        x,_=post(cfg,date_s,column,plate,p)
        payloads.append(x)
    rows=[]
    for pno,obj in enumerate(payloads,1):
        for r in obj.get("announcements") or []:
            z=dict(r)
            z["_page"]=pno
            rows.append(z)
    return rows,payloads,url,pages

def check(cfg):
    paths=[cfg["samples"]["existing_keyword_miss"],cfg["samples"]["unmatched_vocabulary_gap"]]
    ok=all(Path(p).exists() for p in paths)
    out={
        "status":"PASS_CNINFO_BLANK_QUERY_PILOT_V0_1_CHECK" if ok else "BLOCKED_CNINFO_BLANK_QUERY_PILOT_V0_1_CHECK",
        "samples":[{"path":p,"exists":Path(p).exists()} for p in paths],
        "research_domain":"SH/SZ A-shares only",
        "query":"exact event date × exchange, blank searchkey, category blank",
        "network_required":True,
        "pdf_download":False,
    }
    print(json.dumps(out,ensure_ascii=False,indent=2))
    return 0 if ok else 3

def run(cfg):
    a=load_sample(cfg["samples"]["existing_keyword_miss"],"EXISTING_KEYWORD_MISS",cfg["sample_limits"]["existing_keyword_miss"])
    b=load_sample(cfg["samples"]["unmatched_vocabulary_gap"],"UNMATCHED_VOCABULARY_GAP",cfg["sample_limits"]["unmatched_vocabulary_gap"])
    sample=pd.concat([a,b],ignore_index=True)
    sample["column"]=sample["canonical_symbol"].map(lambda s:exchange_params(s)[0] if exchange_params(s) else None)
    sample["plate"]=sample["canonical_symbol"].map(lambda s:exchange_params(s)[1] if exchange_params(s) else None)

    groups=sample[["event_date","column","plate"]].drop_duplicates().sort_values(["event_date","column"])
    outdir=Path(cfg["output_dir"])
    rawdir=outdir/"raw"
    rawdir.mkdir(parents=True,exist_ok=True)

    fetched=[]
    failures=[]
    for _,g in groups.iterrows():
        date_s, column, plate=g["event_date"],g["column"],g["plate"]
        key=f"{date_s}__{column}"
        rawp=rawdir/f"{key}.json.gz"
        manp=rawdir/f"{key}.manifest.json"
        try:
            if rawp.exists() and manp.exists():
                with gzip.open(rawp,"rt",encoding="utf-8") as f:
                    obj=json.load(f)
                rows=obj["rows"]
                pages=obj["pages"]
                url=obj.get("api_url")
                status="REUSE"
            else:
                rows,payloads,url,pages=fetch_exact_day(cfg,date_s,column,plate)
                with gzip.open(rawp,"wt",encoding="utf-8") as f:
                    json.dump({"date":date_s,"column":column,"plate":plate,"pages":pages,"api_url":url,"rows":rows,"payloads":payloads},f,ensure_ascii=False)
                manp.write_text(json.dumps({"status":"SUCCESS","date":date_s,"column":column,"plate":plate,"pages":pages,"rows":len(rows),"api_url":url},ensure_ascii=False,indent=2),encoding="utf-8")
                status="FETCH"
            print(f"{status:>5} {date_s} {column}/{plate} pages={pages} rows={len(rows)}")
            for r in rows:
                z=dict(r)
                z["_query_event_date"]=date_s
                z["_query_column"]=column
                fetched.append(z)
        except Exception as e:
            failures.append({"date":date_s,"column":column,"plate":plate,"error":f"{type(e).__name__}: {e}"})
            print("FAIL",failures[-1])

    fdf=pd.DataFrame(fetched)
    if len(fdf):
        idcol=find_col(fdf,["announcementId","announcement_id"])
        fids=set(fdf[idcol].astype("string").str.strip()) if idcol else set()
    else:
        fids=set()

    sample["recovered_by_blank_exact_date"]=sample["announcement_id"].isin(fids)
    detail=sample.copy()
    detail.to_csv(outdir/"blank_query_pilot_detail_v0_1.csv",index=False,encoding="utf-8-sig")

    by_cohort={}
    for cohort,g in detail.groupby("cohort"):
        n=len(g); k=int(g["recovered_by_blank_exact_date"].sum())
        by_cohort[cohort]={
            "sample_rows":n,
            "recovered":k,
            "recovery_rate":k/n if n else None,
        }

    threshold=float(cfg["decision"]["exact_date_blank_query_recovery_min"])
    all_rates=[v["recovery_rate"] for v in by_cohort.values() if v["recovery_rate"] is not None]
    viable=(not failures) and all(r>=threshold for r in all_rates)

    result={
        "status":"PASS_CNINFO_BLANK_QUERY_PILOT_V0_1_RUN" if not failures else "PARTIAL_CNINFO_BLANK_QUERY_PILOT_V0_1_RUN",
        "generated_at":datetime.now().isoformat(timespec="seconds"),
        "pilot_id":cfg["pilot_id"],
        "sample":{
            "rows":int(len(sample)),
            "date_exchange_queries":int(len(groups)),
            "by_cohort":by_cohort,
        },
        "network":{
            "query":"exact event date × SSE/SZSE with blank searchkey/category",
            "failed_queries":len(failures),
            "failures":failures[:20],
        },
        "decision":{
            "threshold":threshold,
            "broad_first_party_metadata_viable":bool(viable),
            "if_viable":"Stop keyword-expansion as the primary acquisition architecture. Build reusable SH/SZ all-announcement metadata from CNINFO, then filter/classify locally.",
            "if_not_viable":"Diagnose CNINFO endpoint/search semantics and, if needed, use direct SSE/SZSE metadata as layer-2 supplements.",
        },
        "safety":cfg["safety"],
    }
    (outdir/"cninfo_blank_query_exact_date_pilot_v0_1_summary.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print(f"OUTPUT: {outdir}")
    return 0 if not failures else 3

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["check","run"])
    a=p.parse_args(); cfg=load_cfg()
    try: raise SystemExit(check(cfg) if a.command=="check" else run(cfg))
    except Exception as e:
        print(json.dumps({"status":"FAIL_CLOSED_CNINFO_BLANK_QUERY_PILOT_V0_1","error":f"{type(e).__name__}: {e}"},ensure_ascii=False,indent=2))
        raise SystemExit(3)

if __name__=="__main__":
    main()
